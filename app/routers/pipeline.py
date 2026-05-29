"""One-input pipeline — the user provides data, the Supervisor manages the rest.

A single call ingests the file (type auto-detected), normalises every table,
auto-labels categories, and runs the Planner + Watcher. Advisory only.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import config, domain_store
from ..agents import supervisor
from ..schemas import PipelineResult, SQLIngestRequest
from .ingest import ingest_csv, ingest_excel, ingest_json, ingest_sql

router = APIRouter(prefix="/pipeline", tags=["pipeline"])


def _detect(filename: str) -> str:
    n = (filename or "").lower()
    if n.endswith(".csv") or n.endswith(".tsv") or n.endswith(".txt"):
        return "csv"
    if n.endswith(".xlsx") or n.endswith(".xls") or n.endswith(".xlsm"):
        return "excel"
    if n.endswith(".json"):
        return "json"
    return ""


def _finish(group_id, source_type, user_id, use_llm, monthly_budget, current_balance) -> PipelineResult:
    steps = [f"Ingested data ({source_type})."]
    tables = supervisor.process_group(group_id, use_llm=False)  # heuristic mapping = fast + reliable
    steps.append(f"Organiser normalised {len(tables)} table(s) and classified them.")

    agents = supervisor.run_agents(
        user_id=user_id, use_llm=use_llm,
        monthly_budget=monthly_budget, current_balance=current_balance,
    )
    steps.extend(agents["steps"])

    return PipelineResult(
        group_id=group_id, source_type=source_type, tables=tables,
        obligations_count=len(domain_store.list_obligations(user_id=user_id)),
        transactions_count=domain_store.list_transactions(user_id=user_id, limit=1)["total"],
        plan=agents["plan"], assessment=agents["assessment"], steps=steps,
    )


@router.post("/process", response_model=PipelineResult, summary="Drop in a file — we handle the rest")
async def process(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    use_llm: bool = Form(True),
    monthly_budget: Optional[float] = Form(None),
    current_balance: Optional[float] = Form(None),
) -> PipelineResult:
    kind = _detect(file.filename or "")
    if not kind:
        raise HTTPException(status_code=422, detail="Unsupported file. Use CSV, Excel (.xlsx) or JSON.")
    # category=None -> Supervisor auto-classifies after normalising.
    if kind == "csv":
        res = await ingest_csv(file=file, name=name, delimiter=None, category=None)
    elif kind == "excel":
        res = await ingest_excel(file=file, name=name, sheet=None, category=None)
    else:
        res = await ingest_json(file=file, name=name, category=None)

    return _finish(
        res.group_id, res.source_type, config.DEFAULT_USER_ID,
        use_llm, monthly_budget, current_balance,
    )


@router.post("/process-sql", response_model=PipelineResult, summary="Connect a DB — we handle the rest")
def process_sql(
    payload: SQLIngestRequest,
    use_llm: bool = True,
    monthly_budget: Optional[float] = None,
    current_balance: Optional[float] = None,
) -> PipelineResult:
    res = ingest_sql(payload)
    return _finish(
        res.group_id, res.source_type, config.DEFAULT_USER_ID,
        use_llm, monthly_budget, current_balance,
    )
