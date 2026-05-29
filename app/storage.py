"""Dataset storage: normalized tables as parquet on disk + a JSON metadata registry.

Every ingestion source (csv/excel/sql/json) lands here as a pandas DataFrame and
is persisted identically, so the rest of the app sees one uniform "dataset"
regardless of where the data came from. The parquet files + registry are trivially
swappable for PostgreSQL later without changing the API contract.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from . import config
from .schemas import ColumnSchema, DatasetMeta, SourceType

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_dirs() -> None:
    config.DATASETS_DIR.mkdir(parents=True, exist_ok=True)


def _parquet_path(dataset_id: str):
    return config.DATASETS_DIR / f"{dataset_id}.parquet"


def _friendly_dtype(series: pd.Series) -> str:
    dt = series.dtype
    if pd.api.types.is_bool_dtype(dt):
        return "boolean"
    if pd.api.types.is_integer_dtype(dt):
        return "integer"
    if pd.api.types.is_float_dtype(dt):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(dt):
        return "datetime"
    if pd.api.types.is_string_dtype(dt) or dt == object:
        return "string"
    return "other"


def _infer_columns(df: pd.DataFrame) -> list[ColumnSchema]:
    return [ColumnSchema(name=str(c), dtype=_friendly_dtype(df[c])) for c in df.columns]


# ---------------------------------------------------------------------------
# Registry (metadata index) helpers
# ---------------------------------------------------------------------------
def _read_registry() -> dict[str, dict[str, Any]]:
    if not config.REGISTRY_PATH.exists():
        return {}
    with config.REGISTRY_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_registry(reg: dict[str, dict[str, Any]]) -> None:
    _ensure_dirs()
    tmp = config.REGISTRY_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(reg, fh, indent=2, default=str)
    tmp.replace(config.REGISTRY_PATH)


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Stringify column names and de-duplicate so parquet/JSON round-trips cleanly."""
    df = df.copy()
    seen: dict[str, int] = {}
    new_cols = []
    for col in df.columns:
        name = str(col).strip() or "column"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        new_cols.append(name)
    df.columns = new_cols
    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def new_group_id() -> str:
    return uuid.uuid4().hex


def save_dataset(
    df: pd.DataFrame,
    *,
    name: str,
    source_type: SourceType,
    category: str = config.DEFAULT_CATEGORY,
    group_id: Optional[str] = None,
    table_name: Optional[str] = None,
    original_filename: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> DatasetMeta:
    """Persist a DataFrame as a new dataset and register its metadata."""
    _ensure_dirs()
    df = _clean_columns(df)

    dataset_id = uuid.uuid4().hex
    path = _parquet_path(dataset_id)
    df.to_parquet(path, index=False)

    meta = DatasetMeta(
        id=dataset_id,
        name=name,
        source_type=source_type,
        category=category or config.DEFAULT_CATEGORY,
        group_id=group_id,
        table_name=table_name,
        original_filename=original_filename,
        row_count=int(len(df)),
        column_count=int(df.shape[1]),
        columns=_infer_columns(df),
        size_bytes=int(path.stat().st_size),
        created_at=_now(),
        extra=extra or {},
    )

    with _lock:
        reg = _read_registry()
        reg[dataset_id] = json.loads(meta.model_dump_json())
        _write_registry(reg)

    return meta


def list_datasets(
    category: Optional[str] = None, group_id: Optional[str] = None
) -> list[DatasetMeta]:
    reg = _read_registry()
    metas = [DatasetMeta(**v) for v in reg.values()]
    if category:
        metas = [m for m in metas if m.category == category]
    if group_id:
        metas = [m for m in metas if m.group_id == group_id]
    metas.sort(key=lambda m: m.created_at, reverse=True)
    return metas


def get_meta(dataset_id: str) -> Optional[DatasetMeta]:
    reg = _read_registry()
    raw = reg.get(dataset_id)
    return DatasetMeta(**raw) if raw else None


def update_category(dataset_id: str, category: str) -> bool:
    """Relabel a dataset's category (used by the Supervisor after auto-classifying)."""
    with _lock:
        reg = _read_registry()
        if dataset_id not in reg:
            return False
        reg[dataset_id]["category"] = category
        _write_registry(reg)
    return True


def load_dataframe(dataset_id: str) -> Optional[pd.DataFrame]:
    path = _parquet_path(dataset_id)
    if not path.exists():
        return None
    return pd.read_parquet(path)


def delete_dataset(dataset_id: str) -> bool:
    with _lock:
        reg = _read_registry()
        if dataset_id not in reg:
            return False
        del reg[dataset_id]
        _write_registry(reg)
    path = _parquet_path(dataset_id)
    if path.exists():
        path.unlink()
    return True


def preview_rows(dataset_id: str, offset: int, limit: int) -> Optional[dict[str, Any]]:
    df = load_dataframe(dataset_id)
    if df is None:
        return None
    total = int(len(df))
    window = df.iloc[offset : offset + limit]
    # JSON-safe records: NaN/NaT -> None, timestamps -> ISO strings.
    safe = window.where(pd.notna(window), None)
    rows: list[dict[str, Any]] = []
    for record in safe.to_dict(orient="records"):
        clean = {}
        for k, v in record.items():
            if isinstance(v, pd.Timestamp):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        rows.append(clean)
    return {
        "id": dataset_id,
        "total_rows": total,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "columns": [str(c) for c in df.columns],
        "rows": rows,
    }


def to_csv_bytes(dataset_id: str) -> Optional[bytes]:
    df = load_dataframe(dataset_id)
    if df is None:
        return None
    return df.to_csv(index=False).encode("utf-8")
