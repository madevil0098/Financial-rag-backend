"""Supervisor agent — orchestrates the worker agents (spec §6).

This is the "software manages everything else" brain behind the one-input flow:
given freshly ingested data, it normalises every table (Organiser), auto-labels
each table's category, then runs the Watcher and Planner so the user gets a full
picture from a single upload. Strictly advisory — it routes and logs, never moves
money.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from .. import config, domain_store, planner_store, storage, watcher_store
from ..schemas import AlertPreferences, ProcessedTable
from . import organiser, planner, watcher


def _infer_category(result) -> str:
    """Pick a friendly category label from what the Organiser found."""
    if result.target == "transaction":
        return "bank_statement"
    kinds = Counter(
        r.get("kind") for r in result.sample if r.get("kind") and r.get("kind") != "other"
    )
    if len(kinds) > 1:
        return "debts"  # a mixed table of several debt types
    if kinds:
        return kinds.most_common(1)[0][0]
    return "loan"


def process_group(group_id: str, *, use_llm: bool = False) -> list[ProcessedTable]:
    """Normalise every table from one ingestion group and auto-label its category."""
    datasets = storage.list_datasets(group_id=group_id)
    out: list[ProcessedTable] = []
    for ds in datasets:
        res = organiser.normalise_dataset(ds.id, use_llm=use_llm)
        if res is None:
            continue
        category = _infer_category(res)
        storage.update_category(ds.id, category)
        out.append(ProcessedTable(
            dataset_id=ds.id, name=ds.name, target=res.target, category=category,
            record_count=res.record_count, mapping_source=res.mapping_source,
        ))
    return out


def _load_prefs(user_id: str) -> AlertPreferences:
    raw = watcher_store.get_preferences(user_id)
    return AlertPreferences(**raw) if raw else AlertPreferences(user_id=user_id)


def run_agents(
    *,
    user_id: str,
    use_llm: bool = True,
    monthly_budget: Optional[float] = None,
    current_balance: Optional[float] = None,
) -> dict[str, Any]:
    """Run Planner + Watcher over whatever normalised data the user now has."""
    obligations = domain_store.list_obligations(user_id=user_id)
    txn_total = domain_store.list_transactions(user_id=user_id, limit=1)["total"]
    steps: list[str] = []

    plan = None
    if obligations:
        # With no budget given, default to 25% above the minimums so the auto-plan
        # shows a real debt-free date and interest saved (the user can tune it later).
        budget = monthly_budget
        if budget is None:
            total_min = sum(planner._min_payment_for(o) for o in obligations)
            budget = round(total_min * 1.25, 2)
        plan = planner.build_plan(
            user_id=user_id, monthly_budget=budget,
            method="avalanche", use_llm=use_llm,
        )
        if plan is not None:
            planner_store.save_plan(user_id, plan.model_dump(mode="json"))
            steps.append(f"Planner built an avalanche payoff plan for {len(obligations)} debts.")
    else:
        steps.append("No debts found — skipped the payoff plan.")

    assessment = None
    if obligations or txn_total:
        prefs = _load_prefs(user_id)
        assessment = watcher.assess(
            user_id=user_id, prefs=prefs,
            current_balance=current_balance, use_llm=use_llm,
        )
        watcher_store.save_assessment(user_id, assessment.model_dump(mode="json"))
        steps.append(f"Watcher assessed cash-flow risk ({assessment.band}, {assessment.score}/100).")
    else:
        steps.append("No financial data to assess yet.")

    return {"plan": plan, "assessment": assessment, "steps": steps}
