"""
irving/routes/ops.py
─────────────────────
Operational routes: /queue, /ops/sweep, /ops/snapshot.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

from irving.config import IRVING_API_KEY
from irving.models import QueueItem, SnapshotRequest, SweepRequest
from irving.ops.queue import push_to_review_queue
from irving.ops.sweep import build_review_sweep
from irving.ops.snapshot import build_snapshot_payload, create_context_snapshot

logger         = logging.getLogger(__name__)
router         = APIRouter()
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _verify(api_key: Optional[str]) -> None:
    if IRVING_API_KEY and api_key != IRVING_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


@router.post("/queue")
async def queue(item: QueueItem, api_key: Optional[str] = Security(api_key_header)):
    _verify(api_key)
    return {"success": True, "notion_page": push_to_review_queue(item)}


@router.post("/ops/sweep")
async def sweep(req: SweepRequest, api_key: Optional[str] = Security(api_key_header)):
    _verify(api_key)
    queue_items = build_review_sweep(req)
    created     = [push_to_review_queue(item) for item in queue_items]
    return {"success": True, "queued_count": len(created),
            "queue_items": [item.dict() for item in queue_items], "notion_pages": created}


@router.post("/ops/snapshot")
async def snapshot(req: SnapshotRequest, api_key: Optional[str] = Security(api_key_header)):
    _verify(api_key)
    payload = build_snapshot_payload(req)
    created = create_context_snapshot(payload, mark_previous_inactive=bool(req.mark_previous_inactive))
    return {"success": True, "snapshot": payload, "notion_page": created}
