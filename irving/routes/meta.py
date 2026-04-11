"""
irving/routes/meta.py
──────────────────────
Health check, frontend serving, and debug endpoints.
"""
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Security
from fastapi.responses import FileResponse
from fastapi.security.api_key import APIKeyHeader

from irving.config import (
    NOTION_TOKEN, REVIEW_QUEUE_DB_ID, CONTEXT_SNAPSHOTS_DB_ID,
    ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY,
    GOOGLE_SA_JSON, IRVING_API_KEY,
)
from irving.agents.personas import EXPERT_PERSONAS
from irving.persistence.firestore import get_firestore_client

router         = APIRouter()
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_INDEX_HTML    = os.path.join(os.path.dirname(__file__), "..", "..", "index.html")


@router.get("/health")
async def health():
    notion_ok    = bool(NOTION_TOKEN and REVIEW_QUEUE_DB_ID and CONTEXT_SNAPSHOTS_DB_ID)
    firestore_ok = bool(get_firestore_client())
    return {
        "status": "ok",
        "notion":  "connected"  if notion_ok    else "not configured",
        "drive":   "configured" if GOOGLE_SA_JSON else "not configured",
        "history": "configured" if firestore_ok  else "not configured",
        "models": {
            "claude": "ready" if ANTHROPIC_API_KEY else "no key",
            "gpt":    "ready" if OPENAI_API_KEY    else "no key",
            "gemini": "ready" if GEMINI_API_KEY     else "no key",
        },
        "expert_domains": list(EXPERT_PERSONAS.keys()),
        "auth": "enabled" if IRVING_API_KEY else "open",
        "timestamp": datetime.utcnow().isoformat(),
    }


@router.get("/")
async def serve_index():
    path = os.path.abspath(_INDEX_HTML)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Frontend not found")
    return FileResponse(path, media_type="text/html")


@router.get("/debug-notion")
async def debug_notion():
    import notion_client as _nc
    from irving.ops.queue import notion
    result = {
        "db_id": CONTEXT_SNAPSHOTS_DB_ID, "notion_ready": bool(notion), "error": None,
        "raw_count": 0, "notion_version": getattr(_nc, "__version__", "unknown"),
        "db_methods": [m for m in dir(notion.databases) if not m.startswith("_")] if notion else [],
    }
    if notion and CONTEXT_SNAPSHOTS_DB_ID:
        try:
            if hasattr(notion.databases, "query"):
                resp = notion.databases.query(
                    database_id=CONTEXT_SNAPSHOTS_DB_ID,
                    filter={"property": "Still Current?", "checkbox": {"equals": True}},
                    page_size=10,
                )
            else:
                resp = notion.databases.query_database(database_id=CONTEXT_SNAPSHOTS_DB_ID, page_size=10)
            result["raw_count"]   = len(resp.get("results", []))
            result["first_names"] = [
                p["properties"].get("Snapshot Name", {}).get("title", [{}])[0].get("plain_text", "?")
                for p in resp.get("results", [])[:3]
            ]
        except Exception as e:
            result["error"] = str(e)
    return result
