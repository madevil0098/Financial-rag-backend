"""Data ingestion endpoints: csv, excel, sql, json.

Each parses its source into one or more pandas DataFrames (a source may contain
several tables) and hands them to the storage layer, which normalizes every
source into one uniform dataset model. Tables ingested in a single call share a
`group_id`; each carries its own `table_name` and a `category`
(loan / bank_statement / payment_due / ... or any free-form label).
"""
from __future__ import annotations

import io
import json
from typing import Optional

import pandas as pd
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from .. import config, storage
from ..schemas import IngestResult, SQLIngestRequest

router = APIRouter(prefix="/ingest", tags=["ingest"])


def _frame_ok(df: pd.DataFrame) -> bool:
    return df is not None and df.shape[1] > 0


def _persist_tables(
    tables: dict[str, pd.DataFrame],
    *,
    source_type,
    category: Optional[str],
    base_name: str,
    original_filename: Optional[str] = None,
    extra_per_table=None,
) -> IngestResult:
    """Save a set of named tables under one group and build the IngestResult."""
    usable = {name: df for name, df in tables.items() if _frame_ok(df)}
    if not usable:
        raise HTTPException(status_code=422, detail="No non-empty tables found in source.")

    cat = category or config.DEFAULT_CATEGORY
    group_id = storage.new_group_id()
    multi = len(usable) > 1
    metas = []
    for table_name, df in usable.items():
        ds_name = f"{base_name} · {table_name}" if multi else base_name
        extra = dict(extra_per_table(table_name) if extra_per_table else {})
        metas.append(
            storage.save_dataset(
                df,
                name=ds_name,
                source_type=source_type,
                category=cat,
                group_id=group_id,
                table_name=table_name,
                original_filename=original_filename,
                extra=extra,
            )
        )
    return IngestResult(
        group_id=group_id,
        source_type=source_type,
        category=cat,
        count=len(metas),
        datasets=metas,
    )


@router.post("/csv", response_model=IngestResult, summary="Ingest a CSV file")
async def ingest_csv(
    file: UploadFile = File(..., description="CSV file to upload"),
    name: Optional[str] = Form(None),
    delimiter: Optional[str] = Form(None, description="Column delimiter (default ',')"),
    category: Optional[str] = Form(None, description="loan | bank_statement | payment_due | ..."),
) -> IngestResult:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    try:
        df = pd.read_csv(io.BytesIO(content), sep=delimiter or ",")
    except Exception as exc:  # noqa: BLE001 - surface parse errors to the client
        raise HTTPException(status_code=422, detail=f"Could not parse CSV: {exc}")
    base = name or (file.filename or "dataset")
    return _persist_tables(
        {"data": df},
        source_type="csv",
        category=category,
        base_name=base,
        original_filename=file.filename,
    )


@router.post("/excel", response_model=IngestResult, summary="Ingest an Excel file (all sheets)")
async def ingest_excel(
    file: UploadFile = File(..., description="Excel file (.xlsx/.xlsm)"),
    name: Optional[str] = Form(None),
    sheet: Optional[str] = Form(None, description="Single sheet name (default: ALL sheets)"),
    category: Optional[str] = Form(None),
) -> IngestResult:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    try:
        # sheet_name=None -> dict of every sheet; a name -> just that sheet.
        parsed = pd.read_excel(
            io.BytesIO(content),
            sheet_name=sheet if sheet else None,
            engine="openpyxl",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Could not parse Excel: {exc}")
    sheets = parsed if isinstance(parsed, dict) else {sheet or "Sheet1": parsed}
    base = name or (file.filename or "dataset")
    return _persist_tables(
        {str(s): df for s, df in sheets.items()},
        source_type="excel",
        category=category,
        base_name=base,
        original_filename=file.filename,
        extra_per_table=lambda s: {"sheet": s},
    )


@router.post("/sql", response_model=IngestResult, summary="Ingest one or many SQL tables")
def ingest_sql(payload: SQLIngestRequest) -> IngestResult:
    modes = [bool(payload.query), bool(payload.table), bool(payload.tables), payload.all_tables]
    if sum(modes) != 1:
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one of: query, table, tables, all_tables.",
        )
    try:
        from sqlalchemy import create_engine, inspect, text
    except ImportError:  # pragma: no cover
        raise HTTPException(status_code=500, detail="SQLAlchemy is not installed.")

    engine = None
    try:
        engine = create_engine(payload.connection_string)
        with engine.connect() as conn:
            tables: dict[str, pd.DataFrame] = {}
            if payload.query:
                tables["query_result"] = pd.read_sql(text(payload.query), conn)
            elif payload.table:
                tables[payload.table] = pd.read_sql_table(payload.table, conn)
            elif payload.tables:
                for t in payload.tables:
                    tables[t] = pd.read_sql_table(t, conn)
            else:  # all_tables
                names = inspect(engine).get_table_names()
                if not names:
                    raise HTTPException(status_code=422, detail="No tables found in database.")
                for t in names:
                    tables[t] = pd.read_sql_table(t, conn)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"SQL ingestion failed: {exc}")
    finally:
        if engine is not None:
            engine.dispose()

    return _persist_tables(
        tables,
        source_type="sql",
        category=payload.category,
        base_name=payload.name or "sql_dataset",
        extra_per_table=lambda t: {"source": t},
    )


def _json_to_tables(data: object) -> dict[str, pd.DataFrame]:
    """Split arbitrary JSON into named tables.

    - list of objects            -> one table "data"
    - object with array values   -> one table per array-valued key (multi-table)
    - flat object                -> one single-row table "record"
    """
    if isinstance(data, list):
        return {"data": pd.json_normalize(data)}
    if isinstance(data, dict):
        array_keys = [k for k, v in data.items() if isinstance(v, list)]
        if array_keys:
            return {str(k): pd.json_normalize(data[k]) for k in array_keys}
        return {"record": pd.json_normalize(data)}
    raise HTTPException(
        status_code=422,
        detail="JSON must be an array of objects or an object containing one or more arrays.",
    )


@router.post("/json", response_model=IngestResult, summary="Ingest JSON (file or body)")
async def ingest_json(
    request: Request,
    file: Optional[UploadFile] = File(None, description="Optional .json file"),
    name: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
) -> IngestResult:
    if file is not None:
        content = await file.read()
        original = file.filename
    else:
        content = await request.body()
        original = None
    if not content:
        raise HTTPException(
            status_code=400,
            detail="No JSON provided (upload a file or send a JSON body).",
        )
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}")

    tables = _json_to_tables(data)
    base = name or original or "json_dataset"
    return _persist_tables(
        tables,
        source_type="json",
        category=category,
        base_name=base,
        original_filename=original,
    )
