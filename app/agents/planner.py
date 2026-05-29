"""Planner agent (Strategy) — spec §5.5 (payoff plan) + §5.6 (what-if simulation).

Reads the Organiser's Obligations and builds a prioritised payoff plan within an
affordability budget: avalanche (highest APR first) or snowball (lowest balance
first). Shows the projected debt-free date and interest saved vs paying only the
minimums. Also runs what-if simulations (extra payment / windfall / rate change).

ADVISORY ONLY — this plans and projects; it never moves money. The user acts.
The amortization maths is deterministic; the LLM only writes the explanation.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from .. import config, domain_store, planner_store
from ..schemas import (
    DebtSeries,
    PayoffMethod,
    PayoffPoint,
    PlanStep,
    PlanSummary,
    RepaymentPlan,
    StrategyComparison,
)
from . import llm


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    year = d.year + m // 12
    month = m % 12 + 1
    # clamp day to the last valid day of the target month
    day = min(d.day, [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _min_payment_for(ob: dict[str, Any]) -> float:
    mp = ob.get("min_payment")
    if mp is not None:
        return float(mp)
    bal = ob.get("balance")
    return round(float(bal) * 0.02, 2) if bal else 0.0


def _prepare(obligations: list[dict[str, Any]], rate_overrides: dict[str, float]) -> list[dict[str, Any]]:
    debts = []
    for o in obligations:
        bal = float(o.get("balance") or 0)
        if bal <= 0:
            continue
        debts.append({
            "id": o["id"],
            "creditor": o.get("creditor"),
            "bal": bal,
            "balance0": bal,
            "apr": float(rate_overrides.get(o["id"], o.get("apr") or 0.0)),
            "min": _min_payment_for(o),
        })
    return debts


def _simulate(
    debts: list[dict[str, Any]],
    monthly_budget: float,
    method: PayoffMethod,
    *,
    extra: float = 0.0,
    windfall: float = 0.0,
    windfall_month: int = 1,
    max_months: int = config.MAX_PAYOFF_MONTHS,
) -> dict[str, Any]:
    """Month-by-month amortization. Returns months, total_interest, projection, month-1 allocation."""
    work = [dict(d) for d in debts]
    total_interest = 0.0
    projection = [(0, round(sum(w["bal"] for w in work), 2))]
    # Per-debt balance trajectory (month 0 = starting balance), aligned with projection.
    per_debt: dict[str, list[float]] = {w["id"]: [round(w["bal"], 2)] for w in work}
    month1_alloc: dict[str, float] = {}
    payoff_month: dict[str, int] = {}
    months = 0

    def priority(active):
        if method == "avalanche":
            return sorted(active, key=lambda w: (-w["apr"], w["bal"]))
        return sorted(active, key=lambda w: (w["bal"], -w["apr"]))

    while any(w["bal"] > 0.005 for w in work) and months < max_months:
        months += 1
        # 1) accrue monthly interest
        for w in work:
            if w["bal"] > 0:
                interest = w["bal"] * (w["apr"] / 100.0 / 12.0)
                w["bal"] += interest
                total_interest += interest
        # 2) this month's spendable budget
        budget = monthly_budget + extra
        if months == windfall_month:
            budget += windfall
        alloc = {w["id"]: 0.0 for w in work}
        active = priority([w for w in work if w["bal"] > 0])
        # 3) cover minimums in priority order
        for w in active:
            pay = min(w["min"], w["bal"], budget)
            if pay > 0:
                w["bal"] -= pay
                budget -= pay
                alloc[w["id"]] += pay
        # 4) throw everything left at the top-priority debt(s)
        for w in active:
            if budget <= 0.005:
                break
            if w["bal"] > 0:
                pay = min(budget, w["bal"])
                w["bal"] -= pay
                budget -= pay
                alloc[w["id"]] += pay
        # record payoff month as debts clear
        for w in work:
            if w["bal"] <= 0.005 and w["id"] not in payoff_month and w["balance0"] > 0:
                payoff_month[w["id"]] = months
        if months == 1:
            month1_alloc = {k: round(v, 2) for k, v in alloc.items()}
        projection.append((months, round(sum(max(w["bal"], 0) for w in work), 2)))
        for w in work:
            per_debt[w["id"]].append(round(max(w["bal"], 0), 2))

    feasible = all(w["bal"] <= 0.005 for w in work)
    return {
        "per_debt": per_debt,
        "months": months if feasible else None,
        "feasible": feasible,
        "total_interest": round(total_interest, 2),
        "projection": projection,
        "month1_alloc": month1_alloc,
        "payoff_month": payoff_month,
    }


def _summary_numbers(sim: dict[str, Any], method: PayoffMethod) -> PlanSummary:
    months = sim["months"]
    dfd = _add_months(date.today(), months).isoformat() if months else None
    return PlanSummary(
        method=method,
        months_to_debt_free=months,
        debt_free_date=dfd,
        total_interest=sim["total_interest"],
        feasible=sim["feasible"],
    )


def _projection_points(sim: dict[str, Any]) -> list[PayoffPoint]:
    today = date.today()
    return [
        PayoffPoint(month=m, date=_add_months(today, m).isoformat(), total_balance=bal)
        for m, bal in sim["projection"]
    ]


def _default_budget(debts: list[dict[str, Any]], requested: Optional[float]) -> tuple[float, float]:
    total_min = round(sum(d["min"] for d in debts), 2)
    if requested is None or requested < total_min:
        return total_min, total_min
    return round(requested, 2), total_min


def _template_summary(method, dfd, months, interest_saved, feasible, budget, total_min) -> str:
    if not feasible:
        return (
            f"With {budget:.0f}/month the debts don't fully clear within the modelled horizon — "
            f"the minimum payments are barely keeping up with interest. Freeing up more than the "
            f"{total_min:.0f}/month of minimums would change this. This is guidance only; you decide."
        )
    name = "highest-interest-first (avalanche)" if method == "avalanche" else "smallest-balance-first (snowball)"
    parts = [f"Using the {name} strategy at {budget:.0f}/month, you could be debt-free around {dfd} "
             f"(about {months} months)."]
    if interest_saved > 0:
        parts.append(f"That saves roughly {interest_saved:.0f} in interest versus paying only the minimums.")
    parts.append("This is guidance only — you choose what to pay.")
    return " ".join(parts)


def _llm_summary(method, dfd, months, interest_saved, budget, steps) -> Optional[str]:
    if not llm.is_available():
        return None
    facts = {
        "method": method, "debt_free_date": dfd, "months": months,
        "interest_saved_eur": round(interest_saved),
        "monthly_budget_eur": round(budget),
        "priority_debt": steps[0].creditor if steps else None,
    }
    system = (
        "You are a calm, encouraging financial coach. In 2-3 plain sentences, explain the debt "
        "payoff plan to the user: which debt to focus on first and why, the debt-free date, and "
        "the interest saved. Amounts are in euros (EUR); never use a '$' sign. End by reminding "
        'them this is guidance only and they decide. Reply JSON: {"summary": "..."}.'
    )
    out = llm.chat_json(
        system, f"Facts: {facts}",
        model=config.OLLAMA_MODEL_SKILL, timeout=config.OLLAMA_TIMEOUT_SKILL,
    )
    if isinstance(out, dict) and isinstance(out.get("summary"), str):
        return out["summary"].strip()
    return None


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def build_plan(
    *, user_id: str, monthly_budget: Optional[float] = None,
    method: PayoffMethod = "avalanche", use_llm: bool = True,
) -> Optional[RepaymentPlan]:
    obligations = domain_store.list_obligations(user_id=user_id)
    debts = _prepare(obligations, {})
    if not debts:
        return None

    budget, total_min = _default_budget(debts, monthly_budget)
    plan_sim = _simulate(debts, budget, method)
    baseline = _simulate(debts, total_min, method)  # minimums-only
    interest_saved = round(baseline["total_interest"] - plan_sim["total_interest"], 2)

    # Build ordered steps in the chosen priority order.
    if method == "avalanche":
        ordered = sorted(debts, key=lambda d: (-d["apr"], d["bal"]))
    else:
        ordered = sorted(debts, key=lambda d: (d["bal"], -d["apr"]))
    steps = [
        PlanStep(
            order=i + 1, obligation_id=d["id"], creditor=d["creditor"],
            balance=round(d["balance0"], 2), apr=d["apr"], min_payment=d["min"],
            monthly_amount=plan_sim["month1_alloc"].get(d["id"], 0.0),
            payoff_month=plan_sim["payoff_month"].get(d["id"]),
        )
        for i, d in enumerate(ordered)
    ]

    # Per-debt balance trajectories, in the same order as the steps.
    debt_series = [
        DebtSeries(
            obligation_id=d["id"], creditor=d["creditor"],
            balances=plan_sim["per_debt"].get(d["id"], []),
        )
        for d in ordered
    ]

    months = plan_sim["months"]
    dfd = _add_months(date.today(), months).isoformat() if months else None

    summary, llm_used = None, False
    if use_llm:
        summary = _llm_summary(method, dfd, months, interest_saved, budget, steps)
        llm_used = summary is not None
    if summary is None:
        summary = _template_summary(method, dfd, months, interest_saved,
                                    plan_sim["feasible"], budget, total_min)

    existing = planner_store.get_plan(user_id)
    version = (existing.get("version", 0) + 1) if existing else 1

    return RepaymentPlan(
        id=uuid.uuid4().hex, user_id=user_id, method=method, monthly_budget=budget,
        total_minimums=total_min, feasible=plan_sim["feasible"],
        months_to_debt_free=months, debt_free_date=dfd,
        total_interest=plan_sim["total_interest"], interest_saved_vs_minimums=interest_saved,
        baseline_feasible=baseline["feasible"], steps=steps,
        projection=_projection_points(plan_sim), debt_series=debt_series, summary=summary,
        llm_used=llm_used, version=version, created_at=_now(),
    )


def compare(*, user_id: str, monthly_budget: Optional[float] = None) -> Optional[StrategyComparison]:
    obligations = domain_store.list_obligations(user_id=user_id)
    debts = _prepare(obligations, {})
    if not debts:
        return None
    budget, _ = _default_budget(debts, monthly_budget)
    av = _summary_numbers(_simulate(debts, budget, "avalanche"), "avalanche")
    sn = _summary_numbers(_simulate(debts, budget, "snowball"), "snowball")

    if av.total_interest < sn.total_interest - 0.5:
        rec, reason = "avalanche", (
            f"Avalanche saves about {sn.total_interest - av.total_interest:.0f} more in interest "
            "by clearing the highest-rate debt first."
        )
    elif sn.total_interest < av.total_interest - 0.5:
        rec, reason = "snowball", "Snowball costs less interest here while also clearing small debts first."
    else:
        rec, reason = "snowball", (
            "Interest cost is about the same either way, so snowball's quick early wins "
            "can help with motivation."
        )
    return StrategyComparison(monthly_budget=budget, avalanche=av, snowball=sn,
                              recommended=rec, reason=reason)


def simulate(
    *, user_id: str, method: Optional[PayoffMethod], monthly_budget: Optional[float],
    extra_payment: float, windfall: float, windfall_month: int,
    rate_changes: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    obligations = domain_store.list_obligations(user_id=user_id)
    if not obligations:
        return None
    live = planner_store.get_plan(user_id)
    method = method or (live.get("method") if live else "avalanche")
    base_budget = monthly_budget if monthly_budget is not None else (
        live.get("monthly_budget") if live else None
    )

    debts_plain = _prepare(obligations, {})
    budget, _ = _default_budget(debts_plain, base_budget)

    overrides = {rc["obligation_id"]: float(rc["apr"]) for rc in rate_changes}
    debts_sim = _prepare(obligations, overrides)

    baseline_sim = _simulate(debts_plain, budget, method)
    sim = _simulate(
        debts_sim, budget, method,
        extra=extra_payment, windfall=windfall, windfall_month=windfall_month,
    )
    return {
        "method": method, "budget": budget,
        "baseline": _summary_numbers(baseline_sim, method),
        "simulated": _summary_numbers(sim, method),
        "baseline_sim": baseline_sim, "sim": sim,
    }
