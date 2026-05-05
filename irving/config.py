"""
irving/config.py
────────────────
All environment variables, constants, and startup configuration.
Nothing in this module imports from other irving.* modules.
"""
import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ── Notion ───────────────────────────────────────────────────────────────────
DEFAULT_CONTEXT_SNAPSHOTS_DB_ID = "57887d95-300e-4f9d-802c-1283b4132e02"
NOTION_ID_RE = re.compile(
    r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# ── PeakHinge reference ──────────────────────────────────────────────────────
PEAKHINGE_KEYWORDS = (
    "peakhinge", "peak hinge", "tri-fold", "trifold", "a-frame", "aframe",
    "pintle", "knee wall", "ridge pipe", "plinth cassette", "loft joist",
)

def _load_text_file(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Could not load context file %s: %s", path, exc)
        return default


PEAKHINGE_REFERENCE_CONTEXT = _load_text_file(
    PROJECT_ROOT / "knowledge" / "peakhinge-reference.md"
)


# ── Internal helpers ─────────────────────────────────────────────────────────
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


# ── Resolved configuration ────────────────────────────────────────────────────
NOTION_TOKEN            = _env("NOTION_TOKEN")
REVIEW_QUEUE_DB_ID      = _normalize_notion_id(_env("NOTION_REVIEW_QUEUE_DB_ID"))
CONTEXT_SNAPSHOTS_DB_ID = _normalize_notion_id(
    _env("NOTION_CONTEXT_SNAPSHOTS_DB_ID", DEFAULT_CONTEXT_SNAPSHOTS_DB_ID)
)
ANTHROPIC_API_KEY       = _env("ANTHROPIC_API_KEY")
OPENAI_API_KEY          = _env("OPENAI_API_KEY")
GEMINI_API_KEY          = _env("GEMINI_API_KEY")
IRVING_API_KEY          = _env("IRVING_API_KEY")
CLAUDE_MODEL            = _env("CLAUDE_MODEL", "claude-opus-4-6")
OPENAI_MODEL            = _env("OPENAI_MODEL", "gpt-4o")
GEMINI_MODEL            = _env("GEMINI_MODEL", "gemini-1.5-pro")
GOOGLE_SA_JSON          = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SA_INFO          = _load_google_service_account_info(GOOGLE_SA_JSON)
DRIVE_OUTPUT_FOLDER_ID  = _env("DRIVE_OUTPUT_FOLDER_ID")
FIRESTORE_PROJECT_ID    = _env("FIRESTORE_PROJECT_ID")
FIRESTORE_COLLECTION    = _env("FIRESTORE_HISTORY_COLLECTION", "conversation_state")
