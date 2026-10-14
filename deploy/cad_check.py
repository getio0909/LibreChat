"""Deterministic 3D solid verification, clash detection, BOM reconciliation, and CAD export helpers for FreeCAD."""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import date
from typing import Any

def _fc() -> tuple[Any, Any]:
    if "/opt/freecad/lib" not in sys.path and os.path.isdir("/opt/freecad/lib"):
        sys.path.append("/opt/freecad/lib")
    import FreeCAD  # type: ignore[import-not-found]
    import Part  # type: ignore[import-not-found]

    return FreeCAD, Part


_CONTAINER_TYPES = {
    "App::DocumentObjectGroup",
    "App::Part",
    "Part::Compound",
}

_FEATURE_CONSUMERS = {
    "PartDesign::Body",
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
    "Part::Extrusion",
    "Part::Revolution",
    "Part::Loft",
    "Part::Sweep",
    "Part::Slice",
    "Part::CompoundFilter",
}


def _mark_consumed(item: Any, consumed: set[str]) -> None:
    if item is None:
        return
    if isinstance(item, (list, tuple, set)):
        for sub in item:
            _mark_consumed(sub, consumed)
        return
    name = getattr(item, "Name", None)
    if name and name not in consumed:
        consumed.add(name)
        if getattr(item, "TypeId", "") in _CONTAINER_TYPES:
            _mark_consumed(getattr(item, "OutList", None), consumed)
            _mark_consumed(getattr(item, "Group", None), consumed)


def _get_consumed_names(doc: Any) -> set[str]:
    consumed: set[str] = set()
    for obj in doc.Objects:
        if obj.TypeId in _FEATURE_CONSUMERS:
            if obj.TypeId == "PartDesign::Body":
                _mark_consumed(getattr(obj, "OutList", None), consumed)
                _mark_consumed(getattr(obj, "Group", None), consumed)
            for prop in ("Base", "Tool", "Shapes", "BaseFeature", "Profile", "Sections"):
                _mark_consumed(getattr(obj, prop, None), consumed)
    return consumed


def _object_shape(Part: Any, obj: Any) -> Any:
    try:
        sh = Part.getShape(obj)
        if sh is not None and not sh.isNull():
            return sh
    except Exception:
        pass
    return getattr(obj, "Shape", None)


def get_leaf_solids(doc: Any = None) -> list[Any]:
    """Return final visible leaf solid objects, excluding parent groups, compounds, PartDesign internal features, and consumed boolean inputs."""
    FreeCAD, Part = _fc()
    doc = doc or FreeCAD.ActiveDocument
    if doc is None:
        raise ValueError("No active FreeCAD document found.")

    consumed = _get_consumed_names(doc)
    leaves: list[Any] = []
    for obj in doc.Objects:
        if obj.Name in consumed:
            continue
        if obj.TypeId in _CONTAINER_TYPES:
            continue
        if obj.TypeId.startswith("Sketcher::") or obj.TypeId.startswith("TechDraw::"):
            continue
        if getattr(obj, "Visibility", True) is False:
            continue
        vo = getattr(obj, "ViewObject", None)
        if vo is not None and getattr(vo, "Visibility", True) is False:
            continue
        shape = _object_shape(Part, obj)
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
    cleaned = re.sub(
        r"(?:[_\-\s]+(?:L|R|Left|Right|Top|Bottom|Upper|Lower|\d+)|\d{2,})$",
        "",
        label,
        flags=re.IGNORECASE,
    )
    return cleaned.strip() or label.strip()


def _resolve_cad_path(raw_path: str) -> str:
    if os.path.isabs(raw_path):
        return raw_path
    return os.path.join("/tmp/cad", raw_path)


def _parse_qty(val: Any) -> int:
    if isinstance(val, (int, float)):
        return max(1, int(round(val)))
    m = re.search(r"\d+", str(val or ""))
    return max(1, int(m.group(0))) if m else 1


def _wrap_dxf_entities(raw_dxf: str) -> str:
    stripped = raw_dxf.strip()
    if stripped.startswith("0\nSECTION") or stripped.startswith("0\r\nSECTION"):
        return raw_dxf
    return (
        "0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1009\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n"
        "0\nSECTION\n2\nENTITIES\n"
        f"{stripped}\n"
        "0\nENDSEC\n0\nEOF\n"
    )


def export_orthographic_dxf(doc_or_leaves: Any, dxf_path: str) -> str:
    """Export upright 3-view 2D Third-Angle orthographic projections (Front, Right Side, Top Plan) to a valid AutoCAD DXF."""
    FreeCAD, Part = _fc()
    leaf_solids = (
        list(doc_or_leaves)
        if isinstance(doc_or_leaves, (list, tuple, set))
        else get_leaf_solids(doc_or_leaves)
    )
    shapes = [_object_shape(Part, obj) for obj in leaf_solids]
    shapes = [s for s in shapes if s is not None and not s.isNull()]
    if not shapes:
        raise ValueError("Cannot export DXF: no valid solid shapes.")
    dxf_path = _resolve_cad_path(dxf_path)
    os.makedirs(os.path.dirname(dxf_path) or "/tmp/cad", exist_ok=True)
    compound = Part.makeCompound(shapes)
    try:
        import TechDraw

        cb = compound.BoundBox
        gap = max(120.0, round(max(cb.XLength, cb.YLength, cb.ZLength) * 0.12, 1))
        base = compound.copy()
        base.translate(FreeCAD.Vector(-cb.XMin, -cb.YMin, -cb.ZMin))

        # Front view in XY plane: [0..W, 0..H]
        front_3d = base.copy()
        front_3d.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(1, 0, 0), -90)

        # Right side view in XY plane (Third-Angle: to the right of Front): [W+gap .. W+gap+D, 0..H]
        side_3d = base.copy()
        side_3d.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 1, 0), -90)
        side_3d.rotate(FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(0, 0, 1), -90)
        side_3d.translate(FreeCAD.Vector(cb.XLength + gap, 0, 0))

        # Top/Plan view in XY plane (Third-Angle: above Front): [0..W, H+gap .. H+gap+D]
        top_3d = base.copy()
        top_3d.translate(FreeCAD.Vector(0, cb.ZLength + gap, 0))

        multi_view = Part.makeCompound([front_3d, side_3d, top_3d])
        dxf_text = _wrap_dxf_entities(TechDraw.projectToDXF(multi_view, FreeCAD.Vector(0, 0, 1)))
        with open(dxf_path, "w", encoding="utf-8") as fh:
            fh.write(dxf_text)
        return "TechDraw.projectToDXF"
    except Exception:
        import importDXF

        importDXF.export(leaf_solids, dxf_path)
        return "importDXF.export"


def _dedup_consecutive(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for pt in pts:
        if not out or out[-1] != pt:
            out.append(pt)
    return out


def _edges_to_svg_cmds(
    edges: list[Any],
    map_pt: Any,
    deflection_mm: float,
    seen_segments: set[tuple[tuple[float, float], ...]],
    max_segments: int = 800,
) -> list[str]:
    cmds: list[str] = []
    for edge in edges:
        if len(cmds) >= max_segments:
            break
        if getattr(edge, "Length", 0.0) < 0.5:
            continue
        curve_type = getattr(getattr(edge, "Curve", None), "TypeId", "")
        if curve_type == "Part::GeomLine" and len(edge.Vertexes) >= 2:
            raw_pts = [map_pt(edge.Vertexes[0].Point), map_pt(edge.Vertexes[-1].Point)]
        else:
            discretized = edge.discretize(Deflection=max(0.25, float(deflection_mm)))
            raw_pts = [map_pt(p) for p in discretized]
        pts = _dedup_consecutive(raw_pts)
        if len(pts) < 2:
            continue
        if len(pts) == 2 and pts[0] == pts[-1]:
            continue
        canon = tuple(pts) if pts[0] <= pts[-1] else tuple(reversed(pts))
        if canon in seen_segments:
            continue
        seen_segments.add(canon)
        coords = " L ".join(f"{px:g} {py:g}" for px, py in pts)
        cmds.append(f"M {coords}")
    return cmds


def project_view(
    doc: Any = None,
    view: str = "front",
    deflection_mm: float = 1.0,
    include_hidden: bool = False,
    save_json_path: str | None = None,
) -> dict[str, Any]:
    """Project 3D leaf solids (including curved, sculpted, or irregular silhouettes) into canonical 2D model-mm coordinates [0..W, 0..H] for compile_drawing_sheet."""
    FreeCAD, Part = _fc()
    leaves = get_leaf_solids(doc)
    if not leaves:
        raise ValueError("No leaf solids found in document.")
    compound = Part.makeCompound([_object_shape(Part, obj) for obj in leaves])
    cb = compound.BoundBox
    v = view.lower().strip()
    if v in ("front", "xz"):
        w_mm, h_mm = round(cb.XLength, 2), round(cb.ZLength, 2)
        hlr_dir = FreeCAD.Vector(0, -1, 0)
        map_hlr = lambda pt: (round(pt.y - cb.XMin, 2), round(cb.ZMax + pt.x, 2))
        map_3d = lambda pt: (round(pt.x - cb.XMin, 2), round(cb.ZMax - pt.z, 2))
    elif v in ("side", "right", "yz"):
        w_mm, h_mm = round(cb.YLength, 2), round(cb.ZLength, 2)
        hlr_dir = FreeCAD.Vector(1, 0, 0)
        map_hlr = lambda pt: (round(-pt.y - cb.YMin, 2), round(cb.ZMax - pt.x, 2))
        map_3d = lambda pt: (round(pt.y - cb.YMin, 2), round(cb.ZMax - pt.z, 2))
    elif v == "left":
        w_mm, h_mm = round(cb.YLength, 2), round(cb.ZLength, 2)
        hlr_dir = FreeCAD.Vector(-1, 0, 0)
        map_hlr = lambda pt: (round(cb.YMax - pt.y, 2), round(cb.ZMax + pt.x, 2))
        map_3d = lambda pt: (round(cb.YMax - pt.y, 2), round(cb.ZMax - pt.z, 2))
    elif v in ("top", "plan", "xy"):
        w_mm, h_mm = round(cb.XLength, 2), round(cb.YLength, 2)
        hlr_dir = FreeCAD.Vector(0, 0, 1)
        # In plan view, rear (YMax) is at top (y=0) and front (YMin) is at bottom (y=D).
        map_hlr = lambda pt: (round(pt.x - cb.XMin, 2), round(cb.YMax - pt.y, 2))
        map_3d = lambda pt: (round(pt.x - cb.XMin, 2), round(cb.YMax - pt.y, 2))
    else:
        raise ValueError(f"Unsupported view {view!r}; use 'front', 'side', 'left', or 'top'.")

    seen_segments: set[tuple[tuple[float, float], ...]] = set()
    visible_cmds: list[str] = []
    hidden_cmds: list[str] = []
    method = "TechDraw.projectEx"

    try:
        import TechDraw

        groups = TechDraw.projectEx(compound, hlr_dir)
        vis_edges = [e for g in groups[:4] if g is not None and not g.isNull() for e in (g.Edges or [])]
        visible_cmds = _edges_to_svg_cmds(vis_edges, map_hlr, deflection_mm, seen_segments)
        if include_hidden and len(groups) >= 9:
            hid_edges = [e for g in groups[5:9] if g is not None and not g.isNull() for e in (g.Edges or [])]
            hidden_cmds = _edges_to_svg_cmds(hid_edges, map_hlr, deflection_mm, seen_segments)
    except Exception:
        method = "Part.Edges"
        all_edges = [e for obj in leaves for e in (getattr(_object_shape(Part, obj), "Edges", None) or [])]
        visible_cmds = _edges_to_svg_cmds(all_edges, map_3d, deflection_mm, seen_segments)

    paths: list[dict[str, str]] = []
    if visible_cmds:
        paths.append({"d": " ".join(visible_cmds), "style": "solid"})
    if hidden_cmds:
        paths.append({"d": " ".join(hidden_cmds), "style": "hidden"})

    result = {
        "view": v,
        "projection_method": method,
        "width_model_mm": w_mm,
        "height_model_mm": h_mm,
        "edge_count": len(visible_cmds) + len(hidden_cmds),
        "paths": paths,
    }
    if save_json_path:
        resolved = _resolve_cad_path(save_json_path)
        os.makedirs(os.path.dirname(resolved) or "/tmp/cad", exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False)
        result["saved_json_path"] = resolved
    return result


def audit_document(
    doc: Any = None,
    *,
    expected_bbox: tuple[float, float, float] | list[float] | dict[str, float] | None = None,
    target_size: tuple[float, float, float] | list[float] | dict[str, float] | None = None,
    step_path: str | None = None,
    dxf_path: str | None = None,
    cutlist_path: str | None = None,
    bom_rows: list[Any] | None = None,
    default_material: str = "北美黑胡桃木实木 / 多层实木贴皮",
    default_edge_banding: str = "四边 1.0mm 同色木皮封边（见光面木蜡油）",
    overlap_tolerance_mm3: float = 1.0,
    bbox_tolerance_mm: float = 1.5,
    strict: bool = False,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Audit 3D solids for validity, pairwise boolean clashes, envelope dimensions, and BOM reconciliation."""
    FreeCAD, Part = _fc()
    doc = doc or FreeCAD.ActiveDocument
    if doc is None:
        raise ValueError("No active FreeCAD document found.")
    doc.recompute()

    consumed = _get_consumed_names(doc)
    invalid_solids: list[dict[str, Any]] = []
    for obj in doc.Objects:
        if obj.Name in consumed or obj.TypeId in _CONTAINER_TYPES:
            continue
        if obj.TypeId.startswith("Sketcher::") or obj.TypeId.startswith("TechDraw::"):
            continue
        if getattr(obj, "Visibility", True) is False:
            continue
        state = getattr(obj, "State", ()) or ()
        if "Invalid" in state or "Error" in state:
            invalid_solids.append({"name": obj.Name, "label": obj.Label, "reason": f"Feature state {list(state)}"})
            continue
        if obj.TypeId in ("Part::Extrusion", "Part::Revolution", "Part::Loft", "Part::Sweep"):
            sh = _object_shape(Part, obj)
            if sh is not None and not sh.isNull():
                faces = getattr(sh, "Faces", None) or []
                solids = getattr(sh, "Solids", None) or []
                if faces and not solids:
                    invalid_solids.append(
                        {
                            "name": obj.Name,
                            "label": obj.Label,
                            "reason": "Non-solid shell/surface (set Solid=True on Extrusion/Revolution/Loft/Sweep)",
                        }
                    )

    leaves = get_leaf_solids(doc)
    leaf_shapes = [(obj, _object_shape(Part, obj)) for obj in leaves]
    for obj, sh in leaf_shapes:
        if not sh.isValid():
            invalid_solids.append({"name": obj.Name, "label": obj.Label, "reason": "Shape.isValid() is False"})

    if leaf_shapes:
        compound = Part.makeCompound([sh for _, sh in leaf_shapes])
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

    target_env = expected_bbox if expected_bbox is not None else target_size
    bbox_errors: list[str] = []
    if target_env is not None:
        if isinstance(target_env, dict):
            lower_map = {str(k).lower(): v for k, v in target_env.items()}
            exp_w = float(lower_map.get("width") or lower_map.get("w") or lower_map.get("x") or 0.0)
            exp_d = float(lower_map.get("depth") or lower_map.get("d") or lower_map.get("y") or 0.0)
            exp_h = float(lower_map.get("height") or lower_map.get("h") or lower_map.get("z") or 0.0)
        elif len(target_env) == 3:
            exp_w, exp_d, exp_h = (float(target_env[0]), float(target_env[1]), float(target_env[2]))
        else:
            exp_w = exp_d = exp_h = 0.0
        if exp_w > 0 and exp_d > 0 and exp_h > 0:
            dx = abs(overall_bbox["width_x_mm"] - exp_w)
            dy = abs(overall_bbox["depth_y_mm"] - exp_d)
            dz = abs(overall_bbox["height_z_mm"] - exp_h)
            if dx > bbox_tolerance_mm or dy > bbox_tolerance_mm or dz > bbox_tolerance_mm:
                bbox_errors.append(
                    f"Envelope mismatch: modeled ({overall_bbox['width_x_mm']}, {overall_bbox['depth_y_mm']}, "
                    f"{overall_bbox['height_z_mm']}) mm vs expected ({exp_w}, {exp_d}, {exp_h}) mm"
                )

    clashes: list[dict[str, Any]] = []
    for i in range(len(leaf_shapes)):
        a, sha = leaf_shapes[i]
        bba = sha.BoundBox
        for j in range(i + 1, len(leaf_shapes)):
            b, shb = leaf_shapes[j]
            bbb = shb.BoundBox
            if not _boxes_overlap(bba, bbb):
                continue
            try:
                common = sha.common(shb)
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
    total_solid_instances = 0
    for obj, sh in leaf_shapes:
        sub_solids = list(getattr(sh, "Solids", None) or [sh])
        for sub in sub_solids:
            total_solid_instances += 1
            bb = sub.BoundBox
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
        seen_keys: set[tuple[str, str, str, str, str]] = set()
        for idx, row in enumerate(bom_rows, start=1):
            if isinstance(row, dict):
                pid = str(row.get("Part_ID") or f"P{idx:02d}")
                pname = str(row.get("Part_Name") or row.get("Name") or f"Part_{idx}")
                glen = str(row.get("Grain_Length_mm") or row.get("Length_mm") or row.get("Length") or "")
                cwid = str(row.get("Cross_Width_mm") or row.get("Width_mm") or row.get("Width") or "")
                thk = str(row.get("Thickness_mm") or row.get("Thickness") or "")
                cat = str(row.get("Category") or "柜体构件")
                mat = str(row.get("Material") or default_material)
                qty = _parse_qty(row.get("Qty"))
                eband = str(row.get("Edge_Banding") or default_edge_banding)
                notes = str(row.get("Notes") or "净尺寸（开料另加余量）")
            elif isinstance(row, (list, tuple)):
                pid = str(row[0]) if len(row) > 0 else f"P{idx:02d}"
                pname = str(row[1]) if len(row) > 1 else f"Part_{idx}"
                glen = str(row[2]) if len(row) > 2 else ""
                cwid = str(row[3]) if len(row) > 3 else ""
                thk = str(row[4]) if len(row) > 4 else ""
                qty = _parse_qty(row[5]) if len(row) > 5 else 1
                cat = "柜体构件"
                mat = default_material
                eband = str(row[6]) if len(row) > 6 else default_edge_banding
                notes = str(row[7]) if len(row) > 7 else "净尺寸（开料另加余量）"
            else:
                continue
            dedup_key = (pid, pname, glen, cwid, thk)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            final_bom.append(
                {
                    "Part_ID": pid,
                    "Part_Name": pname,
                    "Category": cat,
                    "Material": mat,
                    "Grain_Length_mm": glen,
                    "Cross_Width_mm": cwid,
                    "Thickness_mm": thk,
                    "Qty": qty,
                    "Edge_Banding": eband,
                    "Notes": notes,
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
        step_path = _resolve_cad_path(step_path)
        os.makedirs(os.path.dirname(step_path) or "/tmp/cad", exist_ok=True)
        Part.export(leaves, step_path)
        exports["step_path"] = step_path
        exports["step_bytes"] = os.path.getsize(step_path)

    if dxf_path and leaves:
        dxf_path = _resolve_cad_path(dxf_path)
        method = export_orthographic_dxf(leaves, dxf_path)
        exports["dxf_path"] = dxf_path
        exports["dxf_bytes"] = os.path.getsize(dxf_path)
        exports["dxf_method"] = method

    if cutlist_path:
        cutlist_path = _resolve_cad_path(cutlist_path)
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
        with open(cutlist_path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in final_bom:
                writer.writerow(row)
        exports["cutlist_path"] = cutlist_path
        exports["cutlist_bytes"] = os.path.getsize(cutlist_path)

    bom_total_qty = sum(int(r["Qty"]) for r in final_bom)
    report = {
        "ok": ok,
        "zero_clash": len(clashes) == 0,
        "leaf_solid_count": len(leaves),
        "solid_instance_count": total_solid_instances,
        "part_count": len(leaves),
        "solid_count": len(leaves),
        "unique_part_groups": len(grouped),
        "bom_row_count": len(final_bom),
        "bom_total_qty": bom_total_qty,
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


audit_assembly = audit_document
check_assembly = audit_document


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

