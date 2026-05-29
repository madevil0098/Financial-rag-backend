"""Planner agent endpoints: payoff plan, strategy comparison, what-if simulation.

Advisory only — these endpoints plan and project. None of them move money.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from .. import config, planner_store
from ..agents import planner
from ..schemas import (
    PlanRequest,
    RepaymentPlan,
    SimulationRequest,
    SimulationResult,
    StrategyComparison,
)

router = APIRouter(prefix="/planner", tags=["planner"])


@router.post("/plan", response_model=RepaymentPlan, summary="Generate & save a payoff plan")
def create_plan(req: PlanRequest = Body(default=PlanRequest())) -> RepaymentPlan:
    user_id = req.user_id or config.DEFAULT_USER_ID
    plan = planner.build_plan(
        user_id=user_id, monthly_budget=req.monthly_budget,
        method=req.method, use_llm=req.use_llm,
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="No obligations found. Normalise some first.")
    planner_store.save_plan(user_id, plan.model_dump(mode="json"))
    return plan


@router.get("/plan", response_model=RepaymentPlan, summary="Get the active saved plan")
def get_plan(user_id: str = config.DEFAULT_USER_ID) -> RepaymentPlan:
    raw = planner_store.get_plan(user_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="No plan yet. POST /planner/plan first.")
    return RepaymentPlan(**raw)


@router.get("/plans", summary="Get all saved strategies (avalanche + snowball)")
def get_plans(user_id: str = config.DEFAULT_USER_ID) -> dict:
    plans = planner_store.get_plans(user_id)
    return {
        "count": len(plans),
        "active": planner_store.get_active_method(user_id),
        "plans": plans,  # { method: RepaymentPlan }
    }


@router.post("/compare", response_model=StrategyComparison, summary="Compare avalanche vs snowball")
def compare(req: PlanRequest = Body(default=PlanRequest())) -> StrategyComparison:
    user_id = req.user_id or config.DEFAULT_USER_ID
    result = planner.compare(user_id=user_id, monthly_budget=req.monthly_budget)
    if result is None:
        raise HTTPException(status_code=404, detail="No obligations found. Normalise some first.")
    return result


@router.post("/simulate", response_model=SimulationResult, summary="What-if simulation (no commitment)")
def simulate(req: SimulationRequest = Body(default=SimulationRequest())) -> SimulationResult:
    user_id = req.user_id or config.DEFAULT_USER_ID
    out = planner.simulate(
        user_id=user_id, method=req.method, monthly_budget=req.monthly_budget,
        extra_payment=req.extra_payment, windfall=req.windfall,
        windfall_month=req.windfall_month,
        rate_changes=[rc.model_dump() for rc in req.rate_changes],
    )
    if out is None:
        raise HTTPException(status_code=404, detail="No obligations found. Normalise some first.")

    base, sim = out["baseline"], out["simulated"]
    months_sooner = (
        base.months_to_debt_free - sim.months_to_debt_free
        if base.months_to_debt_free and sim.months_to_debt_free else None
    )
    interest_diff = round(base.total_interest - sim.total_interest, 2)

    applied = False
    note = "Simulation only — your live plan is unchanged."
    if req.apply:
        plan = planner.build_plan(
            user_id=user_id, monthly_budget=out["budget"],
            method=out["method"], use_llm=req.use_llm,
        )
        if plan is not None:
            planner_store.save_plan(user_id, plan.model_dump(mode="json"))
            applied = True
            note = "Applied: saved as your live plan (budget/method only; one-off levers are not persisted)."

    return SimulationResult(
        baseline=base, simulated=sim, months_sooner=months_sooner,
        interest_difference=interest_diff,
        levers={
            "method": out["method"], "monthly_budget": out["budget"],
            "extra_payment": req.extra_payment, "windfall": req.windfall,
            "windfall_month": req.windfall_month,
            "rate_changes": [rc.model_dump() for rc in req.rate_changes],
        },
        projection=planner._projection_points(out["sim"]),
        baseline_projection=planner._projection_points(out["baseline_sim"]),
        applied=applied, note=note,
    )
