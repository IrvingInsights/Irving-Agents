"""
irving/routes/run.py
─────────────────────
Core inference routes: /run, /orchestrate, /cad, /context.
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader

from irving.agents.callers import dispatch
from irving.agents.orchestrate import call_agent_async, decompose_prompt, synthesize_agent_outputs
from irving.agents.routing import build_expert_system, is_peakhinge_prompt
from irving.agents.personas import EXPERT_PERSONAS
from irving.config import IRVING_API_KEY
from irving.context.drive import get_drive_context, save_to_drive
from irving.context.notion import build_context_block, get_current_snapshots, get_project_reference_context
from irving.models import CadRequest, OrchestrateRequest, RunRequest

logger         = logging.getLogger(__name__)
router         = APIRouter()
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _verify(api_key: Optional[str]) -> None:
    if IRVING_API_KEY and api_key != IRVING_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


@router.get("/context")
async def get_context(api_key: Optional[str] = Security(api_key_header)):
    _verify(api_key)
    snapshots     = get_current_snapshots()
    drive_context = get_drive_context()
    context_block = build_context_block(snapshots, drive_context, "")
    return {"snapshots": snapshots, "snapshot_count": len(snapshots),
            "drive_context": bool(drive_context),
            "reference_context": bool(get_project_reference_context("", snapshots)),
            "context_block": context_block}


@router.post("/run")
async def run(request: RunRequest, api_key: Optional[str] = Security(api_key_header)):
    _verify(api_key)
    snapshots      = get_current_snapshots()
    drive_context  = get_drive_context(query=request.prompt[:100])
    context_block  = build_context_block(snapshots, drive_context, request.prompt)
    system, domain = build_expert_system(request.prompt, context_block, request.domain_override)
    try:
        response_text, model_used = dispatch(request.model or "auto", domain, system, request.prompt)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Dispatch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Model call failed: {e}")
    drive_link = drive_error = None
    if request.store_to_drive:
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        drive_link, drive_error = save_to_drive(
            filename=f"Irving_{domain}_{model_used}_{ts}",
            content=f"Prompt:\n{request.prompt}\n\n---\n\nResponse ({model_used}, {domain} expert):\n{response_text}",
            folder_id=request.drive_folder_id,
        )
    return {
        "response": response_text, "model_used": model_used,
        "model_requested": request.model or "auto", "domain": domain,
        "context_snapshots_injected": len(snapshots),
        "drive_context_injected": bool(drive_context),
        "notion_context_used": bool(snapshots), "drive_context_used": bool(drive_context),
        "drive_output_link": drive_link, "drive_output_error": drive_error,
    }


@router.post("/orchestrate")
async def orchestrate(req: OrchestrateRequest, api_key: Optional[str] = Security(api_key_header)):
    _verify(api_key)
    snapshots     = get_current_snapshots()
    drive_context = get_drive_context(req.prompt)
    context_block = build_context_block(snapshots, drive_context, req.prompt)
    decomp = decompose_prompt(req.prompt)
    tasks  = decomp.get("tasks", [])
    if not decomp.get("multi_domain") or len(tasks) < 2:
        system, domain = build_expert_system(req.prompt, context_block, req.domain_override)
        try:
            response, model_used = dispatch(req.model, domain, system, req.prompt)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {"response": response, "model": model_used, "domain": domain,
                "orchestrated": False, "agents": [], "reasoning": decomp.get("reasoning", ""),
                "notion_context_used": bool(snapshots), "drive_context_used": bool(drive_context)}
    logger.info(f"Orchestrating {len(tasks)} agents: {[t['domain'] for t in tasks]}")
    results    = await asyncio.gather(*[
        call_agent_async(t["domain"], t["sub_prompt"], context_block) for t in tasks
    ])
    successful = [r for r in results if r["response"]]
    failed     = [r for r in results if r["error"]]
    if failed:
        logger.warning(f"Agents failed: {[(r['domain'], r['error']) for r in failed]}")
    if not successful:
        raise HTTPException(status_code=502, detail="All agents failed.")
    return {
        "response": synthesize_agent_outputs(req.prompt, successful),
        "model": "claude (synthesis)", "domain": "orchestrated", "orchestrated": True,
        "agents": [{"domain": r["domain"], "model": r["model"], "error": r["error"]} for r in results],
        "reasoning": decomp.get("reasoning", ""),
        "notion_context_used": bool(snapshots), "drive_context_used": bool(drive_context),
    }


_FMT_HINTS = {
    "scr":     "Generate an AutoCAD script (.scr) file. Use ```autocad code blocks.",
    "lsp":     "Generate an AutoLISP routine (.lsp). Use ```autolisp code blocks.",
    "dxf":     "Generate a DXF file fragment. Use ```dxf code blocks.",
    "freecad": ("Generate a FreeCAD Python macro. Use one ```python code block only. "
                "The code must define build_model(doc). Inside that function, create the model, "
                "recompute the document, and return the main objects to export."),
    "auto":    ("Choose the best output format: .scr for simple draw commands, "
                ".lsp for parametric/formulaic geometry, .dxf for 2D entity import, "
                "or FreeCAD Python for parametric 3D solids. "
                "Use the appropriate code block tag (autocad / autolisp / dxf / python)."),
}
_EXT_MAP = {"scr": "scr", "lsp": "lsp", "dxf": "dxf", "freecad": "py"}


@router.post("/cad")
async def cad_endpoint(req: CadRequest, api_key: Optional[str] = Security(api_key_header)):
    _verify(api_key)
    fmt_hint      = _FMT_HINTS.get(req.format, "")
    snapshots     = get_current_snapshots()
    drive_context = get_drive_context(req.prompt)
    context_block = build_context_block(snapshots, drive_context, req.prompt)
    system = EXPERT_PERSONAS["cad"]
    if context_block:
        system += f"\n\n{context_block}"
    if is_peakhinge_prompt(req.prompt, snapshots):
        system += (
            "\n\nPROJECT-SPECIFIC CAD DIRECTIVES:\n"
            "- Use the authoritative PeakHinge context above as ground truth.\n"
            "- For FreeCAD Python, generate the actual PeakHinge tri-fold assembly.\n"
            "- Name objects: LeftRafterPanel, RightRafterPanel, LeftKneeWall, RightKneeWall, "
            "PlinthCassette, RidgePipe, LoftJoists.\n"
            "- Keep geometry suitable for plan, front, side, and isometric views.\n"
        )
    augmented = (
        f"{req.prompt}\n\n[Format instruction: {fmt_hint}]\n"
        "If PeakHinge request, use locked dimensions from system prompt.\n"
        "After the code block include: 1) how to run it, 2) key dimensions/assumptions, "
        "3) layer names, 4) export files if FreeCAD."
    )
    try:
        response_text, model_used = dispatch(req.model, "cad", system, augmented)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))
    rl = response_text.lower()
    script_format = "lsp" if "```autolisp" in rl else "dxf" if "```dxf" in rl else "freecad" if "```python" in rl else "scr"
    return {"response": response_text, "model": model_used, "domain": "cad",
            "script_format": script_format,
            "filename": f"irving_cad_{script_format}.{_EXT_MAP.get(script_format, script_format)}"}
