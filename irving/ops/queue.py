"""
irving/ops/queue.py
────────────────────
Notion Review Queue: normalisation and push.
"""
import logging
from typing import Dict

from fastapi import HTTPException
from notion_client import Client as NotionClient

from irving.config import NOTION_TOKEN, REVIEW_QUEUE_DB_ID
from irving.models import QueueItem

logger = logging.getLogger(__name__)

try:
    notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None
except Exception as exc:
    logger.error(f"Failed to initialise Notion client: {exc}")
    notion = None

_VALID_TYPES    = {"Task", "Project Update", "Decision", "Follow-up", "Idea",
                   "Research Request", "Admin", "Risk", "Reference"}
_VALID_SOURCES  = {"ChatGPT", "Notion", "Google Calendar", "Google Drive",
                   "Email", "Manual", "Voice Note", "Other"}
_VALID_PRIORITY = {"High", "Medium", "Low"}


def normalize_queue_item(payload: Dict) -> QueueItem:
    return QueueItem(
        item=     (payload.get("item")      or "").strip(),
        item_type=(payload.get("item_type") or "Task").strip() or "Task",
        notes=    (payload.get("notes")     or "").strip() or None,
        source="ChatGPT",
        priority= (payload.get("priority")  or "Medium").strip() or "Medium",
        source_link=(payload.get("source_link") or "").strip() or None,
    )


def push_to_review_queue(item: QueueItem) -> dict:
    if not notion or not REVIEW_QUEUE_DB_ID:
        raise HTTPException(status_code=503, detail="Notion not configured")

    properties = {
        "Item":            {"title":     [{"text": {"content": item.item}}]},
        "Item Type":       {"select":    {"name": item.item_type if item.item_type in _VALID_TYPES else "Task"}},
        "Notes / Context": {"rich_text": [{"text": {"content": item.notes or ""}}]},
        "Source":          {"select":    {"name": item.source if item.source in _VALID_SOURCES else "Manual"}},
        "Priority":        {"select":    {"name": item.priority if item.priority in _VALID_PRIORITY else "Medium"}},
        "Queue Status":    {"status":    {"name": "New"}},
        "Needs Review":    {"checkbox":  True},
    }
    if item.source_link:
        properties["Source Link"] = {"url": item.source_link}

    try:
        page = notion.pages.create(
            parent={"database_id": REVIEW_QUEUE_DB_ID},
            properties=properties,
        )
        return {"id": page["id"], "url": page["url"]}
    except Exception as e:
        logger.error(f"Error pushing to queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))
