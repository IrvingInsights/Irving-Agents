import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import FreeCAD as App
import Import
import Mesh
import Part
import TechDraw
from PySide import QtCore


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


def _find_techdraw_template() -> Path:
    resource_dir = Path(App.getResourceDir())
    candidates = [
        resource_dir / "Mod" / "TechDraw" / "Templates" / "A3_Landscape_blank.svg",
        resource_dir / "Mod" / "TechDraw" / "Templates" / "A4_Landscape_blank.svg",
        resource_dir / "Mod" / "TechDraw" / "Templates" / "A4_Landscape_TD.svg",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError("Could not find a TechDraw SVG template")


def _bbox_size(objects):
    shapes = [obj.Shape for obj in objects if hasattr(obj, "Shape") and not obj.Shape.isNull()]
    if not shapes:
        raise RuntimeError("No exportable shapes were created for TechDraw output")
    compound = Part.makeCompound(shapes)
    return compound.BoundBox, compound


def _fit_scale(page_width, page_height, bbox):
    usable_width = max(page_width - 80.0, 50.0)
    usable_height = max(page_height - 60.0, 50.0)

    x_len = max(float(bbox.XLength), 1.0)
    y_len = max(float(bbox.YLength), 1.0)
    z_len = max(float(bbox.ZLength), 1.0)

    top_width = x_len
    top_height = y_len
    front_width = x_len
    front_height = z_len
    right_width = y_len
    right_height = z_len

    two_column_width = top_width + right_width + 30.0
    two_row_height = top_height + front_height + 30.0

    return max(min(usable_width / max(two_column_width, 1.0), usable_height / max(two_row_height, 1.0)), 0.02)


def _make_draw_view(doc, page, label, source, direction, x_dir, x_pos, y_pos, scale):
    view = doc.addObject("TechDraw::DrawViewPart", label)
    page.addView(view)
    view.Source = source
    view.Direction = direction
    view.XDirection = x_dir
    view.Scale = scale
    view.X = x_pos
    view.Y = y_pos
    view.Caption = label
    return view


def _create_techdraw_page(doc, objects):
    bbox, compound = _bbox_size(objects)
    template_path = _find_techdraw_template()

    page = doc.addObject("TechDraw::DrawPage", "DrawingSheet")
    template = doc.addObject("TechDraw::DrawSVGTemplate", "DrawingTemplate")
    template.Template = str(template_path)
    page.Template = template
    doc.recompute()

    page_width = float(page.PageWidth)
    page_height = float(page.PageHeight)
    scale = _fit_scale(page_width, page_height, bbox)

    center_x = page_width * 0.36
    center_y = page_height * 0.44
    spacing_x = 30.0 + max(bbox.YLength * scale * 0.5, 22.0)
    spacing_y = 30.0 + max(bbox.YLength * scale * 0.5, 22.0)

    source = list(objects)
    views = {
        "Top": _make_draw_view(
            doc, page, "Top", source, App.Vector(0, 0, 1), App.Vector(1, 0, 0),
            center_x, center_y + spacing_y, scale,
        ),
        "Front": _make_draw_view(
            doc, page, "Front", source, App.Vector(0, -1, 0), App.Vector(1, 0, 0),
            center_x, center_y, scale,
        ),
        "Right": _make_draw_view(
            doc, page, "Right", source, App.Vector(1, 0, 0), App.Vector(0, 0, 1),
            center_x + spacing_x, center_y, scale,
        ),
        "Iso": _make_draw_view(
            doc, page, "Iso", source, App.Vector(1, -1, 1), App.Vector(1, 1, 0),
            page_width * 0.77, page_height * 0.70, scale * 0.85,
        ),
    }

    for view in views.values():
        view.CoarseView = False
        view.HardHidden = False

    doc.recompute()

    return {
        "page": page,
        "template": template,
        "compound": compound,
        "views": views,
        "scale": scale,
    }


def _wait_for_techdraw_views(views, timeout_s=12.0):
    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
    deadline = time.monotonic() + timeout_s
    last_counts = {}

    while time.monotonic() < deadline:
        QtCore.QCoreApplication.processEvents()
        QtCore.QThread.msleep(200)
        QtCore.QCoreApplication.processEvents()

        counts = {}
        all_ready = True
        any_edges = False
        for name, view in views.items():
            try:
                visible_edges = len(view.getVisibleEdges())
            except Exception:
                visible_edges = 0
            counts[name] = visible_edges
            any_edges = any_edges or visible_edges > 0
            all_ready = all_ready and visible_edges > 0

        last_counts = counts
        if all_ready:
            return counts

        if any_edges and time.monotonic() + 0.75 >= deadline:
            break

    return last_counts

def _export_objects(objects, output_dir: Path, model_basename: str, export_formats, drawing_bundle):
    artifacts = []
    svg_artifacts = []

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
        directions = {
            "top": App.Vector(0, 0, 1),
            "front": App.Vector(0, -1, 0),
            "right": App.Vector(1, 0, 0),
            "iso": App.Vector(1, -1, 1),
        }
        for name, direction in directions.items():
            svg_group = TechDraw.projectToSVG(drawing_bundle["compound"], direction)
            svg_doc = (
                f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
                f'width="{drawing_bundle["page"].PageWidth}mm" '
                f'height="{drawing_bundle["page"].PageHeight}mm" '
                f'viewBox="0 0 {drawing_bundle["page"].PageWidth} {drawing_bundle["page"].PageHeight}">'
                f"{svg_group}</svg>"
            )
            svg_path = output_dir / f"{model_basename}_{name}.svg"
            svg_path.write_text(svg_doc, encoding="utf-8")
            artifact = {"name": svg_path.name, "path": str(svg_path), "type": "svg"}
            artifacts.append(artifact)
            svg_artifacts.append(artifact)

        html_path = output_dir / f"{model_basename}_sheet.html"
        html_doc = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{model_basename} Drawing Sheet</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #111;
      --muted: #666;
      --line: #d6d6d6;
      --paper: #f7f6f2;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", Arial, sans-serif;
      color: var(--ink);
      background: #d9d6cf;
      padding: 24px;
    }}
    .sheet {{
      max-width: 1400px;
      margin: 0 auto;
      background: var(--paper);
      border: 1px solid #bfb9aa;
      box-shadow: 0 18px 60px rgba(0,0,0,.18);
      padding: 28px 28px 18px;
    }}
    .titlebar {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      border-bottom: 2px solid var(--ink);
      padding-bottom: 10px;
      margin-bottom: 18px;
      gap: 20px;
    }}
    .title {{
      font-size: 24px;
      font-weight: 700;
      letter-spacing: .06em;
      text-transform: uppercase;
    }}
    .subtitle {{
      font-size: 12px;
      color: var(--muted);
      letter-spacing: .08em;
      text-transform: uppercase;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 1.2fr 0.9fr;
      gap: 18px;
    }}
    .stack {{
      display: grid;
      gap: 18px;
    }}
    figure {{
      margin: 0;
      border: 1px solid var(--line);
      background: white;
      min-height: 240px;
      display: flex;
      flex-direction: column;
    }}
    figcaption {{
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .08em;
      text-transform: uppercase;
      color: var(--muted);
      padding: 10px 12px 0;
    }}
    img {{
      width: 100%;
      height: 100%;
      object-fit: contain;
      padding: 10px 12px 14px;
    }}
    .footnote {{
      margin-top: 16px;
      padding-top: 12px;
      border-top: 1px solid var(--line);
      font-size: 12px;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <div class="sheet">
    <div class="titlebar">
      <div>
        <div class="title">{model_basename.replace('_', ' ')}</div>
        <div class="subtitle">FreeCAD-generated reference drawing sheet</div>
      </div>
      <div class="subtitle">Top / Front / Right / Iso</div>
    </div>
    <div class="grid">
      <div class="stack">
        <figure>
          <figcaption>Top</figcaption>
          <img src="{model_basename}_top.svg" alt="Top view">
        </figure>
        <figure>
          <figcaption>Front</figcaption>
          <img src="{model_basename}_front.svg" alt="Front view">
        </figure>
      </div>
      <div class="stack">
        <figure>
          <figcaption>Right</figcaption>
          <img src="{model_basename}_right.svg" alt="Right view">
        </figure>
        <figure>
          <figcaption>Iso</figcaption>
          <img src="{model_basename}_iso.svg" alt="Isometric view">
        </figure>
      </div>
    </div>
    <div class="footnote">FreeCAD opens the model document. This sheet is the assembled 2D reference output for the same run.</div>
  </div>
</body>
</html>
"""
        html_path.write_text(html_doc, encoding="utf-8")
        artifacts.append({"name": html_path.name, "path": str(html_path), "type": "html"})

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
    drawing_bundle = _create_techdraw_page(doc, objects)
    doc.recompute()
    edge_counts = _wait_for_techdraw_views(drawing_bundle["views"])
    if not any(count > 0 for count in edge_counts.values()):
        raise RuntimeError(f"TechDraw views did not finish rendering before export: {edge_counts}")
    doc.recompute()

    artifacts = _export_objects(objects, output_dir, model_basename, export_formats, drawing_bundle)
    print(json.dumps({"success": True, "artifacts": artifacts}))


try:
    main()
except Exception as exc:
    print(json.dumps({"success": False, "error": str(exc)}))
    raise
