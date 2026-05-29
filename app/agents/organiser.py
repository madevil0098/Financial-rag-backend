"""Organiser agent — normalizes raw ingested tables into canonical entities.

This is the first agent in the Debt OS pipeline (Section 6 of the spec). It takes
a raw, arbitrarily-shaped dataset produced by ingestion and maps its columns onto
the canonical `Obligation` or `Transaction` model the downstream agents (Watcher,
Planner, Negotiator) consume.

Mapping strategy:
  1. Heuristic column matching (synonyms + fuzzy/token overlap) — always runs,
     instant, offline, deterministic.
  2. Optional LLM refinement (local Ollama, fast model) — corrects the heuristic
     mapping for messy/ambiguous columns. Falls back to heuristics on any failure.
  3. Manual override always wins.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from .. import config, domain_store, storage
from ..schemas import NormaliseResult, TargetEntity
from . import llm

# Canonical field -> known synonyms (normalized form).
OBLIGATION_FIELDS: dict[str, list[str]] = {
    "creditor": ["creditor", "lender", "bank", "provider", "company", "merchant",
                 "counterparty", "name", "institution", "issuer", "account_name"],
    "kind": ["kind", "type", "debt_type", "product", "product_type",
             "account_type", "obligation_type"],
    "balance": ["balance", "outstanding", "outstanding_amt", "outstanding_balance",
                "amount", "owed", "amount_owed", "principal", "current_balance", "debt"],
    "apr": ["apr", "aer", "interest", "interest_rate", "interest_pct", "rate"],
    "min_payment": ["min_payment", "minimum_payment", "min_pay", "minimum",
                    "min_due", "minimum_due", "min_amount"],
    "due_day": ["due_day", "due_date", "due", "payment_date", "due_dom",
                "duedate", "next_payment_date", "payment_due_date"],
    "currency": ["currency", "ccy", "curr"],
}

TRANSACTION_FIELDS: dict[str, list[str]] = {
    "date": ["date", "transaction_date", "posted", "posting_date", "timestamp",
             "value_date", "booking_date", "datetime", "txn_date"],
    # Note: "credit"/"debit" deliberately excluded — "credit" substring-matches
    # "creditor" and mis-classifies obligation tables as transactions.
    "amount": ["amount", "value", "amt", "transaction_amount", "sum", "txn_amount"],
    "category": ["category", "type", "classification", "tag", "mcc"],
    "counterparty": ["counterparty", "payee", "merchant", "description", "desc",
                     "narrative", "reference", "details", "name", "memo"],
}

_OBLIGATION_CATEGORIES = {"loan", "credit_card", "bnpl", "overdraft", "payment_due"}

# Strict, non-overlapping keyword sets used ONLY to decide obligation vs transaction
# (the loose synonym lists above are too entangled for that decision).
_APR_KW = {"apr", "aer", "interest", "interestrate", "interestpct", "rate"}
_MIN_KW = {"minpayment", "minimumpayment", "minpay", "minimum", "mindue", "minimumdue"}
_BAL_KW = {"balance", "outstanding", "outstandingbalance", "outstandingamt",
           "owed", "amountowed", "principal", "currentbalance"}
# Deliberately strict: real creditor words only, NOT merchant/name/payee (transaction-ish).
_CREDITOR_KW = {"creditor", "lender", "issuer", "institution"}
_DATE_KW = {"date", "transactiondate", "postingdate", "posted", "valuedate",
            "bookingdate", "timestamp", "datetime", "txndate"}
_AMOUNT_KW = {"amount", "amt", "value", "transactionamount", "txnamount", "debit", "credit"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", str(text).lower())).strip("_")


def _score(canonical_synonyms: list[str], raw_col: str) -> float:
    col = _norm(raw_col)
    col_collapsed = col.replace("_", "")  # "min_pay" and "minpay" should match
    col_tokens = set(col.split("_"))
    best = 0.0
    for syn in canonical_synonyms:
        s = _norm(syn)
        s_collapsed = s.replace("_", "")
        if col == s or col_collapsed == s_collapsed:
            return 100.0
        if s in col or col in s or s_collapsed in col_collapsed or col_collapsed in s_collapsed:
            best = max(best, 75.0)
        overlap = col_tokens & set(s.split("_"))
        if overlap:
            best = max(best, 40.0 + 10.0 * len(overlap))
    return best


def _heuristic_mapping(fields: dict[str, list[str]], columns: list[str]) -> dict[str, Optional[str]]:
    """Greedy best-score assignment; each raw column used at most once."""
    candidates = []
    for field, synonyms in fields.items():
        for col in columns:
            sc = _score(synonyms, col)
            if sc >= 40:
                candidates.append((sc, field, col))
    candidates.sort(reverse=True)
    mapping: dict[str, Optional[str]] = {f: None for f in fields}
    used_cols: set[str] = set()
    for _, field, col in candidates:
        if mapping[field] is None and col not in used_cols:
            mapping[field] = col
            used_cols.add(col)
    return mapping


def _llm_refine(
    fields: dict[str, list[str]],
    columns: list[str],
    sample_rows: list[dict[str, Any]],
    heuristic: dict[str, Optional[str]],
) -> Optional[dict[str, Optional[str]]]:
    if not llm.is_available():
        return None
    field_list = ", ".join(fields.keys())
    system = (
        "You map messy financial table columns to a fixed canonical schema. "
        "For each canonical field choose the single best matching raw column, or null "
        "if none fits. Use ONLY column names from the provided list. "
        'Reply with JSON: {"mapping": {<canonical_field>: <raw_column_or_null>, ...}}.'
    )
    user = (
        f"Canonical fields: {field_list}\n"
        f"Raw columns: {columns}\n"
        f"Sample rows: {sample_rows}"
    )
    out = llm.chat_json(system, user)
    if not isinstance(out, dict):
        return None
    raw_map = out.get("mapping", out)
    if not isinstance(raw_map, dict):
        return None
    # Merge over the heuristic: the LLM only *overrides* with a valid non-null
    # column, and never discards a heuristic match it happened to miss.
    merged: dict[str, Optional[str]] = dict(heuristic)
    for field in fields:
        val = raw_map.get(field)
        if isinstance(val, str) and val in columns:
            merged[field] = val
    return merged


# --- type coercion -------------------------------------------------------
def _to_float(series: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(series):
        return series.astype("float64")
    cleaned = (
        series.astype(str)
        .str.replace(r"[^0-9.\-]", "", regex=True)
        .replace("", None)
    )
    return pd.to_numeric(cleaned, errors="coerce")


def _to_due_day(series: pd.Series) -> pd.Series:
    num = pd.to_numeric(series, errors="coerce")
    if num.notna().any() and num.dropna().between(1, 31).all():
        return num
    dt = pd.to_datetime(series, errors="coerce")
    return dt.dt.day


def _to_iso_date(series: pd.Series) -> pd.Series:
    dt = pd.to_datetime(series, errors="coerce")
    return dt.dt.strftime("%Y-%m-%d")


_KIND_RULES = [
    (("credit", "card"), "credit_card"),
    (("card",), "credit_card"),
    (("bnpl",), "bnpl"),
    (("klarna",), "bnpl"),
    (("buy", "now"), "bnpl"),
    (("overdraft",), "overdraft"),
    (("mortgage",), "mortgage"),
    (("loan",), "loan"),
]


def _classify_kind(value: Any, category: Optional[str]) -> str:
    text = _norm(value) if value is not None and pd.notna(value) else ""
    for needles, kind in _KIND_RULES:
        if all(n in text for n in needles):
            return kind
    if category in _OBLIGATION_CATEGORIES and category != "payment_due":
        return category
    return "other"


def _clean_str(v: Any) -> Optional[str]:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def _matches_kw(columns: list[str], keywords: set[str]) -> bool:
    """True if any column matches a keyword by collapsed-equality or whole-token
    (not substring — so 'credit' does NOT match 'creditor')."""
    for col in columns:
        n = _norm(col)
        if n.replace("_", "") in keywords:
            return True
        if set(n.split("_")) & keywords:
            return True
    return False


def _decide_target(category: Optional[str], columns: list[str]) -> TargetEntity:
    """Pick obligation vs transaction PER TABLE.

    A single multi-table upload can mix types (e.g. a Loans sheet + a Statement
    sheet), so this decides per table from distinctive column signals. Category is
    only a tiebreaker when the columns are ambiguous.
    """
    has_apr = _matches_kw(columns, _APR_KW)
    has_min = _matches_kw(columns, _MIN_KW)
    has_bal = _matches_kw(columns, _BAL_KW)
    has_creditor = _matches_kw(columns, _CREDITOR_KW)
    has_date = _matches_kw(columns, _DATE_KW)
    has_amount = _matches_kw(columns, _AMOUNT_KW)

    # Decisive obligation signals (no transaction analog).
    if has_apr or has_min:
        return "obligation"
    if has_creditor and has_bal:
        return "obligation"
    # A statement: a date + an amount and no creditor column.
    if has_date and has_amount and not has_creditor:
        return "transaction"
    # Ambiguous -> trust the category label.
    if category == "bank_statement":
        return "transaction"
    if category in _OBLIGATION_CATEGORIES:
        return "obligation"
    # Last resort.
    return "transaction" if (has_date and has_amount) else "obligation"


def _build_obligations(df, mapping, *, user_id, source, category, currency) -> list[dict[str, Any]]:
    records = []
    bal = _to_float(df[mapping["balance"]]) if mapping["balance"] else None
    apr = _to_float(df[mapping["apr"]]) if mapping["apr"] else None
    minp = _to_float(df[mapping["min_payment"]]) if mapping["min_payment"] else None
    due = _to_due_day(df[mapping["due_day"]]) if mapping["due_day"] else None
    # A numeric "kind" column is a mis-map (e.g. the LLM picked a balance column);
    # ignore it and fall back to classifying from the creditor name.
    kind_col = mapping["kind"]
    if kind_col and pd.api.types.is_numeric_dtype(df[kind_col]):
        kind_col = None
    for i in range(len(df)):
        creditor = _clean_str(df[mapping["creditor"]].iloc[i]) if mapping["creditor"] else None
        # Prefer an explicit kind column; otherwise infer the type from the creditor name.
        kind_raw = df[kind_col].iloc[i] if kind_col else creditor
        cur = _clean_str(df[mapping["currency"]].iloc[i]) if mapping["currency"] else None
        records.append({
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "creditor": creditor,
            "kind": _classify_kind(kind_raw, category),
            "balance": None if bal is None or pd.isna(bal.iloc[i]) else float(bal.iloc[i]),
            "apr": None if apr is None or pd.isna(apr.iloc[i]) else float(apr.iloc[i]),
            "min_payment": None if minp is None or pd.isna(minp.iloc[i]) else float(minp.iloc[i]),
            "due_day": None if due is None or pd.isna(due.iloc[i]) else int(due.iloc[i]),
            "currency": cur or currency,
            "source": source,
            "source_category": category,
            "created_at": _now().isoformat(),
        })
    return records


def _build_transactions(df, mapping, *, user_id, source) -> list[dict[str, Any]]:
    records = []
    amount = _to_float(df[mapping["amount"]]) if mapping["amount"] else None
    dates = _to_iso_date(df[mapping["date"]]) if mapping["date"] else None
    for i in range(len(df)):
        records.append({
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "date": None if dates is None or pd.isna(dates.iloc[i]) else str(dates.iloc[i]),
            "amount": None if amount is None or pd.isna(amount.iloc[i]) else float(amount.iloc[i]),
            "category": _clean_str(df[mapping["category"]].iloc[i]) if mapping["category"] else None,
            "counterparty": _clean_str(df[mapping["counterparty"]].iloc[i]) if mapping["counterparty"] else None,
            "source": source,
            "created_at": _now().isoformat(),
        })
    return records


def normalise_dataset(
    dataset_id: str,
    *,
    target: Optional[TargetEntity] = None,
    use_llm: bool = True,
    mapping_override: Optional[dict[str, Optional[str]]] = None,
    user_id: str = config.DEFAULT_USER_ID,
    currency: str = config.DEFAULT_CURRENCY,
) -> Optional[NormaliseResult]:
    """Normalize one ingested dataset into Obligation/Transaction records."""
    df = storage.load_dataframe(dataset_id)
    meta = storage.get_meta(dataset_id)
    if df is None or meta is None:
        return None

    columns = [str(c) for c in df.columns]
    category = meta.category
    target = target or _decide_target(category, columns)
    fields = OBLIGATION_FIELDS if target == "obligation" else TRANSACTION_FIELDS

    mapping = _heuristic_mapping(fields, columns)
    mapping_source = "heuristic"
    llm_model = None

    if use_llm:
        sample = df.head(3).where(pd.notna(df.head(3)), None).to_dict(orient="records")
        refined = _llm_refine(fields, columns, sample, mapping)
        if refined is not None:
            mapping = refined
            mapping_source = "llm"
            llm_model = config.OLLAMA_MODEL

    if mapping_override:
        for f in fields:
            if f in mapping_override:
                ov = mapping_override[f]
                mapping[f] = ov if (ov in columns) else None
        mapping_source = "override"

    if target == "obligation":
        records = _build_obligations(
            df, mapping, user_id=user_id, source=dataset_id,
            category=category, currency=currency,
        )
    else:
        records = _build_transactions(df, mapping, user_id=user_id, source=dataset_id)

    domain_store.replace_by_source(target, dataset_id, records)

    return NormaliseResult(
        dataset_id=dataset_id,
        target=target,
        category=category,
        mapping=mapping,
        unmapped_fields=[f for f, c in mapping.items() if c is None],
        record_count=len(records),
        mapping_source=mapping_source,
        llm_model=llm_model,
        sample=records[:3],
    )
