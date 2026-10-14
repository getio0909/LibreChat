"""Deterministic 3D solid verification, clash detection, BOM reconciliation, and CAD export helpers for FreeCAD."""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import date
from typing import Any

if "/opt/freecad/lib" not in sys.path and os.path.isdir("/opt/freecad/lib"):
    sys.path.append("/opt/freecad/lib")

try:
    import FreeCAD
    import Part
except ImportError:
    FreeCAD = None  # type: ignore[assignment]
    Part = None  # type: ignore[assignment]

_CONTAINER_TYPES = {
    "App::DocumentObjectGroup",
    "App::Part",
    "Part::Compound",
}

_FEATURE_CONSUMERS = {
    "Part::Cut",
    "Part::Fuse",
    "Part::Common",
    "Part::MultiFuse",
    "Part::MultiCommon",
    "Part::Fillet",
    "Part::Chamfer",
    "Part::Thickness",
    "Part::Offset",
    "Part::Offset2D",
}


def get_leaf_solids(doc: Any = None) -> list[Any]:
    """Return final visible leaf solid objects, excluding parent groups, compounds, and consumed boolean inputs."""
    doc = doc or FreeCAD.ActiveDocument
    if doc is None:
        raise ValueError("No active FreeCAD document found.")

    consumed: set[str] = set()
    for obj in doc.Objects:
        if obj.TypeId in _FEATURE_CONSUMERS:
            for child in getattr(obj, "OutList", []) or []:
                if getattr(child, "Name", None):
                    consumed.add(child.Name)

    leaves: list[Any] = []
    for obj in doc.Objects:
        if obj.Name in consumed:
            continue
        if obj.TypeId in _CONTAINER_TYPES:
            continue
        if obj.TypeId.startswith("Sketcher::") or obj.TypeId.startswith("TechDraw::"):
            continue
        vo = getattr(obj, "ViewObject", None)
        if vo is not None and getattr(vo, "Visibility", True) is False:
            continue
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull():
            continue
        solids = getattr(shape, "Solids", None) or []
        if not solids or getattr(shape, "Volume", 0.0) <= 1e-3:
            continue
        leaves.append(obj)
    return leaves


def _boxes_overlap(bb1: Any, bb2: Any, eps: float = 0.05) -> bool:
    return (
        bb1.XMax - bb2.XMin > eps
        and bb2.XMax - bb1.XMin > eps
        and bb1.YMax - bb2.YMin > eps
        and bb2.YMax - bb1.YMin > eps
        and bb1.ZMax - bb2.ZMin > eps
        and bb2.ZMax - bb1.ZMin > eps
    )


def _normalize_part_family(label: str) -> str:
    cleaned = re.sub(r"[_\-\s]*(L|R|Left|Right|Top|Bottom|Upper|Lower|\d+)$", "", label, flags=re.IGNORECASE)
    return cleaned.strip() or label.strip()


def export_orthographic_dxf(leaf_solids: list[Any], dxf_path: str) -> str:
    """Export 2D orthographic projections (Front, Side, Plan) of the compound shape to DXF."""
    shapes = [obj.Shape for obj in leaf_solids if getattr(obj, "Shape", None) and not obj.Shape.isNull()]
    if not shapes:
        raise ValueError("Cannot export DXF: no valid solid shapes.")
    compound = Part.makeCompound(shapes)
    try:
        import TechDraw

        # Front view projection along -Y (looking at XZ plane)
        dxf_text = TechDraw.projectToDXF(compound, FreeCAD.Vector(0, -1, 0))
        with open(dxf_path, "w", encoding="utf-8") as fh:
            fh.write(dxf_text)
        return "TechDraw.projectToDXF"
    except Exception:
        import importDXF

        importDXF.export(leaf_solids, dxf_path)
        return "importDXF.export"


def project_view(
    doc: Any = None,
    view: str = "front",
    deflection_mm: float = 1.0,
) -> dict[str, Any]:
    """Project 3D leaf solids (including curved, sculpted, or irregular geometry) into canonical 2D model-mm coordinates [0..W, 0..H] for compile_drawing_sheet."""
    leaves = get_leaf_solids(doc)
    if not leaves:
        raise ValueError("No leaf solids found in document.")
    compound = Part.makeCompound([obj.Shape for obj in leaves])
    cb = compound.BoundBox
    v = view.lower().strip()
    if v in ("front", "xz"):
        w_mm, h_mm = round(cb.XLength, 2), round(cb.ZLength, 2)
        map_pt = lambda pt: (round(pt.x - cb.XMin, 2), round(cb.ZMax - pt.z, 2))
    elif v in ("side", "right", "left", "yz"):
        w_mm, h_mm = round(cb.YLength, 2), round(cb.ZLength, 2)
        map_pt = lambda pt: (round(pt.y - cb.YMin, 2), round(cb.ZMax - pt.z, 2))
    elif v in ("top", "plan", "xy"):
        w_mm, h_mm = round(cb.XLength, 2), round(cb.YLength, 2)
        map_pt = lambda pt: (round(pt.x - cb.XMin, 2), round(pt.y - cb.YMin, 2))
    else:
        raise ValueError(f"Unsupported view {view!r}; use 'front', 'side', or 'top'.")

    seen_segments: set[tuple[tuple[float, float], ...]] = set()
    path_cmds: list[str] = []
    for obj in leaves:
        for edge in getattr(obj.Shape, "Edges", []) or []:
            if edge.Length < 0.5:
                continue
            curve_type = getattr(getattr(edge, "Curve", None), "TypeId", "")
            if curve_type == "Part::GeomLine" and len(edge.Vertexes) >= 2:
                pts = [map_pt(edge.Vertexes[0].Point), map_pt(edge.Vertexes[-1].Point)]
            else:
                raw_pts = edge.discretize(Deflection=max(0.2, float(deflection_mm)))
                pts = [map_pt(p) for p in raw_pts]
            if len(pts) < 2 or pts[0] == pts[-1] and len(pts) == 2:
                continue
            canon = tuple(pts) if pts[0] <= pts[-1] else tuple(reversed(pts))
            if canon in seen_segments:
                continue
            seen_segments.add(canon)
            coords = " L ".join(f"{px:g} {py:g}" for px, py in pts)
            path_cmds.append(f"M {coords}")

    return {
        "view": v,
        "width_model_mm": w_mm,
        "height_model_mm": h_mm,
        "edge_count": len(path_cmds),
        "paths": [{"d": " ".join(path_cmds), "style": "solid"}] if path_cmds else [],
    }


def audit_document(
    doc: Any = None,
    *,
    expected_bbox: tuple[float, float, float] | list[float] | None = None,
    step_path: str | None = None,
    dxf_path: str | None = None,
    cutlist_path: str | None = None,
    bom_rows: list[dict[str, Any]] | None = None,
    default_material: str = "北美黑胡桃木实木 / 多层实木贴皮",
    default_edge_banding: str = "四边 1.0mm 同色木皮封边（见光面木蜡油）",
    overlap_tolerance_mm3: float = 1.0,
    bbox_tolerance_mm: float = 1.5,
    strict: bool = False,
) -> dict[str, Any]:
    """Audit 3D solids for validity, pairwise boolean clashes, envelope dimensions, and BOM reconciliation."""
    doc = doc or FreeCAD.ActiveDocument
    if doc is None:
        raise ValueError("No active FreeCAD document found.")
    doc.recompute()

    leaves = get_leaf_solids(doc)
    invalid_solids: list[dict[str, Any]] = []
    for obj in leaves:
        if not obj.Shape.isValid():
            invalid_solids.append({"name": obj.Name, "label": obj.Label, "reason": "Shape.isValid() is False"})

    if leaves:
        compound = Part.makeCompound([obj.Shape for obj in leaves])
        cb = compound.BoundBox
        overall_bbox = {
            "x_min": round(cb.XMin, 2),
            "y_min": round(cb.YMin, 2),
            "z_min": round(cb.ZMin, 2),
            "x_max": round(cb.XMax, 2),
            "y_max": round(cb.YMax, 2),
            "z_max": round(cb.ZMax, 2),
            "width_x_mm": round(cb.XLength, 2),
            "depth_y_mm": round(cb.YLength, 2),
            "height_z_mm": round(cb.ZLength, 2),
        }
    else:
        overall_bbox = {
            "x_min": 0.0,
            "y_min": 0.0,
            "z_min": 0.0,
            "x_max": 0.0,
            "y_max": 0.0,
            "z_max": 0.0,
            "width_x_mm": 0.0,
            "depth_y_mm": 0.0,
            "height_z_mm": 0.0,
        }

    bbox_errors: list[str] = []
    if expected_bbox is not None and len(expected_bbox) == 3:
        exp_w, exp_d, exp_h = (float(expected_bbox[0]), float(expected_bbox[1]), float(expected_bbox[2]))
        dx = abs(overall_bbox["width_x_mm"] - exp_w)
        dy = abs(overall_bbox["depth_y_mm"] - exp_d)
        dz = abs(overall_bbox["height_z_mm"] - exp_h)
        if dx > bbox_tolerance_mm or dy > bbox_tolerance_mm or dz > bbox_tolerance_mm:
            bbox_errors.append(
                f"Envelope mismatch: modeled ({overall_bbox['width_x_mm']}, {overall_bbox['depth_y_mm']}, "
                f"{overall_bbox['height_z_mm']}) mm vs expected ({exp_w}, {exp_d}, {exp_h}) mm"
            )

    clashes: list[dict[str, Any]] = []
    for i in range(len(leaves)):
        a = leaves[i]
        bba = a.Shape.BoundBox
        for j in range(i + 1, len(leaves)):
            b = leaves[j]
            bbb = b.Shape.BoundBox
            if not _boxes_overlap(bba, bbb):
                continue
            try:
                common = a.Shape.common(b.Shape)
                vol = float(getattr(common, "Volume", 0.0))
                if vol > overlap_tolerance_mm3:
                    cbb = common.BoundBox
                    clashes.append(
                        {
                            "part_a": a.Label or a.Name,
                            "part_b": b.Label or b.Name,
                            "overlap_volume_mm3": round(vol, 2),
                            "overlap_dims_mm": [
                                round(cbb.XLength, 2),
                                round(cbb.YLength, 2),
                                round(cbb.ZLength, 2),
                            ],
                        }
                    )
            except Exception as exc:
                clashes.append(
                    {
                        "part_a": a.Label or a.Name,
                        "part_b": b.Label or b.Name,
                        "error": str(exc),
                    }
                )

    # Group leaf solids for BOM / Cut-list verification
    grouped: dict[tuple[str, float, float, float], dict[str, Any]] = {}
    for obj in leaves:
        bb = obj.Shape.BoundBox
        dims_sorted = sorted([round(bb.XLength, 1), round(bb.YLength, 1), round(bb.ZLength, 1)], reverse=True)
        grain_len, cross_w, thick = dims_sorted[0], dims_sorted[1], dims_sorted[2]
        family = _normalize_part_family(obj.Label or obj.Name)
        key = (family, grain_len, cross_w, thick)
        if key not in grouped:
            grouped[key] = {
                "Part_Name": family,
                "Category": "结构木作件",
                "Material": default_material,
                "Grain_Length_mm": grain_len,
                "Cross_Width_mm": cross_w,
                "Thickness_mm": thick,
                "Qty": 0,
                "Edge_Banding": default_edge_banding,
                "Notes": f"3D实测净尺寸 {grain_len:g}×{cross_w:g}×{thick:g} mm",
                "instances": [],
            }
        grouped[key]["Qty"] += 1
        grouped[key]["instances"].append(obj.Label or obj.Name)

    final_bom: list[dict[str, Any]] = []
    if bom_rows:
        seen_keys: set[tuple[str, str, str, str]] = set()
        for idx, row in enumerate(bom_rows, start=1):
            pid = str(row.get("Part_ID") or f"P{idx:02d}")
            pname = str(row.get("Part_Name") or row.get("Name") or f"Part_{idx}")
            glen = str(row.get("Grain_Length_mm") or row.get("Length_mm") or row.get("Length") or "")
            cwid = str(row.get("Cross_Width_mm") or row.get("Width_mm") or row.get("Width") or "")
            thk = str(row.get("Thickness_mm") or row.get("Thickness") or "")
            dedup_key = (pname, glen, cwid, thk)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            final_bom.append(
                {
                    "Part_ID": pid,
                    "Part_Name": pname,
                    "Category": str(row.get("Category") or "柜体构件"),
                    "Material": str(row.get("Material") or default_material),
                    "Grain_Length_mm": glen,
                    "Cross_Width_mm": cwid,
                    "Thickness_mm": thk,
                    "Qty": int(row.get("Qty") or 1),
                    "Edge_Banding": str(row.get("Edge_Banding") or default_edge_banding),
                    "Notes": str(row.get("Notes") or "净尺寸（开料另加余量）"),
                }
            )
    else:
        for idx, item in enumerate(grouped.values(), start=1):
            final_bom.append(
                {
                    "Part_ID": f"P{idx:02d}",
                    "Part_Name": item["Part_Name"],
                    "Category": item["Category"],
                    "Material": item["Material"],
                    "Grain_Length_mm": item["Grain_Length_mm"],
                    "Cross_Width_mm": item["Cross_Width_mm"],
                    "Thickness_mm": item["Thickness_mm"],
                    "Qty": item["Qty"],
                    "Edge_Banding": item["Edge_Banding"],
                    "Notes": item["Notes"],
                }
            )

    exports: dict[str, Any] = {}
    ok = len(leaves) > 0 and len(invalid_solids) == 0 and len(clashes) == 0 and len(bbox_errors) == 0

    if step_path and leaves:
        os.makedirs(os.path.dirname(step_path) or "/tmp/cad", exist_ok=True)
        Part.export(leaves, step_path)
        exports["step_path"] = step_path
        exports["step_bytes"] = os.path.getsize(step_path)

    if dxf_path and leaves:
        os.makedirs(os.path.dirname(dxf_path) or "/tmp/cad", exist_ok=True)
        method = export_orthographic_dxf(leaves, dxf_path)
        exports["dxf_path"] = dxf_path
        exports["dxf_bytes"] = os.path.getsize(dxf_path)
        exports["dxf_method"] = method

    if cutlist_path:
        os.makedirs(os.path.dirname(cutlist_path) or "/tmp/cad", exist_ok=True)
        fieldnames = [
            "Part_ID",
            "Part_Name",
            "Category",
            "Material",
            "Grain_Length_mm",
            "Cross_Width_mm",
            "Thickness_mm",
            "Qty",
            "Edge_Banding",
            "Notes",
        ]
        with open(cutlist_path, "w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in final_bom:
                writer.writerow(row)
        exports["cutlist_path"] = cutlist_path
        exports["cutlist_bytes"] = os.path.getsize(cutlist_path)

    report = {
        "ok": ok,
        "leaf_solid_count": len(leaves),
        "unique_part_groups": len(grouped),
        "bom_row_count": len(final_bom),
        "bom_total_qty": sum(int(r["Qty"]) for r in final_bom),
        "overall_bbox_mm": overall_bbox,
        "bbox_errors": bbox_errors,
        "invalid_solids": invalid_solids,
        "clash_count": len(clashes),
        "clashes": clashes,
        "exports": exports,
    }
    if strict and not ok:
        raise ValueError(f"CAD 3D audit failed: {json.dumps(report, ensure_ascii=False)}")
    return report


def extend_notes(notes: tuple[Any, ...]) -> tuple[Any, ...]:
    """Attach the production CAD zero-clash & multi-sheet drafting guidance note to usage_guidance.NOTES."""
    try:
        from mcp_agent_notes import Note, NoteKind, Priority

        prod_note = Note(
            id="production-cad-audit-and-sheets",
            added=date(2026, 9, 25),
            priority=Priority.CRITICAL,
            kind=NoteKind.STRATEGY,
            summary=(
                "Always run cad_check.audit_document(...) in execute_python to verify "
                "0 solid clashes, exact envelope dimensions, and deduplicated BOM/STEP/DXF "
                "exports before generating production drawing sheets with design_tools "
                "compile_drawing_sheet and assemble_drawing_pdf."
            ),
            detail=(
                "1) In execute_python, after creating/updating all 3D parts and saving the .FCStd "
                "file, call:\n"
                "   import cad_check, json\n"
                "   report = cad_check.audit_document(\n"
                "       doc,\n"
                "       expected_bbox=(W, D, H),\n"
                "       step_path='/tmp/cad/<name>.step',\n"
                "       dxf_path='/tmp/cad/<name>_2d.dxf',\n"
                "       cutlist_path='/tmp/cad/<name>_cutlist.csv',\n"
                "       bom_rows=[...],  # optional explicit BOM rows\n"
                "   )\n"
                "   print(json.dumps(report, ensure_ascii=False))\n"
                "2) If report['clash_count'] > 0 or report['bbox_errors'] is non-empty, fix the "
                "part coordinates/dimensions in FreeCAD, recompute, and re-run audit_document "
                "until report['ok'] is True.\n"
                "3) For curved, irregular, sculptural, or high-detail 3D shapes (e.g. moon-gates, "
                "flared eaves, lotus pedestals, cabriole legs, fillets, or complex cutouts), call "
                "cad_check.project_view(doc, view='front'|'side'|'top') to extract normalized "
                "model-space SVG paths (proj['paths']) and pass them into compile_drawing_sheet "
                "views[].paths along with radial_dims and custom_profile_detail.\n"
                "4) For multi-sheet factory drawings (ZG-01 General Arrangement, ZG-02 Carcass "
                "Dimensions & Sections, ZG-03 Part Fabrication & 1:1 Joinery Details, ZG-04 BOM, "
                "Hardware Schedule & Technical Notes), call design_tools compile_drawing_sheet "
                "for each sheet (which automatically verifies dimension-chain closure) and "
                "combine them with assemble_drawing_pdf."
            ),
            tags=(),
            addresses=(
                "production drawing",
                "clash detection",
                "cutlist",
                "STEP export",
                "DXF export",
                "factory drawing set",
            ),
        )
        return (prod_note,) + tuple(notes)
    except Exception:
        return notes

