import os
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
REVIEW_QUEUE_DB_ID = os.getenv("NOTION_REVIEW_QUEUE_DB_ID")
CONTEXT_SNAPSHOTS_DB_ID = os.getenv("NOTION_CONTEXT_SNAPSHOTS_DB_ID")

notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None


class RunRequest(BaseModel):
    prompt: str


class QueueItem(BaseModel):
    item: str
    item_type: Optional[str] = "Task"
    notes: Optional[str] = None
    source: Optional[str] = "Manual"
    priority: Optional[str] = "Medium"


def get_current_snapshots(limit: int = 3):
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
                "name": _text(props.get("Snapshot Name")),
                "snapshot_date": _date(props.get("Snapshot Date")),
                "current_state": _rich_text(props.get("Current State")),
                "top_3_priorities": _rich_text(props.get("Top 3 Priorities")),
                "recent_decisions": _rich_text(props.get("Recent Decisions")),
                "open_questions": _rich_text(props.get("Open Questions")),
                "risks_blockers": _rich_text(props.get("Risks / Blockers")),
                "recommended_next_moves": _rich_text(props.get("Recommended Next Moves")),
            })
        return snapshots
    except Exception as e:
        logger.error(f"Error fetching snapshots: {e}")
        return []


def build_context_block(snapshots):
    if not snapshots:
        return ""
    lines = ["--- CURRENT PROJECT CONTEXT (from Notion Context Snapshots) ---\n"]
    for i, s in enumerate(snapshots, 1):
        lines.append(f"[{i}] {s['name']} | Date: {s['snapshot_date']}")
        for key, label in [("current_state","Current State"),("top_3_priorities","Top Priorities"),
                           ("recent_decisions","Recent Decisions"),("open_questions","Open Questions"),
                           ("risks_blockers","Risks/Blockers"),("recommended_next_moves","Next Moves")]:
            if s.get(key):
                lines.append(f"  {label}: {s[key]}")
        lines.append("")
    lines.append("---")
    return "\n".join(lines)


def push_to_review_queue(item: QueueItem):
    if not notion or not REVIEW_QUEUE_DB_ID:
        raise HTTPException(status_code=503, detail="Notion not configured")
    try:
        page = notion.pages.create(
            parent={"database_id": REVIEW_QUEUE_DB_ID},
            properties={
                "Item": {"title": [{"text": {"content": item.item}}]},
                "Item Type": {"select": {"name": item.item_type}},
                "Notes / Context": {"rich_text": [{"text": {"content": item.notes or ""}}]},
                "Source": {"select": {"name": item.source}},
                "Priority": {"select": {"name": item.priority}},
                "Queue Status": {"status": {"name": "New"}},
                "Needs Review": {"checkbox": True},
            },
        )
        return {"id": page["id"], "url": page["url"]}
    except Exception as e:
        logger.error(f"Error pushing to queue: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _text(prop):
    if not prop: return ""
    titles = prop.get("title", [])
    return titles[0].get("text", {}).get("content", "") if titles else ""

def _rich_text(prop):
    if not prop: return ""
    return " ".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _date(prop):
    if not prop: return ""
    d = prop.get("date", {})
    return d.get("start", "") if d else ""


@app.get("/health")
async def health():
    notion_ok = bool(NOTION_TOKEN and REVIEW_QUEUE_DB_ID and CONTEXT_SNAPSHOTS_DB_ID)
    return {"status": "ok", "notion": "connected" if notion_ok else "not configured", "timestamp": datetime.utcnow().isoformat()}


@app.get("/context")
async def get_context():
    snapshots = get_current_snapshots()
    return {"snapshots": snapshots, "count": len(snapshots)}


@app.post("/run")
async def run(request: RunRequest):
    snapshots = get_current_snapshots()
    context_block = build_context_block(snapshots)
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if not anthropic_key:
        return {"response": f"[echo mode]\n\n{context_block}\n\nPrompt: {request.prompt}", "context_snapshots_injected": len(snapshots)}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_key)
        system = f"You are Irving, Daniel's personal AI assistant.\n\n{context_block}".strip()
        message = client.messages.create(model="claude-opus-4-6", max_tokens=4096, system=system,
                                          messages=[{"role": "user", "content": request.prompt}])
        return {"response": message.content[0].text, "context_snapshots_injected": len(snapshots)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/queue")
async def queue(item: QueueItem):
    result = push_to_review_queue(item)
    return {"success": True, "notion_page": result}