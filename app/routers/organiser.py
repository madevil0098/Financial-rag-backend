"""Organiser agent endpoints: normalize ingested datasets into canonical entities."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from .. import storage
from ..agents import organiser
from ..schemas import NormaliseRequest, NormaliseResult

router = APIRouter(prefix="/organiser", tags=["organiser"])


@router.post(
    "/normalise/{dataset_id}",
    response_model=NormaliseResult,
    summary="Normalise one ingested dataset into Obligations/Transactions",
)
def normalise(dataset_id: str, req: NormaliseRequest = Body(default=NormaliseRequest())) -> NormaliseResult:
    result = organiser.normalise_dataset(
        dataset_id,
        target=req.target,
        use_llm=req.use_llm,
        mapping_override=req.mapping_override,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return result


@router.post(
    "/normalise-group/{group_id}",
    response_model=list[NormaliseResult],
    summary="Normalise every table from one ingestion group",
)
def normalise_group(
    group_id: str, req: NormaliseRequest = Body(default=NormaliseRequest())
) -> list[NormaliseResult]:
    datasets = storage.list_datasets(group_id=group_id)
    if not datasets:
        raise HTTPException(status_code=404, detail="No datasets found for this group.")
    results = []
    for ds in datasets:
        res = organiser.normalise_dataset(
            ds.id,
            target=req.target,
            use_llm=req.use_llm,
            mapping_override=req.mapping_override,
        )
        if res is not None:
            results.append(res)
    return results
