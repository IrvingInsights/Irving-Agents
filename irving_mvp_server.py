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
CONTEXT_SNAPSHOTS_DB_ID = os.getenv("NOTION_CONTEXT_SNAPSHOTS_DB_ID", "57887d95-300e-4f9d-802c-1283b4132e02")
ANTHROPIC_API_KEY       = os.getenv("ANTHROPIC_API_KEY")
OPENAI_API_KEY          = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY          = os.getenv("GEMINI_API_KEY")
IRVING_API_KEY          = os.getenv("IRVING_API_KEY")
GOOGLE_SA_JSON          = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
DRIVE_OUTPUT_FOLDER_ID  = os.getenv("DRIVE_OUTPUT_FOLDER_ID")

notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(api_key: Optional[str] = Security(api_key_header)):
    if IRVING_API_KEY and api_key != IRVING_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


# ── Models ────────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    prompt: str
    model: Optional[str] = "auto"
    store_to_drive: Optional[bool] = False
    drive_folder_id: Optional[str] = None

class QueueItem(BaseModel):
    item: str
    item_type: Optional[str] = "Task"
    notes: Optional[str] = None
    source: Optional[str] = "Manual"
    priority: Optional[str] = "Medium"
    source_link: Optional[str] = None


# ── Google Drive ──────────────────────────────────────────────────────────────
def _get_drive_service():
    if not GOOGLE_SA_JSON:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        sa_bytes = base64.b64decode(GOOGLE_SA_JSON)
        sa_info  = json.loads(sa_bytes.decode("utf-8"))
        creds    = service_account.Credentials.from_service_account_info(
            sa_info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        logger.error(f"Error building Drive service: {e}")
        return None


def get_drive_context(query: str = "", max_files: int = 3) -> str:
    drive = _get_drive_service()
    if not drive:
        return ""
    try:
        search_q = "mimeType='application/vnd.google-apps.document' and trashed=false"
        if query:
            safe = query[:50].replace("'", "")
            search_q += f" and fullText contains '{safe}'"
        results = drive.files().list(
            q=search_q, pageSize=max_files,
            fields="files(id, name, modifiedTime)", orderBy="modifiedTime desc"
        ).execute()
        files = results.get("files", [])
        if not files:
            return ""
        parts = ["--- GOOGLE DRIVE CONTEXT ---\n"]
        for f in files:
            try:
                raw  = drive.files().export(fileId=f["id"], mimeType="text/plain").execute()
                text = (raw.decode("utf-8") if isinstance(raw, bytes) else raw)[:2000].strip()
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


# ── Notion helpers ────────────────────────────────────────────────────────────
def _text(prop) -> str:
    if not prop: return ""
    return "".join(t.get("text", {}).get("content", "") for t in prop.get("title", []))

def _rich_text(prop) -> str:
    if not prop: return ""
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _date(prop) -> str:
    if not prop: return ""
    return (prop.get("date") or {}).get("start", "")


def get_current_snapshots(limit: int = 3) -> list:
    """Query Notion Context Snapshots DB directly via REST (bypasses notion-client version issues)."""
    if not NOTION_TOKEN or not CONTEXT_SNAPSHOTS_DB_ID:
        return []
    try:
        import urllib.request as _ur
        import json as _j
        url  = f"https://api.notion.com/v1/databases/{CONTEXT_SNAPSHOTS_DB_ID}/query"
        body = _j.dumps({
            "filter": {"property": "Still Current?", "checkbox": {"equals": True}},
            "sorts":  [{"property": "Snapshot Date", "direction": "descending"}],
            "page_size": limit,
        }).encode()
        req = _ur.Request(url, data=body, method="POST", headers={
            "Authorization":  f"Bearer {NOTION_TOKEN}",
            "Content-Type":   "application/json",
            "Notion-Version": "2022-06-28",
        })
        with _ur.urlopen(req, timeout=10) as resp:
            data = _j.loads(resp.read())
        snapshots = []
        for page in data.get("results", []):
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
        logger.error(f"Error fetching snapshots via REST: {e}")
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


# ── Expert Domain Routing ─────────────────────────────────────────────────────
#
# Instead of one generic "chief of staff" prompt, each domain activates a
# specialist persona with the right mental models, vocabulary, and priorities.
# The model choice also shifts based on domain (Claude for depth, GPT for code,
# Gemini for research/lookup).

EXPERT_PERSONAS = {

    "structural": (
        "You are a multidisciplinary expert panel: a licensed structural engineer (PE) "
        "specializing in steel and timber connection design, and an architect with deep "
        "experience in unconventional housing — earth-sheltered dwellings, A-frame structures, "
        "prefab assemblies, relocatable and off-grid buildings. You understand pintle hinge "
        "mechanisms, panel systems, envelope detailing, load paths, and parametric geometry.\n\n"
        "When answering:\n"
        "- Lead with structural logic and load paths before aesthetics\n"
        "- Give specific material specs, dimensions, and connection details where relevant\n"
        "- Flag code considerations (IBC, IRC, AISC) when applicable\n"
        "- Think through fabrication constraints and assembly sequencing\n"
        "- Use first-principles reasoning when standards do not cover the case\n"
        "- Be direct about what will and will not work structurally"
    ),

    "strategy": (
        "You are a senior strategy consultant operating at partner level — the analytical "
        "rigor of McKinsey/BCG, without the jargon. You help Daniel Irving think through "
        "consulting engagements, business development, and the Irving Insights practice.\n\n"
        "When answering:\n"
        "- Structure everything MECE — mutually exclusive, collectively exhaustive\n"
        "- Lead with the so-what before the supporting detail\n"
        "- Use frameworks where they genuinely add clarity, not decoration\n"
        "- Be direct about risks and hard truths the client may not want to hear\n"
        "- Think in leverage points and second-order effects, not just tasks\n"
        "- Always translate analysis into a recommended action or decision"
    ),

    "writing": (
        "You are an experienced developmental editor and writing coach who has worked with "
        "nonfiction authors on book proposals, chapter architecture, and voice development. "
        "You understand how ideas land on the page versus in conversation, and how to build "
        "reader momentum across long-form work.\n\n"
        "When answering:\n"
        "- Think about the reader's experience first, always\n"
        "- Diagnose structural and narrative issues before line-level fixes\n"
        "- Offer specific rewrite options, not just observations\n"
        "- Know when to expand an idea and when to cut it entirely\n"
        "- Understand platform-specific voice (Substack, LinkedIn, book vs. article vs. X)\n"
        "- Help Daniel find his argument before worrying about the prose"
    ),

    "hockey": (
        "You are an elite field hockey coach and sports performance analyst with experience "
        "at the competitive club and high school levels. You understand modern field hockey "
        "tactics, player development frameworks, drill design, game film analysis, and how "
        "to build team culture.\n\n"
        "When answering:\n"
        "- Think from the player's perspective as well as the coach's\n"
        "- Be specific about positioning, timing, touch quality, and decision-making cues\n"
        "- Link physical work to tactical understanding\n"
        "- Consider athlete psychology, motivation, and developmental stage\n"
        "- Distinguish between individual skill work and team system work\n"
        "- Design practices that are competitive and game-realistic"
    ),

    "business_ops": (
        "You are a seasoned COO and operations expert who helps founders and executives "
        "build systems, manage teams, and scale operations efficiently. You understand TBK "
        "as a business Daniel operates alongside his other commitments.\n\n"
        "When answering:\n"
        "- Prioritize leverage: what system or process change creates the most impact?\n"
        "- Think about decision rights and accountability, not just tasks\n"
        "- Be concrete about tools, workflows, owners, and timelines\n"
        "- Flag where unnecessary complexity is being introduced\n"
        "- Operate from a 90-day execution window by default\n"
        "- Distinguish between things Daniel must own versus things he should delegate"
    ),

    "health": (
        "You are a performance coach and health optimization specialist who works with "
        "high-performing professionals managing multiple demanding domains simultaneously. "
        "You understand the interaction between training, nutrition, sleep, stress load, "
        "and cognitive performance.\n\n"
        "When answering:\n"
        "- Integrate physical, mental, and recovery dimensions together\n"
        "- Be evidence-based but practical — translate research into protocols\n"
        "- Think about sustainability and consistency over short-term optimization\n"
        "- Personalize to Daniel's context: high cognitive load, multiple life domains\n"
        "- Give specific, actionable interventions — not general advice\n"
        "- Flag when something requires professional medical evaluation"
    ),

    "code": (
        "You are a senior software architect and full-stack engineer with deep experience "
        "building production Python APIs, React frontends, and cloud-deployed services. "
        "You understand clean architecture, API design, and the practical tradeoffs of "
        "real-world systems that must be maintained.\n\n"
        "When answering:\n"
        "- Write complete, working code — never pseudocode unless explicitly asked\n"
        "- Explain the architectural reasoning behind key decisions\n"
        "- Flag security, performance, and operational considerations proactively\n"
        "- Prefer simple and explicit over clever and abstract\n"
        "- Think about observability: logging, error handling, monitoring\n"
        "- Consider what happens when this code runs in production under load"
    ),

    "default": (
        "You are Irving — Daniel Irving's personal AI chief of staff. You have deep context "
        "about his work across six domains: Irving Insights (consulting), Book (writing), "
        "Field Hockey (coaching), TBK (business operations), Health, and Personal. You know "
        "his 80/20 philosophy — focus relentlessly on the signal, cut the noise.\n\n"
        "When answering:\n"
        "- Be direct and opinionated, never hedge everything\n"
        "- Prioritize ruthlessly when demands compete\n"
        "- Connect dots across domains when they're relevant\n"
        "- Ask the question behind the question\n"
        "- Always end with a clear next action or decision\n"
        "- Treat Daniel's time as the scarcest resource in the system"
    ),
}

# Domain keyword detection — ordered by specificity
_DOMAIN_SIGNALS = [
    ("structural", [
        "peakhinge", "peak hinge", "hinge", "pintle", "a-frame", "aframe",
        "structural", "truss", "beam", "load path", "load calc", "foundation",
        "building", "house", "cabin", "dwelling", "earth-shelter", "prefab",
        "panel system", "connection", "weld", "bolt", "steel section", "timber",
        "blueprint", "floor plan", "envelope", "roof line", "wall assembly",
        "fabricat", "architect", "engineer", "ibc", "irc", "aisc",
    ]),
    ("strategy", [
        "irving insights", "consulting", "client", "engagement", "strategy",
        "framework", "market analysis", "business model", "proposal", "deck",
        "stakeholder", "mece", "bcg", "mckinsey", "go-to-market", "gtm",
        "revenue model", "growth strategy", "positioning",
    ]),
    ("writing", [
        "my book", "the book", "chapter", "manuscript", "draft", "outline",
        "narrative arc", "substack", "linkedin post", "article", "essay",
        "blog post", "voice", "developmental edit", "publish", "reader",
        "content pipeline", "writing coach",
    ]),
    ("hockey", [
        "field hockey", "hockey practice", "hockey player", "drill", "hockey game",
        "tournament", "defender", "midfielder", "forward", "goalkeeper",
        "penalty corner", "short corner", "press", "trap", "hockey team",
    ]),
    ("business_ops", [
        "tbk", "t.b.k", "operations", "workflow", "process map",
        "hiring", "vendor", "contract", "invoice", "cashflow",
        "coo", "scale the business", "team management", "ops system",
    ]),
    ("health", [
        "workout", "training plan", "nutrition", "diet", "sleep quality",
        "recovery", "stress load", "energy levels", "weight loss", "lifting",
        "running plan", "supplement", "biometric", "hrv", "vo2 max",
        "health goal", "fitness",
    ]),
    ("code", [
        "python code", "javascript", "react component", "fastapi", "endpoint",
        "function", "script", "bug fix", "error trace", "deploy", "render.com",
        "github", "database query", "sql", "http request", "json schema",
        "refactor", "unit test", "docker", "git commit", "api key",
    ]),
]


def detect_domain(prompt: str) -> str:
    """Detect which of Daniel's domains this prompt belongs to."""
    pl = prompt.lower()
    for domain, signals in _DOMAIN_SIGNALS:
        if any(s in pl for s in signals):
            return domain
    return "default"


def domain_preferred_model(domain: str) -> str:
    """When model='auto', return the best model for each domain."""
    return {
        "code":         "gpt",     # GPT-4o excels at code generation
        "health":       "gemini",  # Gemini good at research / evidence synthesis
        "structural":   "claude",  # Claude for deep analytical reasoning
        "strategy":     "claude",
        "writing":      "claude",
        "hockey":       "claude",
        "business_ops": "claude",
        "default":      "claude",
    }.get(domain, "claude")


def build_expert_system(prompt: str, context_block: str = "") -> tuple:
    """Build the full system prompt using expert domain routing.
    Returns (system_prompt, domain_key).
    """
    domain  = detect_domain(prompt)
    persona = EXPERT_PERSONAS[domain]
    system  = persona
    if context_block:
        system += f"\n\n{context_block}"
    return system, domain


# ── Model callers ─────────────────────────────────────────────────────────────
def call_claude(system: str, prompt: str) -> str:
    if not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="Anthropic API key not configured")
    try:
        import anthropic
        client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=8192,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        raise RuntimeError(f"Claude error: {e}")


def call_gpt(system: str, prompt: str) -> str:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OpenAI API key not configured")
    try:
        from openai import OpenAI
        client   = OpenAI(api_key=OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=8192,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": prompt},
            ]
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"GPT API error: {e}")
        raise RuntimeError(f"GPT error: {e}")


def call_gemini(system: str, prompt: str) -> str:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=503, detail="Gemini API key not configured")
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model    = genai.GenerativeModel("gemini-1.5-pro", system_instruction=system)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logger.error(f"Gemini API error: {e}")
        raise RuntimeError(f"Gemini error: {e}")


def dispatch(model_req: str, domain: str, system: str, prompt: str) -> tuple:
    """Route to the right model. Auto-routing is now domain-aware.
    Returns (response_text, model_label).
    Falls back to GPT-4o if the primary model fails.
    """
    if model_req == "auto":
        resolved = domain_preferred_model(domain)
    else:
        resolved = model_req

    try:
        if resolved == "gpt":
            return call_gpt(system, prompt), "gpt-4o"
        elif resolved == "gemini":
            return call_gemini(system, prompt), "gemini-1.5-pro"
        else:
            return call_claude(system, prompt), "claude-opus-4-6"
    except RuntimeError as primary_err:
        logger.warning(f"Primary model ({resolved}) failed: {primary_err}. Falling back to GPT-4o.")
        if resolved != "gpt" and OPENAI_API_KEY:
            try:
                return call_gpt(system, prompt), "gpt-4o (fallback)"
            except Exception as fallback_err:
                logger.error(f"Fallback also failed: {fallback_err}")
                raise HTTPException(status_code=502, detail=f"All models failed. Primary: {primary_err}. Fallback: {fallback_err}")
        raise HTTPException(status_code=502, detail=str(primary_err))


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
        "expert_domains": list(EXPERT_PERSONAS.keys()),
        "auth":      "enabled" if IRVING_API_KEY else "open",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/debug-notion")
async def debug_notion():
    """Debug: test Notion snapshot query directly."""
    import notion_client as _nc
    result = {"db_id": CONTEXT_SNAPSHOTS_DB_ID, "notion_ready": bool(notion), "error": None, "raw_count": 0,
              "notion_version": getattr(_nc, "__version__", "unknown"),
              "db_methods": [m for m in dir(notion.databases) if not m.startswith("_")] if notion else []}
    if notion and CONTEXT_SNAPSHOTS_DB_ID:
        try:
            # Try databases.query first, fall back to _get_paginated_data
            if hasattr(notion.databases, "query"):
                resp = notion.databases.query(
                    database_id=CONTEXT_SNAPSHOTS_DB_ID,
                    filter={"property": "Still Current?", "checkbox": {"equals": True}},
                    page_size=10,
                )
            else:
                resp = notion.databases.query_database(database_id=CONTEXT_SNAPSHOTS_DB_ID, page_size=10)
            result["raw_count"] = len(resp.get("results", []))
            result["first_names"] = [p["properties"].get("Snapshot Name", {}).get("title", [{}])[0].get("plain_text", "?") for p in resp.get("results", [])[:3]]
        except Exception as e:
            result["error"] = str(e)
    return result

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

    # 1. Build context from Notion + Drive
    snapshots     = get_current_snapshots()
    drive_context = get_drive_context(query=request.prompt[:100])
    context_block = build_context_block(snapshots, drive_context)

    # 2. Expert domain routing — picks persona + preferred model
    system, domain = build_expert_system(request.prompt, context_block)

    # 3. Dispatch to model (domain-aware auto-routing)
    model_req = request.model or "auto"
    try:
        response_text, model_used = dispatch(model_req, domain, system, request.prompt)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Dispatch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Model call failed: {e}")

    # 4. Optionally save to Drive
    drive_link = None
    if request.store_to_drive:
        ts         = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        drive_link = save_to_drive(
            filename  = f"Irving_{domain}_{model_used}_{ts}",
            content   = f"Prompt:\n{request.prompt}\n\n---\n\nResponse ({model_used}, {domain} expert):\n{response_text}",
            folder_id = request.drive_folder_id,
        )

    return {
        "response":                   response_text,
        "model_used":                 model_used,
        "model_requested":            model_req,
        "domain":                     domain,
        "context_snapshots_injected": len(snapshots),
        "drive_context_injected":     bool(drive_context),
        "drive_output_link":          drive_link,
    }


@app.post("/queue")
async def queue(item: QueueItem, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    result = push_to_review_queue(item)
    return {"success": True, "notion_page": result}
