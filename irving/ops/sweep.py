"""
irving/ops/sweep.py
────────────────────
Build a list of QueueItems from an AI conversation (the "sweep" operation).
"""
import logging
from typing import List

from irving.agents.callers import structured_completion
from irving.models import QueueItem, SweepRequest
from irving.ops.queue import normalize_queue_item

logger = logging.getLogger(__name__)


def build_review_sweep(req: SweepRequest) -> List[QueueItem]:
    system = (
        "You extract actionable operational items from AI conversations. "
        "Return ONLY JSON with this shape: "
        '{"queue_items":[{"item":"", "item_type":"Task|Project Update|Decision|Follow-up|Idea|Research Request|Admin|Risk|Reference", '
        '"notes":"", "priority":"High|Medium|Low"}]}. '
        "Include only concrete items worth saving to a review queue. "
        "Do not invent facts. Use an empty array if nothing merits capture."
    )
    max_items = max(1, min(req.max_items or 5, 10))
    prompt = (
        f"User prompt:\n{req.prompt}\n\n"
        f"Assistant response:\n{req.response}\n\n"
        f"Conversation context:\n{req.conversation or ''}\n\n"
        f"Maximum items: {max_items}"
    )
    payload    = structured_completion(system, prompt, domain="business_ops")
    raw_items  = payload.get("queue_items") or []
    normalized: List[QueueItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item = normalize_queue_item(raw_item)
        if item.item:
            normalized.append(item)
    return normalized[:max_items]
