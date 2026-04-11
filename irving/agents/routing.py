"""
irving/agents/routing.py
────────────────────────
Domain detection, model preference, and system-prompt assembly.
"""
from typing import Optional

from fastapi import HTTPException

from irving.config import PEAKHINGE_KEYWORDS, PEAKHINGE_REFERENCE_CONTEXT
from irving.agents.personas import EXPERT_PERSONAS, DOMAIN_SIGNALS


# ── Domain detection ─────────────────────────────────────────────────────────

def detect_domain(prompt: str) -> str:
    """Return the best-matching expert domain for a prompt."""
    pl = prompt.lower()
    for domain, signals in DOMAIN_SIGNALS:
        if any(s in pl for s in signals):
            return domain
    return "default"


def domain_preferred_model(domain: str) -> str:
    """When model='auto', return the best model for a given domain."""
    return {
        "code":         "gpt",
        "cad":          "claude",
        "architecture": "claude",
        "health":       "gemini",
        "structural":   "claude",
        "strategy":     "claude",
        "writing":      "claude",
        "design":       "claude",
        "content":      "claude",
        "hockey":       "claude",
        "business_ops": "claude",
        "default":      "claude",
    }.get(domain, "claude")


def resolve_domain_override(domain_override: Optional[str]) -> Optional[str]:
    """Normalise a UI domain override string to a canonical domain key."""
    if not domain_override:
        return None
    value = domain_override.strip().lower().replace("-", "_")
    alias_map = {
        "core":             None,
        "general":          "default",
        "arch":             "architecture",
        "graphic_design":   "design",
        "graphic":          "design",
        "brand":            "design",
        "content_pipeline": "content",
        "content_team":     "content",
    }
    resolved = alias_map.get(value, value)
    if resolved is None:
        return None
    if resolved not in EXPERT_PERSONAS:
        raise HTTPException(status_code=400, detail=f"Unknown domain override: {domain_override}")
    return resolved


# ── PeakHinge detection ──────────────────────────────────────────────────────

def is_peakhinge_prompt(prompt: str = "", snapshots: Optional[list] = None) -> bool:
    """Return True if the prompt or any current snapshot mentions PeakHinge."""
    prompt_l = (prompt or "").lower()
    if any(token in prompt_l for token in PEAKHINGE_KEYWORDS):
        return True
    for snapshot in snapshots or []:
        combined = " ".join(
            str(snapshot.get(key, ""))
            for key in ("name", "current_state", "recent_decisions")
        )
        if any(token in combined.lower() for token in PEAKHINGE_KEYWORDS):
            return True
    return False


def get_project_reference_context(prompt: str = "", snapshots: Optional[list] = None) -> str:
    """Return the locked PeakHinge reference block when the prompt is relevant."""
    return PEAKHINGE_REFERENCE_CONTEXT if is_peakhinge_prompt(prompt, snapshots) else ""


# ── System-prompt assembly ───────────────────────────────────────────────────

def build_expert_system(
    prompt: str,
    context_block: str = "",
    domain_override: Optional[str] = None,
) -> tuple[str, str]:
    """Build the full system prompt via expert domain routing.
    Returns (system_prompt, domain_key).
    """
    domain  = resolve_domain_override(domain_override) or detect_domain(prompt)
    persona = EXPERT_PERSONAS[domain]
    system  = persona

    if context_block:
        system += f"\n\n{context_block}"

    if is_peakhinge_prompt(prompt):
        if domain == "cad":
            system += (
                "\n\nPROJECT-SPECIFIC CAD DIRECTIVES:\n"
                "- Treat the authoritative PeakHinge reference context in this system prompt as ground truth.\n"
                "- Never regress to piano-hinge language or generic A-frame assumptions.\n"
                "- For FreeCAD output, build the actual tri-fold assembly components with meaningful object names, not placeholder boxes.\n"
                "- Unless the user explicitly requests a massing study, include the ridge pipe axis, paired rafter panels, knee walls, plinth cassette, and loft-related geometry.\n"
                "- Make the resulting model legible in top, front, right, and isometric drawing views.\n"
            )
        elif domain in {"architecture", "structural"}:
            system += (
                "\n\nPROJECT-SPECIFIC PEAKHINGE DIRECTIVES:\n"
                "- Use the locked PeakHinge geometry and hinge facts in the system prompt as authoritative.\n"
                "- Distinguish clearly between locked facts and open engineering items.\n"
                "- Do not present unresolved engineering items as settled.\n"
            )

    return system, domain
