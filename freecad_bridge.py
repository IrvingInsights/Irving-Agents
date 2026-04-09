import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


DEFAULT_FREECAD_PATHS = [
    r"C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe",
    r"C:\Program Files\FreeCAD\bin\freecadcmd.exe",
    r"C:\Program Files (x86)\FreeCAD\bin\freecadcmd.exe",
]
RUNS_DIR = Path(os.environ.get("IRVING_FREECAD_RUNS_DIR", Path.home() / ".irving-freecad" / "runs"))
RUNNER_PATH = Path(__file__).with_name("freecad_bridge_runner.py")

app = FastAPI(title="Irving FreeCAD Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExecuteFreeCADRequest(BaseModel):
    macro_source: str
    model_basename: str = Field(default="irving_freecad_model", min_length=1, max_length=120)
    export_formats: List[str] = Field(default_factory=lambda: ["fcstd", "step"])
    timeout_s: int = Field(default=180, ge=10, le=900)
    open_in_gui: bool = True


def _find_freecadcmd() -> Path:
    override = os.environ.get("FREECADCMD_PATH")
    candidates = [override] if override else []
    candidates.extend(DEFAULT_FREECAD_PATHS)
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    raise HTTPException(status_code=500, detail="FreeCADCmd executable not found")


def _safe_basename(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name).strip("_")
    return cleaned[:80] or "irving_freecad_model"


def _find_freecad_gui(freecadcmd: Path) -> Optional[Path]:
    sibling = freecadcmd.with_name("freecad.exe")
    if sibling.exists():
        return sibling
    return None


def _parse_runner_json(stdout: str) -> dict:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {}


@app.get("/health")
def health():
    freecadcmd = _find_freecadcmd()
    freecad_gui = _find_freecad_gui(freecadcmd)
    return {
        "status": "ok",
        "freecadcmd": str(freecadcmd),
        "freecad_gui": str(freecad_gui) if freecad_gui else None,
        "runner": str(RUNNER_PATH),
    }


@app.post("/freecad/execute")
def execute_freecad(req: ExecuteFreeCADRequest):
    freecadcmd = _find_freecadcmd()
    freecad_gui = _find_freecad_gui(freecadcmd)
    run_id = uuid.uuid4().hex
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    model_basename = _safe_basename(req.model_basename)
    macro_path = run_dir / f"{model_basename}.py"
    macro_path.write_text(req.macro_source, encoding="utf-8")

    export_formats = sorted({fmt.lower() for fmt in req.export_formats} | {"fcstd"})
    allowed_formats = {"fcstd", "step", "stl", "svg"}
    if any(fmt not in allowed_formats for fmt in export_formats):
        raise HTTPException(status_code=400, detail=f"Supported export formats: {sorted(allowed_formats)}")

    cmd = [
        str(freecadcmd),
        str(RUNNER_PATH),
        "--pass",
        str(macro_path),
        str(run_dir),
        model_basename,
        json.dumps(export_formats),
    ]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=req.timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="FreeCAD bridge timed out")

    runner_data = _parse_runner_json(completed.stdout)
    if completed.returncode != 0 or not runner_data.get("success"):
        detail = runner_data.get("error") or completed.stderr.strip() or completed.stdout.strip() or "FreeCAD execution failed"
        raise HTTPException(status_code=502, detail=detail)

    artifacts = []
    for artifact in runner_data.get("artifacts", []):
        path = Path(artifact["path"])
        if path.exists():
            artifacts.append({
                "name": artifact["name"],
                "type": artifact["type"],
                "size": path.stat().st_size,
                "url": f"http://127.0.0.1:8765/artifacts/{run_id}/{artifact['name']}",
            })

    gui_opened = False
    fcstd_artifact = next((a for a in artifacts if a["type"] == "fcstd"), None)
    if req.open_in_gui and freecad_gui and fcstd_artifact:
        fcstd_path = run_dir / fcstd_artifact["name"]
        subprocess.Popen([str(freecad_gui), str(fcstd_path)])
        gui_opened = True

    return {
        "success": True,
        "run_id": run_id,
        "artifacts": artifacts,
        "opened_in_gui": gui_opened,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


@app.get("/artifacts/{run_id}/{filename}")
def get_artifact(run_id: str, filename: str):
    path = (RUNS_DIR / run_id / filename).resolve()
    run_root = (RUNS_DIR / run_id).resolve()
    if run_root not in path.parents or not path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path)
