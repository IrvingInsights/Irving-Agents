import os
import json
import base64
import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

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
DEFAULT_CONTEXT_SNAPSHOTS_DB_ID = "57887d95-300e-4f9d-802c-1283b4132e02"
NOTION_ID_RE = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _normalize_notion_id(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    match = NOTION_ID_RE.search(value)
    if not match:
        return value.strip()
    token = match.group(0).replace("-", "").lower()
    return f"{token[:8]}-{token[8:12]}-{token[12:16]}-{token[16:20]}-{token[20:32]}"


def _load_google_service_account_info(raw_value: Optional[str]) -> Optional[dict]:
    if not raw_value:
        return None

    candidates = [raw_value.strip()]
    try:
        decoded = base64.b64decode(raw_value).decode("utf-8")
        candidates.insert(0, decoded.strip())
    except Exception:
        pass

    for candidate in candidates:
        if not candidate:
            continue
        try:
            info = json.loads(candidate)
            if isinstance(info, dict):
                return info
        except json.JSONDecodeError:
            continue

    logger.error("GOOGLE_SERVICE_ACCOUNT_JSON is set but is neither valid JSON nor valid base64-encoded JSON")
    return None


NOTION_TOKEN            = _env("NOTION_TOKEN")
REVIEW_QUEUE_DB_ID      = _normalize_notion_id(_env("NOTION_REVIEW_QUEUE_DB_ID"))
CONTEXT_SNAPSHOTS_DB_ID = _normalize_notion_id(_env("NOTION_CONTEXT_SNAPSHOTS_DB_ID", DEFAULT_CONTEXT_SNAPSHOTS_DB_ID))
ANTHROPIC_API_KEY       = _env("ANTHROPIC_API_KEY")
OPENAI_API_KEY          = _env("OPENAI_API_KEY")
GEMINI_API_KEY          = _env("GEMINI_API_KEY")
IRVING_API_KEY          = _env("IRVING_API_KEY")
GOOGLE_SA_JSON          = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SA_INFO          = _load_google_service_account_info(GOOGLE_SA_JSON)
DRIVE_OUTPUT_FOLDER_ID  = _env("DRIVE_OUTPUT_FOLDER_ID")
HISTORY_DB_PATH         = Path(_env("IRVING_HISTORY_DB_PATH", "data/irving_history.db")).expanduser()

try:
    notion = NotionClient(auth=NOTION_TOKEN) if NOTION_TOKEN else None
except Exception as exc:
    logger.error(f"Failed to initialize Notion client: {exc}")
    notion = None

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_history_lock = threading.Lock()


def _history_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(HISTORY_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_history_db() -> None:
    HISTORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _history_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversation_state (
                user_id TEXT PRIMARY KEY,
                current_conv_id TEXT,
                conversation_memory TEXT NOT NULL,
                conversations TEXT NOT NULL,
                last_updated TEXT NOT NULL
            )
            """
        )
        conn.commit()


_init_history_db()


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


class SweepRequest(BaseModel):
    prompt: str
    response: str
    conversation: Optional[str] = None
    max_items: Optional[int] = 5


class SnapshotRequest(BaseModel):
    prompt: str
    response: str
    conversation: Optional[str] = None
    snapshot_name: Optional[str] = None
    mark_previous_inactive: Optional[bool] = False


class HistoryStateRequest(BaseModel):
    user_id: str
    current_conv_id: Optional[str] = None
    conversation_memory: List[Dict[str, Any]] = []
    conversations: List[Dict[str, Any]] = []


# ── Google Drive ──────────────────────────────────────────────────────────────
def _get_drive_service():
    if not GOOGLE_SA_INFO:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        creds    = service_account.Credentials.from_service_account_info(
            GOOGLE_SA_INFO, scopes=["https://www.googleapis.com/auth/drive"]
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


def _safe_title_parts(value: str) -> list:
    text = (value or "").strip()[:1800]
    return [{"text": {"content": text}}] if text else [{"text": {"content": "Untitled"}}]


def _safe_rich_text_parts(value: Optional[str], chunk_size: int = 1800) -> list:
    text = (value or "").strip()
    if not text:
        return []
    return [{"text": {"content": text[i:i + chunk_size]}} for i in range(0, len(text), chunk_size)]


def _extract_json_object(raw: str) -> Dict[str, Any]:
    if not raw:
        raise ValueError("Empty model output")

    candidates = []
    stripped = raw.strip()
    candidates.append(stripped)

    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw, re.IGNORECASE)
    if fence_match:
        candidates.append(fence_match.group(1).strip())

    brace_match = re.search(r"\{[\s\S]*\}", raw)
    if brace_match:
        candidates.append(brace_match.group(0).strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    raise ValueError(f"Could not parse JSON object from model output: {raw[:300]}")


def _first_available_model(*preferred: str) -> Optional[str]:
    for model_name in preferred:
        if model_name == "claude" and ANTHROPIC_API_KEY:
            return "claude"
        if model_name == "gpt" and OPENAI_API_KEY:
            return "gpt"
        if model_name == "gemini" and GEMINI_API_KEY:
            return "gemini"
    return None


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
    "cad": (
        "You are an expert AutoCAD drafter, AutoLISP programmer, and computational design specialist. "
        "You help Daniel Irving translate natural language design intent into precise AutoCAD commands, "
        "scripts, and parametric routines - primarily for the PeakHinge A-frame dwelling project and "
        "related structural/architectural work, but also for any technical drawing need.\n\n"
        "Your core capabilities:\n"
        "1. AutoCAD Script Files (.scr) - plain-text command sequences loadable directly via AutoCAD's "
        "SCRIPT command or drag-and-drop\n"
        "2. AutoLISP Routines (.lsp) - parametric scripts for formula-driven or repetitive geometry; "
        "loaded via (load \"filename.lsp\") or the APPLOAD dialog\n"
        "3. DXF content - structured geometry for import into AutoCAD, FreeCAD, or any CAD platform\n"
        "4. Natural language to command translation - convert plain English descriptions into exact "
        "AutoCAD command sequences\n\n"
        "Output format rules (ALWAYS follow these):\n"
        "- Wrap AutoCAD script content in ```autocad code blocks\n"
        "- Wrap AutoLISP code in ```autolisp code blocks\n"
        "- Wrap DXF content in ```dxf code blocks\n"
        "- Always explain what the script draws and exactly how to run it in AutoCAD\n"
        "- Lead with a brief summary of what the output will produce\n\n"
        "Technical standards:\n"
        "- Default to architectural units (feet/inches) unless metric is specified\n"
        "- Include UNITS, LIMITS, and ZOOM E commands at the top of every .scr file\n"
        "- Use proper AIA layer naming: A-WALL, A-DOOR, A-GLAZ, S-BEAM, S-COLS, C-TOPO, etc.\n"
        "- For A-frame / PeakHinge geometry: define key parametric variables first (span, pitch, "
        "height, bay spacing), then derive all other dimensions from those variables\n"
        "- For structural drawings: include dimension strings, leader notes, and title block placeholder\n"
        "- DXF output uses R2013 format for maximum compatibility\n"
        "- In AutoLISP: define variables at the top, add inline comments, end with a usage docstring\n"
        "- Always state assumptions about units, origin point, and coordinate system\n"
        "- Flag any geometry that requires field verification or engineering stamp"
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
    ("cad", [
        "autocad", "autolisp", "cad drawing", "cad file", "dxf", "dwg",
        "draw a ", "draw the ", "floor plan", "site plan", "elevation drawing",
        "section drawing", "detail drawing", "cad script", "lisp routine",
        "drafting", "2d drawing", "technical drawing", "orthographic",
        "hatching", "dimension line", "annotation", "viewports",
        "layer management", "block insert", "xref", ".scr file", ".lsp file",
        "peakhinge drawing", "a-frame drawing", "cad model",
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
        "cad":          "claude",  # Claude generates best-quality CAD scripts and AutoLISP
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


def _structured_completion(system: str, prompt: str, domain: str = "business_ops") -> Dict[str, Any]:
    model_name = _first_available_model("claude", "gpt", "gemini")
    if not model_name:
        raise HTTPException(status_code=503, detail="No model API key is configured for operational actions")
    raw, _ = dispatch(model_name, domain, system, prompt)
    try:
        return _extract_json_object(raw)
    except ValueError as exc:
        logger.error(f"Structured completion parse failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc))


def _normalize_queue_item(payload: Dict[str, Any]) -> QueueItem:
    return QueueItem(
        item=(payload.get("item") or "").strip(),
        item_type=(payload.get("item_type") or "Task").strip() or "Task",
        notes=(payload.get("notes") or "").strip() or None,
        source="ChatGPT",
        priority=(payload.get("priority") or "Medium").strip() or "Medium",
        source_link=(payload.get("source_link") or "").strip() or None,
    )


def _build_review_sweep(req: SweepRequest) -> List[QueueItem]:
    system = (
        "You extract actionable operational items from AI conversations. "
        "Return ONLY JSON with this shape: "
        '{"queue_items":[{"item":"", "item_type":"Task|Project Update|Decision|Follow-up|Idea|Research Request|Admin|Risk|Reference", '
        '"notes":"", "priority":"High|Medium|Low"}]}. '
        "Include only concrete items worth saving to a review queue. "
        "Do not invent facts. Use an empty array if nothing merits capture."
    )
    prompt = (
        f"User prompt:\n{req.prompt}\n\n"
        f"Assistant response:\n{req.response}\n\n"
        f"Conversation context:\n{req.conversation or ''}\n\n"
        f"Maximum items: {max(1, min(req.max_items or 5, 10))}"
    )
    payload = _structured_completion(system, prompt, domain="business_ops")
    queue_items = payload.get("queue_items") or []
    normalized: List[QueueItem] = []
    for raw_item in queue_items:
        if not isinstance(raw_item, dict):
            continue
        item = _normalize_queue_item(raw_item)
        if item.item:
            normalized.append(item)
    return normalized[:max(1, min(req.max_items or 5, 10))]


def _build_snapshot_payload(req: SnapshotRequest) -> Dict[str, str]:
    system = (
        "You convert an AI conversation into a structured project snapshot. "
        "Return ONLY JSON with keys: "
        '{"snapshot_name":"", "current_state":"", "top_3_priorities":"", "recent_decisions":"", '
        '"open_questions":"", "risks_blockers":"", "recommended_next_moves":""}. '
        "Be concise, concrete, and operational. If a section is unknown, return an empty string."
    )
    prompt = (
        f"Preferred snapshot name: {req.snapshot_name or ''}\n\n"
        f"User prompt:\n{req.prompt}\n\n"
        f"Assistant response:\n{req.response}\n\n"
        f"Conversation context:\n{req.conversation or ''}"
    )
    payload = _structured_completion(system, prompt, domain="strategy")
    snapshot_name = (req.snapshot_name or payload.get("snapshot_name") or "").strip()
    if not snapshot_name:
        domain = detect_domain(req.prompt).replace("_", " ").title()
        snapshot_name = f"{domain} Snapshot {datetime.utcnow().strftime('%Y-%m-%d')}"
    return {
        "snapshot_name": snapshot_name[:1800],
        "current_state": (payload.get("current_state") or "").strip(),
        "top_3_priorities": (payload.get("top_3_priorities") or "").strip(),
        "recent_decisions": (payload.get("recent_decisions") or "").strip(),
        "open_questions": (payload.get("open_questions") or "").strip(),
        "risks_blockers": (payload.get("risks_blockers") or "").strip(),
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
        raise HTTPException(status_code=500, detail=f"Failed to archive prior current snapshots: {exc}")
    return updated


def create_context_snapshot(snapshot: Dict[str, str], mark_previous_inactive: bool = False) -> dict:
    if not notion or not CONTEXT_SNAPSHOTS_DB_ID:
        raise HTTPException(status_code=503, detail="Context snapshots database is not configured")

    archived_count = _mark_existing_snapshots_inactive() if mark_previous_inactive else 0
    properties = {
        "Snapshot Name": {"title": _safe_title_parts(snapshot["snapshot_name"])},
        "Snapshot Date": {"date": {"start": datetime.utcnow().date().isoformat()}},
        "Current State": {"rich_text": _safe_rich_text_parts(snapshot.get("current_state"))},
        "Top 3 Priorities": {"rich_text": _safe_rich_text_parts(snapshot.get("top_3_priorities"))},
        "Recent Decisions": {"rich_text": _safe_rich_text_parts(snapshot.get("recent_decisions"))},
        "Open Questions": {"rich_text": _safe_rich_text_parts(snapshot.get("open_questions"))},
        "Risks / Blockers": {"rich_text": _safe_rich_text_parts(snapshot.get("risks_blockers"))},
        "Recommended Next Moves": {"rich_text": _safe_rich_text_parts(snapshot.get("recommended_next_moves"))},
        "Still Current?": {"checkbox": True},
    }

    try:
        page = notion.pages.create(
            parent={"database_id": CONTEXT_SNAPSHOTS_DB_ID},
            properties=properties,
        )
        return {"id": page["id"], "url": page["url"], "archived_count": archived_count}
    except Exception as exc:
        logger.error(f"Error creating context snapshot: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


def _normalize_history_messages(messages: List[Dict[str, Any]], limit: int = 20) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    for msg in (messages or [])[-limit:]:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")[:32]
        content = str(msg.get("content") or "")[:12000]
        if role and content:
            normalized.append({"role": role, "content": content})
    return normalized


def _normalize_history_conversations(conversations: List[Dict[str, Any]], limit: int = 30) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for conv in (conversations or [])[:limit]:
        if not isinstance(conv, dict):
            continue
        conv_id = str(conv.get("id") or "").strip()
        if not conv_id:
            continue
        normalized.append({
            "id": conv_id[:128],
            "title": str(conv.get("title") or "")[:200],
            "model": str(conv.get("model") or "auto")[:64],
            "ts": int(conv.get("ts") or 0),
            "messages": _normalize_history_messages(conv.get("messages") or [], limit=60),
        })
    return normalized


def read_history_state(user_id: str) -> Dict[str, Any]:
    with _history_lock, _history_conn() as conn:
        row = conn.execute(
            "SELECT user_id, current_conv_id, conversation_memory, conversations, last_updated FROM conversation_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return {
            "found": False,
            "user_id": user_id,
            "current_conv_id": None,
            "conversation_memory": [],
            "conversations": [],
            "last_updated": None,
        }
    return {
        "found": True,
        "user_id": row["user_id"],
        "current_conv_id": row["current_conv_id"],
        "conversation_memory": json.loads(row["conversation_memory"]),
        "conversations": json.loads(row["conversations"]),
        "last_updated": row["last_updated"],
    }


def write_history_state(state: HistoryStateRequest) -> Dict[str, Any]:
    user_id = state.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")

    payload = {
        "current_conv_id": (state.current_conv_id or "").strip() or None,
        "conversation_memory": _normalize_history_messages(state.conversation_memory),
        "conversations": _normalize_history_conversations(state.conversations),
    }
    now = datetime.utcnow().isoformat()
    with _history_lock, _history_conn() as conn:
        conn.execute(
            """
            INSERT INTO conversation_state (user_id, current_conv_id, conversation_memory, conversations, last_updated)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                current_conv_id = excluded.current_conv_id,
                conversation_memory = excluded.conversation_memory,
                conversations = excluded.conversations,
                last_updated = excluded.last_updated
            """,
            (
                user_id,
                payload["current_conv_id"],
                json.dumps(payload["conversation_memory"], ensure_ascii=False),
                json.dumps(payload["conversations"], ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    return {"success": True, "user_id": user_id, "last_updated": now, **payload}


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


@app.get("/history/state")
async def get_history_state(user_id: str, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    return read_history_state(user_id.strip())


@app.post("/history/state")
async def put_history_state(state: HistoryStateRequest, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    return write_history_state(state)


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

# ?? Thread pool for parallel async agent calls ??????????????????????????????
import asyncio, json as _json, re as _re
from concurrent.futures import ThreadPoolExecutor
_executor = ThreadPoolExecutor(max_workers=8)

# ?? Orchestration models ??????????????????????????????????????????????????????
class OrchestrateRequest(BaseModel):
    prompt: str
    model:  str = "auto"

async def _call_agent_async(domain: str, sub_prompt: str, context_block: str) -> dict:
    """Run one domain agent in a thread so agents execute in parallel."""
    persona = EXPERT_PERSONAS.get(domain, EXPERT_PERSONAS["default"])
    system  = persona + (f"\n\n{context_block}" if context_block else "")
    loop    = asyncio.get_running_loop()
    try:
        result, model_used = await loop.run_in_executor(
            _executor,
            lambda: dispatch(domain_preferred_model(domain), domain, system, sub_prompt)
        )
        return {"domain": domain, "response": result, "model": model_used, "error": None}
    except Exception as e:
        logger.error(f"Agent {domain} failed: {e}")
        return {"domain": domain, "response": None, "model": None, "error": str(e)}

def _decompose_prompt(prompt: str) -> dict:
    """Ask Claude to split a multi-domain prompt into domain-specific sub-tasks."""
    domain_list = ", ".join(k for k in EXPERT_PERSONAS.keys() if k != "default")
    system = (
        "You are a task router for a multi-agent AI system. "
        "Given the user's prompt, decide if it spans multiple expert domains. "
        f"Available domains: {domain_list}.\n\n"
        "Reply with ONLY valid JSON - no explanation, no markdown:\n"
        '{"multi_domain": true/false, '
        '"tasks": [{"domain": "...", "sub_prompt": "...focused sub-task..."}], '
        '"reasoning": "one sentence"}'
        "\n\nRules:\n"
        "- Only set multi_domain=true if the prompt GENUINELY needs 2+ distinct expert perspectives.\n"
        "- Each sub_prompt must be self-contained and specific to its domain.\n"
        "- If single domain, return tasks with one entry and multi_domain=false.\n"
        "- Maximum 4 tasks."
    )
    try:
        raw = call_claude(system, prompt)
        m   = _re.search(r'\{[\s\S]*\}', raw)
        return _json.loads(m.group()) if m else {"multi_domain": False, "tasks": []}
    except Exception as e:
        logger.error(f"Decompose failed: {e}")
        return {"multi_domain": False, "tasks": []}

@app.post("/orchestrate")
async def orchestrate(req: OrchestrateRequest, api_key: Optional[str] = Security(api_key_header)):
    """
    Multi-agent orchestration:
    1. Claude decomposes the prompt into domain sub-tasks.
    2. All domain agents run IN PARALLEL via asyncio.gather().
    3. Claude synthesizes the parallel outputs into one response.
    Falls back to single-agent /run if the prompt is single-domain.
    """
    verify_api_key(api_key)
    snapshots = get_current_snapshots()
    drive_context = get_drive_context()
    context_block = build_context_block(snapshots, drive_context)

    # ?? Step 1: Decompose ????????????????????????????????????????????????????
    decomp = _decompose_prompt(req.prompt)
    tasks  = decomp.get("tasks", [])

    if not decomp.get("multi_domain") or len(tasks) < 2:
        # Single domain - use normal dispatch
        system, domain = build_expert_system(req.prompt, context_block)
        try:
            response, model_used = dispatch(req.model, domain, system, req.prompt)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {
            "response":      response,
            "model":         model_used,
            "domain":        domain,
            "orchestrated":  False,
            "agents":        [],
            "reasoning":     decomp.get("reasoning", ""),
            "notion_context_used": bool(snapshots),
            "drive_context_used": bool(drive_context),
        }

    # ?? Step 2: Parallel agent dispatch ?????????????????????????????????????
    logger.info(f"Orchestrating {len(tasks)} agents: {[t['domain'] for t in tasks]}")
    agent_coros = [_call_agent_async(t["domain"], t["sub_prompt"], context_block) for t in tasks]
    results     = await asyncio.gather(*agent_coros)

    successful  = [r for r in results if r["response"]]
    failed      = [r for r in results if r["error"]]
    if failed:
        logger.warning(f"Agents failed: {[(r['domain'], r['error']) for r in failed]}")

    if not successful:
        raise HTTPException(status_code=502, detail="All agents failed.")

    # ?? Step 3: Synthesize ???????????????????????????????????????????????????
    agent_outputs = "\n\n".join(
        f"=== {r['domain'].upper()} AGENT ===\n{r['response']}" for r in successful
    )
    synthesis_prompt = (
        f"Original user request:\n{req.prompt}\n\n"
        f"Expert agent responses:\n{agent_outputs}\n\n"
        "Synthesize these into one clear, well-structured response. "
        "Integrate the domain perspectives naturally - do not just concatenate. "
        "Lead with the most actionable insight."
    )
    synthesis_system = (
        "You are a synthesis agent. You receive parallel expert responses and weave them "
        "into one authoritative, cohesive answer. Preserve domain-specific precision while "
        "creating a unified narrative. Be direct and action-oriented."
    )
    try:
        final_response, _ = dispatch("claude", "default", synthesis_system, synthesis_prompt)
    except Exception:
        # Graceful fallback: structured concatenation
        final_response = "\n\n".join(
            f"**{r['domain'].upper()}**\n{r['response']}" for r in successful
        )

    return {
        "response":    final_response,
        "model":       "claude (synthesis)",
        "domain":      "orchestrated",
        "orchestrated": True,
        "agents":      [{"domain": r["domain"], "model": r["model"], "error": r["error"]} for r in results],
        "reasoning":   decomp.get("reasoning", ""),
        "notion_context_used": bool(snapshots),
        "drive_context_used": bool(drive_context),
    }

class CadRequest(BaseModel):
    prompt:    str
    format:    str  = "auto"   # "auto" | "scr" | "lsp" | "dxf"
    model:     str  = "auto"

@app.post("/cad")
async def cad_endpoint(req: CadRequest, api_key: Optional[str] = Security(api_key_header)):
    """Natural language ? AutoCAD script/routine with downloadable output."""
    verify_api_key(api_key)
    fmt_hint = {
        "scr": "Generate an AutoCAD script (.scr) file. Use ```autocad code blocks.",
        "lsp": "Generate an AutoLISP routine (.lsp). Use ```autolisp code blocks.",
        "dxf": "Generate a DXF file fragment. Use ```dxf code blocks.",
        "auto": (
            "Choose the best output format: .scr for simple draw commands, "
            ".lsp for parametric/formulaic geometry, .dxf for entity import. "
            "Use the appropriate code block tag (autocad / autolisp / dxf)."
        ),
    }.get(req.format, "")

    context_block = build_context_block(get_current_snapshots(), get_drive_context())
    system = EXPERT_PERSONAS["cad"]
    if context_block:
        system += f"\n\n{context_block}"

    augmented_prompt = (
        f"{req.prompt}\n\n"
        f"[Format instruction: {fmt_hint}]\n"
        "After the code block, include:\n"
        "1. How to load/run this in AutoCAD (exact steps)\n"
        "2. Key dimensions and assumptions made\n"
        "3. Suggested layer names and colors"
    )

    try:
        response_text, model_used = dispatch(req.model, "cad", system, augmented_prompt)
        # Detect which script format was used
        script_format = "scr"
        if "```autolisp" in response_text.lower():
            script_format = "lsp"
        elif "```dxf" in response_text.lower():
            script_format = "dxf"

        return {
            "response":      response_text,
            "model":         model_used,
            "domain":        "cad",
            "script_format": script_format,
            "filename":      f"irving_cad_{script_format}.{script_format}",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

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
        "notion_context_used":        bool(snapshots),
        "drive_context_used":         bool(drive_context),
        "drive_output_link":          drive_link,
    }


@app.post("/queue")
async def queue(item: QueueItem, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    result = push_to_review_queue(item)
    return {"success": True, "notion_page": result}


@app.post("/ops/sweep")
async def sweep(req: SweepRequest, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    queue_items = _build_review_sweep(req)
    created = [push_to_review_queue(item) for item in queue_items]
    return {
        "success": True,
        "queued_count": len(created),
        "queue_items": [item.dict() for item in queue_items],
        "notion_pages": created,
    }


@app.post("/ops/snapshot")
async def snapshot(req: SnapshotRequest, api_key: Optional[str] = Security(api_key_header)):
    verify_api_key(api_key)
    snapshot_payload = _build_snapshot_payload(req)
    created = create_context_snapshot(snapshot_payload, mark_previous_inactive=bool(req.mark_previous_inactive))
    return {
        "success": True,
        "snapshot": snapshot_payload,
        "notion_page": created,
    }
