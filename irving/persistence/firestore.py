"""
irving/persistence/firestore.py
────────────────────────────────
Firestore-backed conversation history: read and write. Thread-safe.
"""
import hashlib
import logging
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from irving.config import FIRESTORE_PROJECT_ID, FIRESTORE_COLLECTION
from irving.models import HistoryStateRequest

logger        = logging.getLogger(__name__)
_history_lock = threading.Lock()
_fs_client    = None


def get_firestore_client():
    global _fs_client
    if _fs_client is not None:
        return _fs_client
    try:
        from google.cloud import firestore
        kwargs = {}
        if FIRESTORE_PROJECT_ID:
            kwargs["project"] = FIRESTORE_PROJECT_ID
        _fs_client = firestore.Client(**kwargs)
        return _fs_client
    except Exception as exc:
        logger.error(f"Failed to initialise Firestore client: {exc}")
        return None


def _doc_id(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _normalize_messages(messages: List[Dict[str, Any]], limit: int = 20) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    for msg in (messages or [])[-limit:]:
        if not isinstance(msg, dict):
            continue
        role    = str(msg.get("role")    or "")[:32]
        content = str(msg.get("content") or "")[:12000]
        if role and content:
            normalized.append({"role": role, "content": content})
    return normalized


def _normalize_conversations(conversations: List[Dict[str, Any]], limit: int = 30) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for conv in (conversations or [])[:limit]:
        if not isinstance(conv, dict):
            continue
        conv_id = str(conv.get("id") or "").strip()
        if not conv_id:
            continue
        normalized.append({
            "id":       conv_id[:128],
            "title":    str(conv.get("title") or "")[:200],
            "model":    str(conv.get("model")  or "auto")[:64],
            "ts":       int(conv.get("ts")     or 0),
            "messages": _normalize_messages(conv.get("messages") or [], limit=60),
        })
    return normalized


def read_history_state(user_id: str) -> Dict[str, Any]:
    client = get_firestore_client()
    if not client:
        raise HTTPException(status_code=503, detail="Firestore is not configured")
    try:
        with _history_lock:
            doc = client.collection(FIRESTORE_COLLECTION).document(_doc_id(user_id)).get()
    except Exception as exc:
        logger.error(f"Error reading history state: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to read history state: {exc}")
    if not doc.exists:
        return {"found": False, "user_id": user_id, "current_conv_id": None,
                "conversation_memory": [], "conversations": [], "last_updated": None}
    data = doc.to_dict() or {}
    return {"found": True, "user_id": data.get("user_id") or user_id,
            "current_conv_id": data.get("current_conv_id"),
            "conversation_memory": data.get("conversation_memory") or [],
            "conversations": data.get("conversations") or [],
            "last_updated": data.get("last_updated")}


def write_history_state(state: HistoryStateRequest) -> Dict[str, Any]:
    user_id = state.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    payload = {
        "current_conv_id":     (state.current_conv_id or "").strip() or None,
        "conversation_memory": _normalize_messages(state.conversation_memory),
        "conversations":       _normalize_conversations(state.conversations),
    }
    now    = datetime.utcnow().isoformat()
    client = get_firestore_client()
    if not client:
        raise HTTPException(status_code=503, detail="Firestore is not configured")
    try:
        with _history_lock:
            client.collection(FIRESTORE_COLLECTION).document(_doc_id(user_id)).set({
                "user_id": user_id, "current_conv_id": payload["current_conv_id"],
                "conversation_memory": payload["conversation_memory"],
                "conversations": payload["conversations"], "last_updated": now,
            })
    except Exception as exc:
        logger.error(f"Error writing history state: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to write history state: {exc}")
    return {"success": True, "user_id": user_id, "last_updated": now, **payload}
