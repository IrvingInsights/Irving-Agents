import os
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from notion_client import Client as NotionClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Irving Agents MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────────────────
NOTION_TOKEN            = os.getenv("NOTION_TOKEN")
REVIEW_QUEUE_DB_ID      = os.getenv("NOTION_REVIEW_QUEUE_DB_ID")
CONTEXT_SNAPSHOTS_DB_ID = os.getenv("NOTION_CONTEXT_SNAPSHOTS_DB_ID")
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY")
IRVING_API_KEY          = os.getenv("IRVING_API_KEY")  # optional bearer auth

notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: Optional[str] = Security(api_key_header)):
    """Enforce IRVING_API_KEY if set; otherwise allow all requests."""
    if IRVING_API_KEY and api_key != IRVING_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


# ── Models ────────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    prompt: str

class QueueItem(BaseModel):
    item: str
    item_type: Optional[str] = "Task"
    notes: Optional[str] = None
    source: Optional[str] = "Manual"
    priority: Optional[str] = "Medium"
    source_link: Optional[str] = None


# ── Notion property extractors ────────────────────────────────────────────────
def _text(prop) -> str:
    if not prop:
        return ""
    return "".join(t.get("text", {}).get("content", "") for t in prop.get("title", []))

def _rich_text(prop) -> str:
    if not prop:
        return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _date(prop) -> str:
    if not prop:
        return ""
    d = prop.get("date") or {}
    return d.get("start", "")


# ── Core logic ────────────────────────────────────────────────────────────────
def get_current_snapshots(limit: int = 3) -> list:
    """Fetch top N snapshots where Still Current? = true, sorted by Snapshot Date desc."""
    if not notion or not CONTEXT_SNAPSHOTS_DB_ID:
        return []
    try:
        response = notion.databases.query(
            database_id=CONTEXT_SNAPSHOTS_DB_ID,
            filter={"property": "Still Current?", "checkbox": {"equals": True}},
            sorts=[{"property": "Snapshot Date", "direction": "descending"}],
            page_size=limit,
        )
        snapshots = []
        for page in response.get("results", []):
            props = page["properties"]
            snapshots.append({
                "name":                   _text(props.get("Snapshot Name")),
                "snapshot_date":          _date(props.get("Snapshot Date")),
                "current_state":          _rich_text(props.get("Current State")),
                "top_3_priorities":       _rich_text(props.get("Top 3 Priorities")),
                "recent_decisions":       _rich_text(props.get("Recent Decisions")),
                "open_questions":         _rich_text(props.get("Open Questions")),
                "risks_blockers":         _rich_text(props.get("Risks / Blockers")),
                "recommended_next_moves": _rich_text(props.get("Recommended Next Moves")),
            })
        return snapshots
    except Exception as e:
        logger.error(f"Error fetching snapshots: {e}")
        return []


def build_context_block(snapshots: list) -> str:
    if not snapshots:
        return ""
    lines = ["--- CURRENT PROJECT CONTEXT (from Notion Context Snapshots) ---\n"]
    for i, s in enumerate(snapshots, 1):
        lines.append(f"[{i}] {s['name']}  |  Date: {s['snapshot_date']}")
        for key, label in [
            ("current_state",          "Current State"),
            ("top_3_priorities",       "Top Priorities"),
            ("recent_decisions",       "Recent Decisions"),
            ("open_questions",         "Open Questions"),
            ("risks_blockers",         "Risks / Blockers"),
            ("recommended_next_moves", "Next Moves"),
        ]:
            if s.get(key):
                lines.append(f"  {label}: {s[key]}")
        lines.append("")
    lines.append("---")
    return "\n".join(lines)


def push_to_review_queue(item: QueueItem) -> dict:
    if not notion or not REVIEW_QUEUE_DB_ID:
        raise HTTPException(status_code=503, detail="Notion not configured")

    valid_types    = {"Task","Project Update","Decision","Follow-up","Idea","Research Request","Admin","Risk","Reference"}
    valid_sources  = {"ChatGPT","Notion","Google Calendar","Google Drive","Email","Manual","Voice Note","Other"}
    valid_priority = {"High","Medium","Low"}

    properties = {
        "Item":            {"title":     [{"text": {"content": item.item}}]},
        "Item Type":       {"select":    {"name": item.item_type if item.item_type in valid_types else "Task"}},
        "Notes / Context": {"rich_text": [{"text": {"content": item.notes or ""}}]},
        "Source":          {"select":    {"name": item.source if item.source in valid_sources else "Manual"}},
        "Priority":        {"select":    {"name": item.priority if item.priority in valid_priority else "Medium"}},
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


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Public — no auth required."""
    notion_ok = bool(NOTION_TOKEN and REVIEW_QUEUE_DB_ID and CONTEXT_SNAPSHOTS_DB_ID)
    return {
        "status":    "ok",
        "notion":    "connected" if notion_ok else "not configured",
        "auth":      "enabled" if IRVING_API_KEY else "open",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/context")
async def get_context(api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    snapshots     = get_current_snapshots()
    context_block = build_context_block(snapshots)
    return {"snapshots": snapshots, "count": len(snapshots), "context_block": context_block}


@app.post("/run")
async def run(request: RunRequest, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    snapshots     = get_current_snapshots()
    context_block = build_context_block(snapshots)

    if not ANTHROPIC_API_KEY:
        return {
            "response": f"[echo — no Anthropic key]\n\n{context_block}\n\nPrompt: {request.prompt}",
            "context_snapshots_injected": len(snapshots),
        }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        system = (
            "You are Irving, Daniel Irving's personal AI chief of staff. "
            "You have deep context about his projects, priorities, and decisions. "
            "Be direct, smart, and concise — no fluff."
        )
        if context_block:
            system += f"\n\n{context_block}"

        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": request.prompt}],
        )
        return {
            "response":                   message.content[0].text,
            "context_snapshots_injected": len(snapshots),
        }
    except Exception as e:
        logger.error(f"Error calling Claude: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/queue")
async def queue(item: QueueItem, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    result = push_to_review_queue(item)
    return {"success": True, "notion_page": result}
