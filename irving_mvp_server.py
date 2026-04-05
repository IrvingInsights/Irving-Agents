import os
import json
import base64
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

app = FastAPI(title="Irving Agents")

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
OPENAI_API_KEY          = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY          = os.getenv("GEMINI_API_KEY")
IRVING_API_KEY          = os.getenv("IRVING_API_KEY")
GOOGLE_SA_JSON          = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # base64-encoded service account JSON
DRIVE_OUTPUT_FOLDER_ID  = os.getenv("DRIVE_OUTPUT_FOLDER_ID")       # optional Drive folder ID for outputs

notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: Optional[str] = Security(api_key_header)):
    """Enforce IRVING_API_KEY if set; otherwise allow all requests."""
    if IRVING_API_KEY and api_key != IRVING_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


# ── Models ────────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    prompt: str
    model: Optional[str] = "auto"          # auto | claude | gpt | gemini
    store_to_drive: Optional[bool] = False  # save response as Google Doc
    drive_folder_id: Optional[str] = None   # override default output folder

class QueueItem(BaseModel):
    item: str
    item_type: Optional[str] = "Task"
    notes: Optional[str] = None
    source: Optional[str] = "Manual"
    priority: Optional[str] = "Medium"
    source_link: Optional[str] = None


# ── Google Drive ──────────────────────────────────────────────────────────────
def _get_drive_service():
    """Build a Google Drive API service using a base64-encoded service account JSON."""
    if not GOOGLE_SA_JSON:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        sa_bytes = base64.b64decode(GOOGLE_SA_JSON)
        sa_info  = json.loads(sa_bytes.decode("utf-8"))
        creds    = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Error building Drive service: {e}")
        return None


def get_drive_context(query: str = "", max_files: int = 3) -> str:
    """Search Drive for recent relevant Google Docs and return their text as context."""
    drive = _get_drive_service()
    if not drive:
        return ""
    try:
        search_q = "mimeType='application/vnd.google-apps.document' and trashed=false"
        if query:
            safe = query[:50].replace("'", "")
            search_q += f" and fullText contains '{safe}'"

        results = drive.files().list(
            q=search_q,
            pageSize=max_files,
            fields="files(id, name, modifiedTime)",
            orderBy="modifiedTime desc"
        ).execute()

        files = results.get("files", [])
        if not files:
            return ""

        parts = ["--- GOOGLE DRIVE CONTEXT ---\n"]
        for f in files:
            try:
                raw   = drive.files().export(fileId=f["id"], mimeType="text/plain").execute()
                text  = (raw.decode("utf-8") if isinstance(raw, bytes) else raw)[:2000].strip()
                if text:
                    parts.append(f"[{f['name']}] (modified: {f['modifiedTime'][:10]})\n{text}\n")
            except Exception as ex:
                logger.warning(f"Could not export {f['name']}: {ex}")

        parts.append("---")
        return "\n".join(parts) if len(parts) > 2 else ""
    except Exception as e:
        logger.error(f"Error fetching Drive context: {e}")
        return ""


def save_to_drive(filename: str, content: str, folder_id: str = None) -> Optional[str]:
    """Store text as a new Google Doc in Drive. Returns webViewLink or None."""
    drive = _get_drive_service()
    if not drive:
        return None
    try:
        from googleapiclient.http import MediaInMemoryUpload

        target = folder_id or DRIVE_OUTPUT_FOLDER_ID
        meta   = {"name": filename, "mimeType": "application/vnd.google-apps.document"}
        if target:
            meta["parents"] = [target]

        media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain", resumable=False)
        file  = drive.files().create(body=meta, media_body=media, fields="id,webViewLink").execute()
        return file.get("webViewLink")
    except Exception as e:
        logger.error(f"Error saving to Drive: {e}")
        return None


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


# ── Notion context ────────────────────────────────────────────────────────────
def get_current_snapshots(limit: int = 3) -> list:
    """Fetch top N context snapshots where Still Current? = true."""
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


def build_context_block(snapshots: list, drive_context: str = "") -> str:
    lines = []

    if snapshots:
        lines.append("--- CURRENT PROJECT CONTEXT (Notion Snapshots) ---\n")
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
        lines.append("---\n")

    if drive_context:
        lines.append(drive_context)

    return "\n".join(lines)


# ── Multi-model routing ───────────────────────────────────────────────────────
def auto_route(prompt: str) -> str:
    """Heuristic routing: pick the best model for the task type."""
    pl = prompt.lower()

    # Long-form writing, strategy, analysis -> Claude
    if any(k in pl for k in ["analyze", "strategy", "write", "draft", "plan", "review",
                               "summarize", "evaluate", "critique", "advise", "recommend"]):
        return "claude"

    # Code, APIs, structured output -> GPT
    if any(k in pl for k in ["code", "function", "script", "json", "api", "debug",
                               "fix", "implement", "build", "refactor", "class", "sql"]):
        return "gpt"

    # Research, comparisons, factual lookups -> Gemini
    if any(k in pl for k in ["research", "find", "search", "compare", "list",
                               "what is", "explain", "define", "who is", "when did"]):
        return "gemini"

    # Default: Claude
    return "claude"


def call_claude(system: str, prompt: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")
    import anthropic
    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def call_gpt(system: str, prompt: str) -> str:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI API key not configured")
    from openai import OpenAI
    client   = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4096,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ]
    )
    return response.choices[0].message.content


def call_gemini(system: str, prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Gemini API key not configured")
    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model    = genai.GenerativeModel("gemini-1.5-pro", system_instruction=system)
    response = model.generate_content(prompt)
    return response.text


def dispatch(model_req: str, system: str, prompt: str) -> tuple:
    """Route to the right model. Returns (response_text, model_label)."""
    resolved = model_req if model_req != "auto" else auto_route(prompt)

    if resolved == "gpt":
        return call_gpt(system, prompt), "gpt-4o"
    elif resolved == "gemini":
        return call_gemini(system, prompt), "gemini-1.5-pro"
    else:
        return call_claude(system, prompt), "claude-opus-4-6"


# ── Notion Review Queue ───────────────────────────────────────────────────────
def push_to_review_queue(item: QueueItem) -> dict:
    if not notion or not REVIEW_QUEUE_DB_ID:
        raise HTTPException(status_code=503, detail="Notion not configured")

    valid_types    = {"Task","Project Update","Decision","Follow-up","Idea",
                      "Research Request","Admin","Risk","Reference"}
    valid_sources  = {"ChatGPT","Notion","Google Calendar","Google Drive",
                      "Email","Manual","Voice Note","Other"}
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
    """Public -- no auth required."""
    notion_ok = bool(NOTION_TOKEN and REVIEW_QUEUE_DB_ID and CONTEXT_SNAPSHOTS_DB_ID)
    return {
        "status": "ok",
        "notion": "connected" if notion_ok else "not configured",
        "drive":  "configured" if GOOGLE_SA_JSON else "not configured",
        "models": {
            "claude": "ready" if ANTHROPIC_API_KEY else "no key",
            "gpt":    "ready" if OPENAI_API_KEY    else "no key",
            "gemini": "ready" if GEMINI_API_KEY     else "no key",
        },
        "auth":      "enabled" if IRVING_API_KEY else "open",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/context")
async def get_context(api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    snapshots     = get_current_snapshots()
    drive_context = get_drive_context()
    context_block = build_context_block(snapshots, drive_context)
    return {
        "snapshots":      snapshots,
        "snapshot_count": len(snapshots),
        "drive_context":  bool(drive_context),
        "context_block":  context_block,
    }


@app.post("/run")
async def run(request: RunRequest, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)

    # Build context from Notion + Drive
    snapshots     = get_current_snapshots()
    drive_context = get_drive_context(query=request.prompt[:100])
    context_block = build_context_block(snapshots, drive_context)

    system = (
        "You are Irving, Daniel Irving's personal AI chief of staff. "
        "You have deep context about his projects, priorities, and decisions. "
        "Be direct, smart, and concise -- no fluff."
    )
    if context_block:
        system += f"\n\n{context_block}"

    # Route and call
    model_req                 = request.model or "auto"
    response_text, model_used = dispatch(model_req, system, request.prompt)

    # Optionally save output to Drive
    drive_link = None
    if request.store_to_drive:
        ts         = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        drive_link = save_to_drive(
            filename  = f"Irving_{model_used}_{ts}",
            content   = f"Prompt:\n{request.prompt}\n\n---\n\nResponse ({model_used}):\n{response_text}",
            folder_id = request.drive_folder_id,
        )

    return {
        "response":                   response_text,
        "model_used":                 model_used,
        "model_requested":            model_req,
        "context_snapshots_injected": len(snapshots),
        "drive_context_injected":     bool(drive_context),
        "drive_output_link":          drive_link,
    }


@app.post("/queue")
async def queue(item: QueueItem, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    result = push_to_review_queue(item)
    return {"success": True, "notion_page": result}
