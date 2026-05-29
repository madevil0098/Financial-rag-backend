"""Persistence for normalized domain entities (Obligation / Transaction).

JSON-backed for the demo; swappable for PostgreSQL later. Writes are
replace-by-source so re-normalizing the same dataset is idempotent (it replaces
that source's prior records rather than duplicating them).
"""
from __future__ import annotations

import json
import threading
from typing import Any, Optional

from . import config

_lock = threading.Lock()


def _ensure_dir() -> None:
    config.DOMAIN_DIR.mkdir(parents=True, exist_ok=True)


def _path(entity: str):
    return config.OBLIGATIONS_PATH if entity == "obligation" else config.TRANSACTIONS_PATH


def _load(entity: str) -> list[dict[str, Any]]:
    path = _path(entity)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _save(entity: str, records: list[dict[str, Any]]) -> None:
    _ensure_dir()
    path = _path(entity)
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2, default=str)
    tmp.replace(path)


def replace_by_source(entity: str, source: str, records: list[dict[str, Any]]) -> int:
    """Drop existing records for `source`, then insert the new ones. Idempotent."""
    with _lock:
        existing = [r for r in _load(entity) if r.get("source") != source]
        existing.extend(records)
        _save(entity, existing)
    return len(records)


def list_obligations(
    user_id: Optional[str] = None,
    kind: Optional[str] = None,
    source: Optional[str] = None,
) -> list[dict[str, Any]]:
    rows = _load("obligation")
    if user_id:
        rows = [r for r in rows if r.get("user_id") == user_id]
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if source:
        rows = [r for r in rows if r.get("source") == source]
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows


def list_transactions(
    source: Optional[str] = None,
    user_id: Optional[str] = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    rows = _load("transaction")
    if user_id:
        rows = [r for r in rows if r.get("user_id") == user_id]
    if source:
        rows = [r for r in rows if r.get("source") == source]
    rows.sort(key=lambda r: (r.get("date") or ""), reverse=True)
    total = len(rows)
    window = rows[offset : offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(window),
        "transactions": window,
    }


def counts() -> dict[str, int]:
    return {
        "obligations": len(_load("obligation")),
        "transactions": len(_load("transaction")),
    }
