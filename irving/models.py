"""
irving/models.py
────────────────
All Pydantic request/response models.
"""
from typing import Any, Dict, List, Optional
from pydantic import BaseModel


class RunRequest(BaseModel):
    prompt: str
    model: Optional[str] = "auto"
    store_to_drive: Optional[bool] = False
    drive_folder_id: Optional[str] = None
    domain_override: Optional[str] = None


class CadRequest(BaseModel):
    prompt: str
    format: str = "auto"   # "auto" | "scr" | "lsp" | "dxf" | "freecad"
    model: str = "auto"


class OrchestrateRequest(BaseModel):
    prompt: str
    model: str = "auto"
    domain_override: Optional[str] = None


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
