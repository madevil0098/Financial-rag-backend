"""Read endpoints for normalized domain entities (Obligations / Transactions)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Query

from .. import config, domain_store
from ..schemas import ObligationList, TransactionList

router = APIRouter(tags=["domain"])


@router.get("/obligations", response_model=ObligationList, summary="List normalized obligations")
def list_obligations(
    user_id: Optional[str] = Query(None),
    kind: Optional[str] = Query(None, description="credit_card | loan | bnpl | overdraft | ..."),
    source: Optional[str] = Query(None, description="Filter by source dataset id"),
) -> ObligationList:
    rows = domain_store.list_obligations(user_id=user_id, kind=kind, source=source)
    return ObligationList(count=len(rows), obligations=rows)


@router.get("/transactions", response_model=TransactionList, summary="List normalized transactions")
def list_transactions(
    source: Optional[str] = Query(None, description="Filter by source dataset id"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=config.MAX_PREVIEW_LIMIT),
) -> TransactionList:
    result = domain_store.list_transactions(source=source, offset=offset, limit=limit)
    return TransactionList(**result)
