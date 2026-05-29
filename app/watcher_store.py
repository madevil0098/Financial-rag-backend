"""Persistence for the Watcher agent: latest RiskAssessment + AlertPreferences per user."""
from __future__ import annotations

import json
import threading
from typing import Any, Optional

from . import config

_lock = threading.Lock()


def _ensure_dir() -> None:
    config.WATCHER_DIR.mkdir(parents=True, exist_ok=True)


def _read(path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(path, data: dict[str, Any]) -> None:
    _ensure_dir()
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str)
    tmp.replace(path)


def save_assessment(user_id: str, assessment: dict[str, Any]) -> None:
    with _lock:
        store = _read(config.ASSESSMENTS_PATH)
        store[user_id] = assessment
        _write(config.ASSESSMENTS_PATH, store)


def get_assessment(user_id: str) -> Optional[dict[str, Any]]:
    return _read(config.ASSESSMENTS_PATH).get(user_id)


def get_preferences(user_id: str) -> Optional[dict[str, Any]]:
    return _read(config.PREFERENCES_PATH).get(user_id)


def save_preferences(user_id: str, prefs: dict[str, Any]) -> None:
    with _lock:
        store = _read(config.PREFERENCES_PATH)
        store[user_id] = prefs
        _write(config.PREFERENCES_PATH, store)
