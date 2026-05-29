"""Watcher agent (Risk Monitor) — spec §5.4 / FR-4.x.

Reads the Organiser's normalized Transactions + Obligations, models monthly
cash-flow, projects the running balance over a 30-60 day horizon, and flags
"danger zones" (e.g. a projected shortfall). It produces a RiskAssessment with a
0-100 score, the drivers behind it, and plain-language ADVISORY alerts.

Strictly advisory (GDPR Art. 22 / EU AI Act posture): every alert carries an
explanation and optional suggested actions, and nothing is ever auto-decided.
The numbers are deterministic Python; the LLM only writes the summary text.

Convention: transaction amount > 0 is an inflow, < 0 is an outflow.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .. import config, domain_store
from ..schemas import (
    Alert,
    AlertPreferences,
    CashflowPoint,
    CashflowSummary,
    RiskAssessment,
    RiskDriver,
)
from . import llm


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _parse_date(s: Any) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)[:10]).date()
    except (ValueError, TypeError):
        return None


def _min_payment_for(ob: dict[str, Any]) -> float:
    """Use the stated minimum payment; estimate ~2% of balance if it's missing."""
    mp = ob.get("min_payment")
    if mp is not None:
        return float(mp)
    bal = ob.get("balance")
    return round(float(bal) * 0.02, 2) if bal else 0.0


# ---------------------------------------------------------------------------
# Cash-flow modelling
# ---------------------------------------------------------------------------
def _cashflow_model(transactions: list[dict[str, Any]], obligations: list[dict[str, Any]]):
    """Estimate monthly income / expenses / obligations and the typical pay day."""
    dated = [(d, float(t["amount"])) for t in transactions
             if (d := _parse_date(t.get("date"))) is not None and t.get("amount") is not None]

    months = 1.0
    if len(dated) >= 2:
        span = (max(d for d, _ in dated) - min(d for d, _ in dated)).days
        months = max(span / 30.0, 1.0)

    inflows = [(d, a) for d, a in dated if a > 0]
    outflows = [a for _, a in dated if a < 0]
    monthly_income = round(sum(a for _, a in inflows) / months, 2)
    monthly_expenses = round(sum(-a for a in outflows) / months, 2)

    # Pay day = day-of-month of the largest single inflow (assume salary); default 1.
    payday = max(inflows, key=lambda x: x[1])[0].day if inflows else 1

    monthly_obligations = round(sum(_min_payment_for(o) for o in obligations), 2)
    return monthly_income, monthly_expenses, monthly_obligations, months, payday


def _project(
    *, start_balance, monthly_income, monthly_expenses, obligations, payday,
    horizon_days, safety_buffer,
) -> tuple[list[CashflowPoint], Optional[str]]:
    today = date.today()
    balance = start_balance
    daily_expense = monthly_expenses / 30.0
    due = [(int(o["due_day"]), _min_payment_for(o)) for o in obligations if o.get("due_day")]
    points = [CashflowPoint(date=today.isoformat(), projected_balance=round(balance, 2))]
    shortfall: Optional[str] = None
    for d in range(1, horizon_days + 1):
        day = today + timedelta(days=d)
        if day.day == payday:
            balance += monthly_income
        balance -= daily_expense
        for due_day, amt in due:
            if day.day == due_day:
                balance -= amt
        points.append(CashflowPoint(date=day.isoformat(), projected_balance=round(balance, 2)))
        if shortfall is None and balance < safety_buffer:
            shortfall = day.isoformat()
    return points, shortfall


# ---------------------------------------------------------------------------
# Scoring + alerts
# ---------------------------------------------------------------------------
def _score_and_drivers(
    *, monthly_income, monthly_expenses, monthly_obligations,
    start_balance, shortfall_date, horizon_days,
) -> tuple[int, list[RiskDriver]]:
    drivers: list[RiskDriver] = []
    total_outflow = monthly_expenses + monthly_obligations

    # 1. Cash-flow coverage: outflow vs income.
    ratio = (total_outflow / monthly_income) if monthly_income > 0 else 2.0
    cov = _clamp((ratio - 0.8) / 0.7, 0, 1) * 35  # ratio 0.8->0 ... 1.5->35
    drivers.append(RiskDriver(
        factor="cashflow_coverage",
        detail=f"Monthly outgoings are {ratio*100:.0f}% of income.",
        impact=round(cov, 1),
    ))

    # 2. Projected shortfall proximity.
    prox = 0.0
    if shortfall_date:
        days = max((_parse_date(shortfall_date) - date.today()).days, 0)
        prox = (1 - days / horizon_days) * 35
        drivers.append(RiskDriver(
            factor="projected_shortfall",
            detail=f"Balance is projected to run short in {days} day(s).",
            impact=round(prox, 1),
        ))

    # 3. Debt-to-income (obligation minimums vs income).
    dti = (monthly_obligations / monthly_income) if monthly_income > 0 else 1.0
    dti_impact = _clamp(dti / 0.4, 0, 1) * 20
    drivers.append(RiskDriver(
        factor="debt_to_income",
        detail=f"Minimum debt payments are {dti*100:.0f}% of income.",
        impact=round(dti_impact, 1),
    ))

    # 4. Buffer runway (months of outgoings the starting balance covers).
    runway = (start_balance / total_outflow) if total_outflow > 0 else 99.0
    buf = _clamp((1.0 - runway) / 1.0, 0, 1) * 10
    drivers.append(RiskDriver(
        factor="low_buffer",
        detail=f"Cash buffer covers about {runway:.1f} month(s) of outgoings.",
        impact=round(buf, 1),
    ))

    score = int(round(_clamp(cov + prox + dti_impact + buf, 0, 100)))
    return score, drivers


def _band(score: int) -> str:
    if score < 25:
        return "low"
    if score < 50:
        return "moderate"
    if score < 75:
        return "elevated"
    return "high"


def _build_alerts(
    *, monthly_income, monthly_expenses, monthly_obligations, monthly_net,
    shortfall_date, start_balance, horizon_days, sensitivity,
) -> list[Alert]:
    m = config.SENSITIVITY_MULTIPLIERS.get(sensitivity, 1.0)
    alerts: list[Alert] = []
    total_outflow = monthly_expenses + monthly_obligations

    def add(type_, severity, title, explanation, actions):
        alerts.append(Alert(
            id=uuid.uuid4().hex, type=type_, severity=severity, title=title,
            explanation=explanation, suggested_actions=actions, created_at=_now(),
        ))

    if monthly_income <= 0:
        add("no_income_data", "info", "No income detected",
            "We couldn't find income in your transactions, so the projection may be "
            "incomplete. Add an income source or connect the right account for a fuller picture.",
            ["Check that the account with your salary is connected."])

    if shortfall_date:
        days = max((_parse_date(shortfall_date) - date.today()).days, 0)
        sev = "critical" if days <= 30 * m else "warning"
        add("projected_shortfall", sev, "Possible cash shortfall ahead",
            f"At the current pace your balance could dip below your safety buffer around "
            f"{shortfall_date} (about {days} day(s) away). This is an early heads-up, not a certainty.",
            ["Review upcoming payments and see if any can be timed differently.",
             "Try the simulator to see how a small change affects this date.",
             "Consider which expenses are flexible this month."])

    if monthly_net < 0:
        sev = "critical" if monthly_net < -0.1 * max(monthly_income, 1) * m else "warning"
        add("negative_cashflow", sev, "Spending more than you earn",
            f"Your average monthly outgoings (≈{total_outflow:.0f}) exceed your income "
            f"(≈{monthly_income:.0f}) by about {-monthly_net:.0f}. Over time this erodes your buffer.",
            ["Identify your largest flexible expenses.",
             "A payoff plan can lower interest costs over time."])

    dti = (monthly_obligations / monthly_income) if monthly_income > 0 else 1.0
    if dti > 0.35 * m:
        add("high_debt_burden", "warning", "High share of income goes to debt",
            f"About {dti*100:.0f}% of your income goes to minimum debt payments. "
            "Lowering high-interest balances first usually helps most.",
            ["Ask the Planner for an avalanche payoff plan.",
             "Consider drafting a hardship/lower-rate message to a lender."])

    runway = (start_balance / total_outflow) if total_outflow > 0 else 99.0
    if runway < 1.0 * m:
        add("low_buffer", "info" if runway >= 0.5 else "warning", "Thin cash buffer",
            f"Your starting balance covers roughly {runway:.1f} month(s) of outgoings. "
            "A small emergency buffer reduces the risk of a missed payment.",
            ["Set aside a small amount toward a starter buffer when possible."])

    return alerts


# ---------------------------------------------------------------------------
# LLM summary (optional, with deterministic fallback)
# ---------------------------------------------------------------------------
def _template_summary(score, band, shortfall_date, monthly_net) -> str:
    parts = [f"Your financial-stress level looks {band} ({score}/100)."]
    if monthly_net >= 0:
        parts.append(f"You're roughly cash-flow positive (about {monthly_net:.0f}/month spare).")
    else:
        parts.append(f"You're spending about {-monthly_net:.0f}/month more than you earn.")
    if shortfall_date:
        parts.append(f"Watch out for a possible shortfall around {shortfall_date}.")
    parts.append("This is guidance only — you decide what to do next.")
    return " ".join(parts)


def _llm_summary(score, band, drivers, alerts, cashflow) -> Optional[str]:
    if not llm.is_available():
        return None
    facts = {
        "score": score, "band": band,
        "monthly_income": cashflow.monthly_income,
        "monthly_expenses": cashflow.monthly_expenses,
        "monthly_obligations": cashflow.monthly_obligations,
        "monthly_net": cashflow.monthly_net,
        "drivers": [d.detail for d in drivers],
        "alert_titles": [a.title for a in alerts],
    }
    system = (
        "You are a calm, non-judgmental financial assistant. Given the facts, write a "
        "2-3 sentence plain-language summary of the person's cash-flow risk. Be supportive, "
        "concrete, and never alarming. Amounts are in euros (EUR) — never use a '$' sign. "
        "End by reminding them this is guidance only and they decide. "
        'Reply JSON: {"summary": "..."}.'
    )
    out = llm.chat_json(system, f"Facts: {facts}")
    if isinstance(out, dict) and isinstance(out.get("summary"), str):
        return out["summary"].strip()
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def assess(
    *,
    user_id: str,
    prefs: AlertPreferences,
    current_balance: Optional[float] = None,
    horizon_days: Optional[int] = None,
    use_llm: bool = True,
) -> RiskAssessment:
    transactions = domain_store.list_transactions(user_id=user_id, limit=100000)["transactions"]
    obligations = domain_store.list_obligations(user_id=user_id)

    horizon = horizon_days or prefs.horizon_days or config.DEFAULT_HORIZON_DAYS
    (monthly_income, monthly_expenses, monthly_obligations,
     months, payday) = _cashflow_model(transactions, obligations)
    monthly_net = round(monthly_income - monthly_expenses - monthly_obligations, 2)

    # Starting balance: use the supplied figure, else assume ~one month of income.
    assumed = current_balance is None
    start_balance = round(monthly_income if assumed else current_balance, 2)

    projection, shortfall_date = _project(
        start_balance=start_balance, monthly_income=monthly_income,
        monthly_expenses=monthly_expenses, obligations=obligations, payday=payday,
        horizon_days=horizon, safety_buffer=prefs.safety_buffer,
    )
    score, drivers = _score_and_drivers(
        monthly_income=monthly_income, monthly_expenses=monthly_expenses,
        monthly_obligations=monthly_obligations, start_balance=start_balance,
        shortfall_date=shortfall_date, horizon_days=horizon,
    )
    alerts = _build_alerts(
        monthly_income=monthly_income, monthly_expenses=monthly_expenses,
        monthly_obligations=monthly_obligations, monthly_net=monthly_net,
        shortfall_date=shortfall_date, start_balance=start_balance,
        horizon_days=horizon, sensitivity=prefs.sensitivity,
    )

    cashflow = CashflowSummary(
        monthly_income=monthly_income, monthly_expenses=monthly_expenses,
        monthly_obligations=monthly_obligations, monthly_net=monthly_net,
        months_of_history=round(months, 1), starting_balance=start_balance,
        starting_balance_assumed=assumed,
    )

    summary, llm_used = None, False
    if use_llm:
        summary = _llm_summary(score, _band(score), drivers, alerts, cashflow)
        llm_used = summary is not None
    if summary is None:
        summary = _template_summary(score, _band(score), shortfall_date, monthly_net)

    return RiskAssessment(
        id=uuid.uuid4().hex, user_id=user_id, score=score, band=_band(score),
        projected_shortfall_date=shortfall_date, horizon_days=horizon,
        summary=summary, drivers=drivers, alerts=alerts, cashflow=cashflow,
        projection=projection, llm_used=llm_used, created_at=_now(),
    )
