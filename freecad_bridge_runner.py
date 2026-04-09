import importlib.util
import json
import os
import sys
from pathlib import Path

import FreeCAD as App
import Import
import Mesh
import Part
import TechDraw


def _load_module(module_path: str):
    spec = importlib.util.spec_from_file_location("irving_freecad_macro", module_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"Could not load FreeCAD macro: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_objects(result, doc):
    if result is None:
        return list(doc.Objects)
    if isinstance(result, (list, tuple, set)):
        return [obj for obj in result if hasattr(obj, "TypeId")]
    if hasattr(result, "TypeId"):
        return [result]
    raise RuntimeError("build_model(doc) must return a FreeCAD object, a list of objects, or None")


def _ensure_objects_visible(objects):
    for obj in objects:
        view_obj = getattr(obj, "ViewObject", None)
        if view_obj and hasattr(view_obj, "Visibility"):
            view_obj.Visibility = True


def _export_objects(objects, output_dir: Path, model_basename: str, export_formats):
    artifacts = []

    fcstd_path = output_dir / f"{model_basename}.FCStd"
    App.ActiveDocument.saveAs(str(fcstd_path))
    artifacts.append({"name": fcstd_path.name, "path": str(fcstd_path), "type": "fcstd"})

    if "step" in export_formats:
        step_path = output_dir / f"{model_basename}.step"
        Import.export(objects, str(step_path))
        artifacts.append({"name": step_path.name, "path": str(step_path), "type": "step"})

    if "stl" in export_formats:
        stl_path = output_dir / f"{model_basename}.stl"
        Mesh.export(objects, str(stl_path))
        artifacts.append({"name": stl_path.name, "path": str(stl_path), "type": "stl"})

    if "svg" in export_formats:
        compound = Part.makeCompound([obj.Shape for obj in objects if hasattr(obj, "Shape")])
        views = [
            ("top", App.Vector(0, 0, 1)),
            ("front", App.Vector(0, -1, 0)),
            ("right", App.Vector(1, 0, 0)),
        ]
        for name, direction in views:
            svg_group = TechDraw.projectToSVG(compound, direction)
            svg_doc = (
                '<svg xmlns="http://www.w3.org/2000/svg" version="1.1">'
                f"{svg_group}</svg>"
            )
            svg_path = output_dir / f"{model_basename}_{name}.svg"
            svg_path.write_text(svg_doc, encoding="utf-8")
            artifacts.append({"name": svg_path.name, "path": str(svg_path), "type": "svg"})

    return artifacts


def main():
    argv = sys.argv[1:]
    if "--pass" in argv:
        argv = argv[argv.index("--pass") + 1:]
    else:
        argv = argv[1:]
    if len(argv) != 4:
        raise SystemExit("Usage: freecad_bridge_runner.py <macro_path> <output_dir> <model_basename> <export_formats_json>")

    macro_path = Path(argv[0]).resolve()
    output_dir = Path(argv[1]).resolve()
    model_basename = argv[2]
    export_formats = set(json.loads(argv[3]))

    module = _load_module(str(macro_path))
    build_model = getattr(module, "build_model", None)
    if not callable(build_model):
        raise RuntimeError("Macro must define build_model(doc)")

    output_dir.mkdir(parents=True, exist_ok=True)

    doc = App.newDocument(model_basename)
    result = build_model(doc)
    doc.recompute()

    objects = _normalize_objects(result, doc)
    if not objects:
        raise RuntimeError("No FreeCAD objects were created for export")
    _ensure_objects_visible(objects)

    artifacts = _export_objects(objects, output_dir, model_basename, export_formats)
    print(json.dumps({"success": True, "artifacts": artifacts}))


try:
    main()
except Exception as exc:
    print(json.dumps({"success": False, "error": str(exc)}))
    raise
