"""
irving/agents/orchestrate.py
─────────────────────────────
Multi-agent orchestration: decompose → parallel dispatch → synthesise.
"""
import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from irving.agents.callers import call_claude, dispatch
from irving.agents.personas import EXPERT_PERSONAS
from irving.agents.routing import domain_preferred_model

logger    = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=8)


def decompose_prompt(prompt: str) -> dict:
    """Ask Claude to split a multi-domain prompt into domain-specific sub-tasks."""
    domain_list = ", ".join(k for k in EXPERT_PERSONAS if k != "default")
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
        raw   = call_claude(system, prompt)
        match = re.search(r"\{[\s\S]*\}", raw)
        return json.loads(match.group()) if match else {"multi_domain": False, "tasks": []}
    except Exception as e:
        logger.error(f"Decompose failed: {e}")
        return {"multi_domain": False, "tasks": []}


async def call_agent_async(domain: str, sub_prompt: str, context_block: str) -> dict:
    """Run one domain agent in a thread so all agents execute in parallel."""
    persona = EXPERT_PERSONAS.get(domain, EXPERT_PERSONAS["default"])
    system  = persona + (f"\n\n{context_block}" if context_block else "")
    loop    = asyncio.get_running_loop()
    try:
        result, model_used = await loop.run_in_executor(
            _executor,
            lambda: dispatch(domain_preferred_model(domain), domain, system, sub_prompt),
        )
        return {"domain": domain, "response": result, "model": model_used, "error": None}
    except Exception as e:
        logger.error(f"Agent {domain} failed: {e}")
        return {"domain": domain, "response": None, "model": None, "error": str(e)}


def synthesize_agent_outputs(original_prompt: str, successful: list) -> str:
    """Merge parallel agent responses into one coherent answer via Claude."""
    agent_outputs = "\n\n".join(
        f"=== {r['domain'].upper()} AGENT ===\n{r['response']}" for r in successful
    )
    synthesis_prompt = (
        f"Original user request:\n{original_prompt}\n\n"
        f"Expert agent responses:\n{agent_outputs}\n\n"
        "Synthesise these into one clear, well-structured response. "
        "Integrate the domain perspectives naturally - do not just concatenate. "
        "Lead with the most actionable insight."
    )
    synthesis_system = (
        "You are a synthesis agent. You receive parallel expert responses and weave them "
        "into one authoritative, cohesive answer. Preserve domain-specific precision while "
        "creating a unified narrative. Be direct and action-oriented."
    )
    try:
        final, _ = dispatch("claude", "default", synthesis_system, synthesis_prompt)
        return final
    except Exception:
        return "\n\n".join(f"**{r['domain'].upper()}**\n{r['response']}" for r in successful)
