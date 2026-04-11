"""
irving/context/notion.py
─────────────────────────
Notion snapshot fetching and context-block assembly.
"""
import json
import logging
import urllib.request
from typing import Optional

from irving.config import NOTION_TOKEN, CONTEXT_SNAPSHOTS_DB_ID
from irving.agents.routing import get_project_reference_context

logger = logging.getLogger(__name__)


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
    if not text: return []
    return [{"text": {"content": text[i:i+chunk_size]}} for i in range(0, len(text), chunk_size)]


def get_current_snapshots(limit: int = 3) -> list:
    """Query Notion Context Snapshots DB via REST (bypasses notion-client version quirks)."""
    if not NOTION_TOKEN or not CONTEXT_SNAPSHOTS_DB_ID:
        return []
    try:
        url  = f"https://api.notion.com/v1/databases/{CONTEXT_SNAPSHOTS_DB_ID}/query"
        body = json.dumps({
            "filter": {"property": "Still Current?", "checkbox": {"equals": True}},
            "sorts":  [{"property": "Snapshot Date", "direction": "descending"}],
            "page_size": limit,
        }).encode()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Authorization":  f"Bearer {NOTION_TOKEN}",
            "Content-Type":   "application/json",
            "Notion-Version": "2022-06-28",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
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


def build_context_block(snapshots: list, drive_context: str = "", prompt: str = "") -> str:
    lines = []
    if snapshots:
        lines.append("--- CURRENT PROJECT CONTEXT (Notion Snapshots) ---\n")
        for i, s in enumerate(snapshots, 1):
            lines.append(f"[{i}] {s['name']}  |  Date: {s['snapshot_date']}")
            for key, label in [
                ("current_state", "Current State"), ("top_3_priorities", "Top Priorities"),
                ("recent_decisions", "Recent Decisions"), ("open_questions", "Open Questions"),
                ("risks_blockers", "Risks / Blockers"), ("recommended_next_moves", "Next Moves"),
            ]:
                if s.get(key):
                    lines.append(f"  {label}: {s[key]}")
            lines.append("")
        lines.append("---\n")
    reference_context = get_project_reference_context(prompt, snapshots)
    if reference_context:
        lines.append(reference_context)
    if drive_context:
        lines.append(drive_context)
    return "\n".join(lines)
