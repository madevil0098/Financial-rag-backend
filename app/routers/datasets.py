"""Dataset management endpoints: list, get, preview, download, delete."""
from __future__ import annotations

import io

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from .. import config, storage
from ..schemas import DatasetList, DatasetMeta, DeleteResponse, PreviewResponse

router = APIRouter(prefix="/datasets", tags=["datasets"])


@router.get("", response_model=DatasetList, summary="List datasets (optionally filtered)")
def list_datasets(
    category: Optional[str] = Query(None, description="Filter by category"),
    group_id: Optional[str] = Query(None, description="Filter by ingestion group"),
) -> DatasetList:
    metas = storage.list_datasets(category=category, group_id=group_id)
    return DatasetList(count=len(metas), datasets=metas)


@router.get("/{dataset_id}", response_model=DatasetMeta, summary="Get dataset metadata")
def get_dataset(dataset_id: str) -> DatasetMeta:
    meta = storage.get_meta(dataset_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return meta


@router.get(
    "/{dataset_id}/preview",
    response_model=PreviewResponse,
    summary="Preview dataset rows (paginated)",
)
def preview_dataset(
    dataset_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=config.MAX_PREVIEW_LIMIT),
) -> PreviewResponse:
    result = storage.preview_rows(dataset_id, offset=offset, limit=limit)
    if result is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return PreviewResponse(**result)


@router.get("/{dataset_id}/download", summary="Download dataset as CSV")
def download_dataset(dataset_id: str) -> StreamingResponse:
    meta = storage.get_meta(dataset_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    data = storage.to_csv_bytes(dataset_id)
    if data is None:
        raise HTTPException(status_code=404, detail="Dataset file missing.")
    filename = f"{meta.name or dataset_id}.csv".replace("/", "_").replace("\\", "_")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete(
    "/{dataset_id}", response_model=DeleteResponse, summary="Delete a dataset"
)
def delete_dataset(dataset_id: str) -> DeleteResponse:
    ok = storage.delete_dataset(dataset_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Dataset not found.")
    return DeleteResponse(deleted=True, id=dataset_id)
