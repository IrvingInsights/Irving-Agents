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
from typing import Optional

logger = logging.getLogger(__name__)

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

PEAKHINGE_REFERENCE_CONTEXT = """\
--- AUTHORITATIVE PEAKHINGE REFERENCE CONTEXT (Notion) ---
Ground truth sources:
- Design Documentation - v1 · March 2026 (32541623-f9ca-8142-80c9-c42f65ab2ced)
- Technical Specification - PeakHinge Tri-fold System · v1 March 2026 (32641623-f9ca-8179-8248-fc8b7e01f167)
- Cowork Session Brief - PeakHinge Designs · Active (33441623-f9ca-814f-b554-fdd6ae57a247)

Locked system facts:
- The hinge mechanism is a pintle design, not a piano hinge or barrel hinge.
- Every hinge line uses the same 2" Schedule 40 galvanized steel pipe with proud UHMW-PE sleeves as the bearing surface and thermal break.
- PeakHinge 144 locked geometry: 10'-0" cabin width, 14'-4 13/16" cabin length, 60 degree pitch, 3'-0" knee walls, 2x6 SPF ribs, 6 ribs at 24" OC, 10'-0" rafter length, ridge about 11'-8" above floor, loft at 7'-0" AFF with about 5'-4" clear width.
- Ridge hinge geometry: rafters extend past the pipe and cross like scissors, with the ridge pipe about 12" below the rafter tips. A horizontal lock beam drops into notches at the top faces.
- The plinth cassette is one rigid 10' x 10' floor panel with no center fold.
- Knee wall base hinge (cassette hinge) uses a 4x4 compression post, steel bearing plate, cotter pin / bolt secondary lock, and full-width EPDM sill gasket.
- Assembly intent: rafter panels, knee walls, ridge pipe axis, loft joists, and plinth cassette should all read clearly in plan, front, side, and isometric outputs.

Use this reference context as ground truth over generic A-frame assumptions or stale drive notes. Do not simplify PeakHinge into placeholder boxes or a generic rib array unless the user explicitly asks for a massing study only.
---"""


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
GOOGLE_SA_JSON          = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SA_INFO          = _load_google_service_account_info(GOOGLE_SA_JSON)
DRIVE_OUTPUT_FOLDER_ID  = _env("DRIVE_OUTPUT_FOLDER_ID")
FIRESTORE_PROJECT_ID    = _env("FIRESTORE_PROJECT_ID")
FIRESTORE_COLLECTION    = _env("FIRESTORE_HISTORY_COLLECTION", "conversation_state")
