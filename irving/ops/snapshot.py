"""
irving/ops/snapshot.py
───────────────────────
Build and persist project snapshots to Notion Context Snapshots DB.
"""
import logging
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from notion_client import Client as NotionClient

from irving.agents.callers import structured_completion
from irving.agents.routing import detect_domain
from irving.config import NOTION_TOKEN, CONTEXT_SNAPSHOTS_DB_ID
from irving.context.notion import _safe_title_parts, _safe_rich_text_parts
from irving.models import SnapshotRequest

logger = logging.getLogger(__name__)

try:
    notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None
except Exception as exc:
    logger.error(f"Failed to initialise Notion client: {exc}")
    notion = None


def build_snapshot_payload(req: SnapshotRequest) -> Dict[str, str]:
    system = (
        "You convert an AI conversation into a structured project snapshot. "
        "Return ONLY JSON with keys: "
        '{"snapshot_name":"", "current_state":"", "top_3_priorities":"", "recent_decisions":"", '
        '"open_questions":"", "risks_blockers":"", "recommended_next_moves":""}. '
        "Be concise, concrete, and operational. If a section is unknown, return an empty string."
    )
    prompt = (
        f"Preferred snapshot name: {req.snapshot_name or ''}\n\n"
        f"User prompt:\n{req.prompt}\n\nAssistant response:\n{req.response}\n\n"
        f"Conversation context:\n{req.conversation or ''}"
    )
    payload       = structured_completion(system, prompt, domain="strategy")
    snapshot_name = (req.snapshot_name or payload.get("snapshot_name") or "").strip()
    if not snapshot_name:
        domain        = detect_domain(req.prompt).replace("_", " ").title()
        snapshot_name = f"{domain} Snapshot {datetime.utcnow().strftime('%Y-%m-%d')}"
    return {
        "snapshot_name":          snapshot_name[:1800],
        "current_state":          (payload.get("current_state")          or "").strip(),
        "top_3_priorities":       (payload.get("top_3_priorities")       or "").strip(),
        "recent_decisions":       (payload.get("recent_decisions")       or "").strip(),
        "open_questions":         (payload.get("open_questions")         or "").strip(),
        "risks_blockers":         (payload.get("risks_blockers")         or "").strip(),
        "recommended_next_moves": (payload.get("recommended_next_moves") or "").strip(),
    }


def _mark_existing_snapshots_inactive() -> int:
    if not notion or not CONTEXT_SNAPSHOTS_DB_ID:
        return 0
    updated = 0
    try:
        response = notion.databases.query(
            database_id=CONTEXT_SNAPSHOTS_DB_ID,
            filter={"property": "Still Current?", "checkbox": {"equals": True}},
            page_size=50,
        )
        for page in response.get("results", []):
            notion.pages.update(page_id=page["id"], properties={"Still Current?": {"checkbox": False}})
            updated += 1
    except Exception as exc:
        logger.error(f"Error marking prior snapshots inactive: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to archive prior snapshots: {exc}")
    return updated


def create_context_snapshot(snapshot: Dict[str, str], mark_previous_inactive: bool = False) -> dict:
    if not notion or not CONTEXT_SNAPSHOTS_DB_ID:
        raise HTTPException(status_code=503, detail="Context snapshots database is not configured")
    archived_count = _mark_existing_snapshots_inactive() if mark_previous_inactive else 0
    properties = {
        "Snapshot Name":          {"title":     _safe_title_parts(snapshot["snapshot_name"])},
        "Snapshot Date":          {"date":      {"start": datetime.utcnow().date().isoformat()}},
        "Current State":          {"rich_text": _safe_rich_text_parts(snapshot.get("current_state"))},
        "Top 3 Priorities":       {"rich_text": _safe_rich_text_parts(snapshot.get("top_3_priorities"))},
        "Recent Decisions":       {"rich_text": _safe_rich_text_parts(snapshot.get("recent_decisions"))},
        "Open Questions":         {"rich_text": _safe_rich_text_parts(snapshot.get("open_questions"))},
        "Risks / Blockers":       {"rich_text": _safe_rich_text_parts(snapshot.get("risks_blockers"))},
        "Recommended Next Moves": {"rich_text": _safe_rich_text_parts(snapshot.get("recommended_next_moves"))},
        "Still Current?":         {"checkbox":  True},
    }
    try:
        page = notion.pages.create(
            parent={"database_id": CONTEXT_SNAPSHOTS_DB_ID}, properties=properties,
        )
        return {"id": page["id"], "url": page["url"], "archived_count": archived_count}
    except Exception as exc:
        logger.error(f"Error creating context snapshot: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
