"""
irving/routes/history.py
─────────────────────────
Conversation history persistence routes: GET and POST /history/state.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

from irving.config import IRVING_API_KEY
from irving.models import HistoryStateRequest
from irving.persistence.firestore import read_history_state, write_history_state

logger         = logging.getLogger(__name__)
router         = APIRouter()
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _verify(api_key: Optional[str]) -> None:
    if IRVING_API_KEY and api_key != IRVING_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


@router.get("/history/state")
async def get_history_state(user_id: str, api_key: Optional[str] = Security(api_key_header)):
    _verify(api_key)
    return read_history_state(user_id.strip())


@router.post("/history/state")
async def put_history_state(state: HistoryStateRequest, api_key: Optional[str] = Security(api_key_header)):
    _verify(api_key)
    return write_history_state(state)
