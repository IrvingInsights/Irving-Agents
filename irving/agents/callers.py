"""
irving/agents/callers.py
────────────────────────
Raw model callers (Claude, GPT-4o, Gemini) and the dispatch router.
Also contains the structured-completion helper used by ops endpoints.
"""
import json
import logging
import re
from typing import Any, Dict, Optional

from fastapi import HTTPException

from irving.config import ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY
from irving.agents.routing import domain_preferred_model

logger = logging.getLogger(__name__)


# ── Raw callers ──────────────────────────────────────────────────────────────

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
            ],
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


# ── Dispatch ─────────────────────────────────────────────────────────────────

def dispatch(model_req: str, domain: str, system: str, prompt: str) -> tuple[str, str]:
    """Route to the right model. Returns (response_text, model_label).
    Falls back to GPT-4o if the primary model fails.
    """
    resolved = domain_preferred_model(domain) if model_req == "auto" else model_req
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
                raise HTTPException(
                    status_code=502,
                    detail=f"All models failed. Primary: {primary_err}. Fallback: {fallback_err}",
                )
        raise HTTPException(status_code=502, detail=str(primary_err))


# ── Structured JSON completion ────────────────────────────────────────────────

def _first_available_model(*preferred: str) -> Optional[str]:
    for model_name in preferred:
        if model_name == "claude" and ANTHROPIC_API_KEY:
            return "claude"
        if model_name == "gpt" and OPENAI_API_KEY:
            return "gpt"
        if model_name == "gemini" and GEMINI_API_KEY:
            return "gemini"
    return None


def _extract_json_object(raw: str) -> Dict[str, Any]:
    if not raw:
        raise ValueError("Empty model output")
    candidates = [raw.strip()]
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


def structured_completion(system: str, prompt: str, domain: str = "business_ops") -> Dict[str, Any]:
    """Run a model call and parse the JSON response. Used by ops endpoints."""
    model_name = _first_available_model("claude", "gpt", "gemini")
    if not model_name:
        raise HTTPException(status_code=503, detail="No model API key configured for operational actions")
    raw, _ = dispatch(model_name, domain, system, prompt)
    try:
        return _extract_json_object(raw)
    except ValueError as exc:
        logger.error(f"Structured completion parse failed: {exc}")
        raise HTTPException(status_code=502, detail=str(exc))
