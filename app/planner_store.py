"""Persistence for the Planner: keeps BOTH strategies per user.

File shape:  { user_id: { "active": "avalanche", "plans": { "avalanche": {...}, "snowball": {...} } } }
Saving a plan stores it under its method and marks it active, so avalanche and
snowball both persist and the user can switch without rebuilding.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional

from . import config

_lock = threading.Lock()


def _read() -> dict[str, Any]:
    if not config.PLANS_PATH.exists():
        return {}
    with config.PLANS_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(data: dict[str, Any]) -> None:
    config.PLANNER_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.PLANS_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    tmp.replace(config.PLANS_PATH)


def _norm(rec: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Accept the new {active, plans} shape, or migrate an old single-plan record."""
    if not rec:
        return None
    if "plans" in rec and isinstance(rec["plans"], dict):
        return rec
    # Legacy: the record IS a single plan -> wrap it.
    method = rec.get("method", "avalanche")
    return {"active": method, "plans": {method: rec}}


def save_plan(user_id: str, plan: dict[str, Any]) -> None:
    with _lock:
        store = _read()
        rec = _norm(store.get(user_id)) or {"active": None, "plans": {}}
        method = plan.get("method", "avalanche")
        rec["plans"][method] = plan
        rec["active"] = method
        store[user_id] = rec
        _write(store)


def get_plan(user_id: str) -> Optional[dict[str, Any]]:
    """The active (most recently built) plan."""
    rec = _norm(_read().get(user_id))
    if not rec:
        return None
    plans = rec["plans"]
    return plans.get(rec.get("active")) or (next(iter(plans.values()), None))


def get_plans(user_id: str) -> dict[str, Any]:
    """All saved strategies for the user: {method: plan}."""
    rec = _norm(_read().get(user_id))
    return rec["plans"] if rec else {}


def get_active_method(user_id: str) -> Optional[str]:
    rec = _norm(_read().get(user_id))
    return rec.get("active") if rec else None
