"""Watcher agent endpoints: cash-flow risk assessment, alerts, preferences."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from .. import config, watcher_store
from ..agents import watcher
from ..schemas import (
    Alert,
    AlertList,
    AlertPreferences,
    AssessRequest,
    RiskAssessment,
)

router = APIRouter(tags=["watcher"])


def _load_prefs(user_id: str) -> AlertPreferences:
    raw = watcher_store.get_preferences(user_id)
    return AlertPreferences(**raw) if raw else AlertPreferences(user_id=user_id)


@router.post("/watcher/assess", response_model=RiskAssessment, summary="Run a cash-flow risk assessment")
def assess(req: AssessRequest = Body(default=AssessRequest())) -> RiskAssessment:
    user_id = req.user_id or config.DEFAULT_USER_ID
    prefs = _load_prefs(user_id)
    assessment = watcher.assess(
        user_id=user_id,
        prefs=prefs,
        current_balance=req.current_balance,
        horizon_days=req.horizon_days,
        use_llm=req.use_llm,
    )
    watcher_store.save_assessment(user_id, assessment.model_dump(mode="json"))
    return assessment


@router.get("/watcher/assessment", response_model=RiskAssessment, summary="Get the latest assessment")
def latest_assessment(user_id: str = config.DEFAULT_USER_ID) -> RiskAssessment:
    raw = watcher_store.get_assessment(user_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="No assessment yet. POST /watcher/assess first.")
    return RiskAssessment(**raw)


@router.get("/alerts", response_model=AlertList, summary="List alerts from the latest assessment")
def list_alerts(user_id: str = config.DEFAULT_USER_ID) -> AlertList:
    raw = watcher_store.get_assessment(user_id)
    if raw is None:
        return AlertList(count=0, generated_at=None, alerts=[])
    alerts = [Alert(**a) for a in raw.get("alerts", [])]
    return AlertList(count=len(alerts), generated_at=raw.get("created_at"), alerts=alerts)


@router.get("/watcher/preferences", response_model=AlertPreferences, summary="Get alert preferences")
def get_preferences(user_id: str = config.DEFAULT_USER_ID) -> AlertPreferences:
    return _load_prefs(user_id)


@router.put("/watcher/preferences", response_model=AlertPreferences, summary="Update alert preferences")
def update_preferences(prefs: AlertPreferences) -> AlertPreferences:
    if prefs.sensitivity not in config.SENSITIVITY_MULTIPLIERS:
        raise HTTPException(status_code=400, detail="sensitivity must be low, medium or high.")
    watcher_store.save_preferences(prefs.user_id, prefs.model_dump(mode="json"))
    return prefs
