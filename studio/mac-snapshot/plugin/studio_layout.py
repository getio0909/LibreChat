"""Internal source-plan analysis and room coverage, without geometry forms."""
import base64
import hashlib
import io
import itertools
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageCms, ImageOps

from .studio_images import BASE_URL, NoRedirect, read_config

LAYOUT_MODEL = 'doubao-seed-evolving'
OBJECT_KINDS = ('round_table', 'table', 'chair', 'sofa', 'counter', 'cabinet',
                'wardrobe', 'bed', 'bed_detail', 'sink', 'toilet', 'tub', 'shower',
                'washer', 'dryer', 'cooktop', 'dishwasher', 'refrigerator', 'oven',
                'appliance', 'plant', 'other')
EDGE_KINDS = ('wall', 'open', 'window', 'door', 'unclear')


class LayoutUnavailable(RuntimeError):
    def __init__(self, message, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic or {}


def read_plan(path):
    with Image.open(path) as source:
        if source.format not in ('JPEG', 'PNG', 'WEBP') or source.width * source.height > 40_000_000:
            raise ValueError('平面图格式或像素数量不支持')
        image = ImageOps.exif_transpose(source)
        alpha = image.convert('RGBA').getchannel('A') if 'A' in image.getbands() or 'transparency' in image.info else None
        if source.info.get('icc_profile'):
            profile = ImageCms.ImageCmsProfile(io.BytesIO(source.info['icc_profile']))
            mode = {'RGB': 'RGB', 'CMYK': 'CMYK', 'GRAY': 'L', 'LAB': 'LAB'}.get(profile.profile.xcolor_space.strip(), image.mode)
            image = ImageCms.profileToProfile(image if image.mode == mode else image.convert(mode),
                                             profile, ImageCms.createProfile('sRGB'), outputMode='RGB')
        image = image.convert('RGBA')
        if alpha is not None:
            image.putalpha(alpha)
        image = Image.alpha_composite(Image.new('RGBA', image.size, 'white'), image).convert('RGB')
        image.info.clear()
        return image


def _projection_run(values, ratio):
    if not values.any():
        return None
    mask = (values > values.max() * ratio).astype('uint8').reshape(1, -1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((1, 15), np.uint8))
    indices = np.flatnonzero(mask.ravel())
    if not len(indices):
        return None
    runs = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
    run = max(runs, key=len)
    return int(run[0]), int(run[-1] + 1)


def geometry_crop(image):
    """Tighten a filled plan; sparse line drawings retain their complete image."""
    preview = image.copy()
    preview.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    pixels = np.asarray(preview).astype(np.int16)
    low = pixels.min(2)
    red = ((pixels[:, :, 0] > pixels[:, :, 1] * 1.35)
           & (pixels[:, :, 0] > pixels[:, :, 2] * 1.3) & (pixels[:, :, 0] > 130))
    mask = ((low < 225) & ~red).astype('uint8')
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    complete = (0, 0, image.width, image.height)
    if count <= 1:
        return complete, 'complete_image'
    component_id = int(np.argmax(stats[1:, 4])) + 1
    component = labels == component_id
    boxes = []
    for ratio in (.45, .55, .65):
        xs, ys = _projection_run(component.sum(0), ratio), _projection_run(component.sum(1), ratio)
        if xs is None or ys is None:
            return complete, 'complete_image'
        boxes.append((xs[0], ys[0], xs[1], ys[1]))
    if boxes[0] != boxes[1] or boxes[0] != boxes[2]:
        return complete, 'complete_image'
    x, y, xx, yy = boxes[0]
    coverage = float(component[y:yy, x:xx].mean())
    if coverage < .5 or (xx - x) * (yy - y) < preview.width * preview.height * .04:
        return complete, 'complete_image'
    sx, sy = image.width / preview.width, image.height / preview.height
    pad_x, pad_y = (xx - x) * .01, (yy - y) * .01
    box = (max(0, math.floor((x - pad_x) * sx)), max(0, math.floor((y - pad_y) * sy)),
           min(image.width, math.ceil((xx + pad_x) * sx)), min(image.height, math.ceil((yy + pad_y) * sy)))
    return box, 'dense_component_projection'


def image_data(image, maximum=4096):
    image = image.copy()
    image.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
    image.info.clear()
    binary = io.BytesIO()
    image.save(binary, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(binary.getvalue()).decode('ascii'), list(image.size)


def plan_images(path):
    path = Path(path)
    image = read_plan(path)
    box, method = geometry_crop(image)
    original, original_pixels = image_data(image)
    detail, detail_pixels = image_data(image.crop(box))
    return dict(original=original, detail=detail, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                source_pixels=list(image.size), original_pixels=original_pixels, detail_pixels=detail_pixels,
                crop_box=list(box), crop_method=method)


def reconcile_coordinate_frame(layout, image):
    """Register a globally mis-scaled coordinate axis to several source walls."""
    layout = validate_layout(layout)
    preview = image.copy()
    preview.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    ink = (np.asarray(preview.convert('RGB')).max(2) < 100).astype('uint8')
    ink = cv2.erode(ink, np.ones((3, 3), np.uint8))
    transforms, evidence = [(1., 0.), (1., 0.)], []
    bounds = layout['floor_bounds']
    for axis in (0, 1):
        dimension = preview.size[axis]
        span = preview.size[1 - axis]
        kernel = np.ones((max(15, span // 12), 1) if axis == 0 else (1, max(15, span // 12)), np.uint8)
        projection = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel).sum(axis)
        indices = np.flatnonzero(projection > max(12, projection.max() * .15))
        runs = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1) if len(indices) else []
        targets = np.array([np.average(run, weights=projection[run]) / dimension for run in runs])
        pairs = []
        for edge in layout['edges']:
            a, b = edge['from'], edge['to']
            length = abs(a[1 - axis] - b[1 - axis])
            if (edge['kind'] == 'wall' and edge['confidence'] >= .6
                    and abs(a[axis] - b[axis]) < .012
                    and length > (bounds[3 - axis] - bounds[1 - axis]) * .06):
                pairs.append(((a[axis] + b[axis]) / 2, length))
        groups = []
        for value, weight in sorted(pairs):
            if groups and value - groups[-1][0] < .007:
                old, total = groups[-1]
                groups[-1] = ((old * total + value * weight) / (total + weight), total + weight)
            else:
                groups.append((value, weight))
        if not 4 <= len(groups) <= 32 or not 4 <= len(targets) <= 32:
            continue
        values, weights = np.array(groups).T
        tolerance = max(3 / dimension, .008)
        def score(scale, offset):
            error = np.abs((scale * values + offset)[:, None] - targets).min(1)
            return float(np.average(np.clip(1 - error / (tolerance * 2), 0, 1), weights=weights)), error
        baseline, _ = score(1, 0)
        options = []
        for a, b in itertools.combinations(range(len(values)), 2):
            if values[b] - values[a] < (bounds[axis + 2] - bounds[axis]) * .25:
                continue
            for c, d in itertools.combinations(range(len(targets)), 2):
                scale = (targets[d] - targets[c]) / (values[b] - values[a])
                offset = targets[c] - scale * values[a]
                if not .25 < scale < 4 or min(scale * np.array(bounds)[[axis, axis + 2]] + offset) < 0 or max(scale * np.array(bounds)[[axis, axis + 2]] + offset) > 1:
                    continue
                value, error = score(scale, offset)
                matched = error < tolerance * 1.5
                if matched.sum() < max(4, math.ceil(len(values) * .7)):
                    continue
                mapped = scale * values[matched] + offset
                if np.ptp(mapped) < np.ptp(targets) * .75:
                    continue
                options.append((value, float(scale), float(offset), int(matched.sum())))
        if not options:
            continue
        options.sort(reverse=True)
        best = options[0]
        competing = next((row for row in options if abs(row[1] - best[1]) > .15 or abs(row[2] - best[2]) > .04), None)
        significant = max(abs(best[1] * point + best[2] - point) for point in (bounds[axis], bounds[axis + 2])) >= .04
        accepted = significant and best[0] >= .65 and best[0] - baseline >= .22 and (competing is None or best[0] - competing[0] >= .09)
        coordinates = [bounds[axis], bounds[axis + 2]]
        coordinates.extend(p[axis] for space in layout['spaces'] for p in space['outline'])
        coordinates.extend(edge[key][axis] for edge in layout['edges'] for key in ('from', 'to'))
        coordinates.extend(row['box'][k] for row in layout['objects'] for k in (axis, axis + 2))
        if min(best[1] * p + best[2] for p in coordinates) < 0 or max(best[1] * p + best[2] for p in coordinates) > 1:
            accepted = False
        evidence.append(dict(axis='xy'[axis], accepted=bool(accepted), baseline_score=round(baseline, 4),
                             candidate_score=round(best[0], 4), matched_wall_lines=best[3],
                             scale=round(best[1], 6), offset=round(best[2], 6)))
        if accepted:
            transforms[axis] = best[1:3]
    def point(value):
        return [transforms[k][0] * value[k] + transforms[k][1] for k in (0, 1)]
    if transforms != [(1., 0.), (1., 0.)]:
        layout['floor_bounds'] = point(bounds[:2]) + point(bounds[2:])
        for space in layout['spaces']:
            space['outline'] = [point(p) for p in space['outline']]
        for edge in layout['edges']:
            edge['from'], edge['to'] = point(edge['from']), point(edge['to'])
        for row in layout['objects']:
            row['box'] = point(row['box'][:2]) + point(row['box'][2:])
        layout = validate_layout(layout)
    return layout, evidence


def outer_boundary_evidence(image):
    """Detect substantial, uniform border bands relative to the depicted floor."""
    image = image.copy()
    image.thumbnail((512, 768), Image.Resampling.LANCZOS)
    pixels = np.asarray(image).astype(float)
    h, w = pixels.shape[:2]
    interior = pixels[round(h * .2):round(h * .85), round(w * .15):round(w * .85)].reshape(-1, 3)
    floor = np.median(interior / np.maximum(1, interior.sum(1, keepdims=True)), axis=0)
    result = {}
    for side in ('left', 'right', 'top', 'bottom'):
        dimension = w if side in ('left', 'right') else h
        indices = range(0, round(dimension * .1)) if side in ('left', 'top') else range(round(dimension * .9), dimension)
        uniform, floor_lines, occupied_lines = [], [], []
        for k in indices:
            line = pixels[round(h * .1):round(h * .9), k] if side in ('left', 'right') else pixels[k, round(w * .1):round(w * .9)]
            median = np.median(line, axis=0)
            chroma = line / np.maximum(1, line.sum(1, keepdims=True))
            middle = np.median(chroma, axis=0)
            fraction = float((np.linalg.norm(chroma - middle, axis=1) < .03).mean())
            occupied = float((line.min(1) < 235).mean())
            if occupied > .8:
                occupied_lines.append(k)
            # A textured floor is not a uniform colour: require positive floor
            # samples along a continuous strip, not an almost uniform line.
            if occupied > .8 and float(((np.linalg.norm(chroma - floor, axis=1) < .03) & (line.min(1) < 235)).mean()) > .3:
                floor_lines.append(k)
            if fraction > .8 and median.min() < 235 and np.linalg.norm(middle - floor) > .05:
                uniform.append(k)
        runs = np.split(uniform, np.flatnonzero(np.diff(uniform) > 1) + 1) if uniform else []
        band = max(runs, key=len) if runs else []
        outer = min if side in ('left', 'top') else max
        floor_runs = np.split(floor_lines, np.flatnonzero(np.diff(floor_lines) > 1) + 1) if floor_lines else []
        outer_floor = min(floor_runs, key=lambda run: abs(outer(run) - outer(occupied_lines))) if occupied_lines and floor_runs else []
        floor_reaches_boundary = bool(len(outer_floor) / dimension >= .012 and occupied_lines
            and abs(outer(outer_floor) - outer(occupied_lines)) / dimension <= .006)
        result[side] = dict(band=[float(band[0]) / dimension, float(band[-1] + 1) / dimension] if len(band) else None,
                            relative_thickness=len(band) / dimension, substantial_band=len(band) / dimension >= .012,
                            floor_reaches_boundary=floor_reaches_boundary)
    return result


def reconcile_outer_edges(layout, detail, method):
    """Reconcile single-space outer edges only with positive pixel evidence."""
    layout = validate_layout(layout)
    evidence = outer_boundary_evidence(detail) if method == 'dense_component_projection' else {}
    strong = {side for side, row in evidence.items() if row['substantial_band']}
    adjacent_pairs = ({'left', 'top'}, {'top', 'right'}, {'right', 'bottom'}, {'bottom', 'left'})
    if len(layout['spaces']) != 1 or strong not in adjacent_pairs:
        return layout, evidence, []
    x, y, xx, yy = layout['floor_bounds']
    changes = []
    for edge in layout['edges']:
        a, b = edge['from'], edge['to']
        side = None
        if abs(a[0] - b[0]) < .015 and abs(a[1] - b[1]) > (yy - y) * .6:
            side = 'left' if abs(a[0] - x) < .075 else 'right' if abs(a[0] - xx) < .075 else None
        elif abs(a[1] - b[1]) < .015 and abs(a[0] - b[0]) > (xx - x) * .6:
            side = 'top' if abs(a[1] - y) < .075 else 'bottom' if abs(a[1] - yy) < .075 else None
        if side is None or edge['kind'] in ('window', 'door'):
            continue
        open_pixels = evidence[side]['floor_reaches_boundary'] and side not in strong
        if side not in strong and not open_pixels:
            continue
        expected = 'wall' if side in strong else 'open'
        if edge['kind'] != expected:
            changes.append(dict(side=side, before=edge['kind'], after=expected,
                                method='floor_continues_to_boundary' if open_pixels else 'substantial_border_band', model_confidence=edge['confidence']))
            edge['kind'] = expected
    return layout, evidence, changes


def reconcile_storage_spaces(layout, image):
    """Rejoin a narrow fitted-storage strip only when wall pixels support it."""
    layout = validate_layout(layout)
    pixels = np.asarray(image.convert('RGB'))
    h, w = pixels.shape[:2]
    ink = (pixels.max(2) < 120).astype('uint8')
    structural = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    if int(structural.sum()) < max(100, w * h * .001):
        return layout, []
    changes = []

    def rectangle(space):
        points = space['outline']
        if len(points) != 4:
            return None
        x, y = min(p[0] for p in points), min(p[1] for p in points)
        xx, yy = max(p[0] for p in points), max(p[1] for p in points)
        if any(min(math.dist(p, corner) for corner in ((x, y), (xx, y), (xx, yy), (x, yy))) > 1e-6 for p in points):
            return None
        return [x, y, xx, yy]

    def line_band(axis, start, end, lo, hi):
        along = h if axis == 0 else w
        across = w if axis == 0 else h
        a, b = round((start + (end - start) * .1) * along), round((end - (end - start) * .1) * along)
        c, d = max(0, round(lo * across)), min(across, round(hi * across) + 1)
        a, b = max(0, a), min(along, b)
        if a >= b or c >= d:
            return None
        band = structural[a:b, c:d] if axis == 0 else structural[c:d, a:b].T
        return band, c, across

    def support(axis, start, end, lo, hi):
        sampled = line_band(axis, start, end, lo, hi)
        return None if sampled is None else float(sampled[0].any(1).mean())

    for storage in list(layout['spaces']):
        box = rectangle(storage)
        own = [row for row in layout['objects'] if row['space_id'] == storage['id']]
        if (box is None or not own or any(row['kind'] not in ('wardrobe', 'cabinet') for row in own)
                or any(storage['id'] in row['space_ids'] for row in layout.get('area_anchors', []))):
            continue
        sides = [(box[2] - box[0]) * w, (box[3] - box[1]) * h]
        if min(sides) / max(sides) > .35:
            continue
        for room in layout['spaces']:
            other = rectangle(room)
            if (room['id'] == storage['id'] or other is None
                    or not any(row['space_id'] == room['id'] and row['kind'] == 'bed' for row in layout['objects'])):
                continue
            seam = None
            for axis in (0, 1):
                along = 1 - axis
                if max(abs(box[along] - other[along]), abs(box[along + 2] - other[along + 2])) > .003:
                    continue
                if abs(box[axis] - other[axis + 2]) <= .003:
                    seam = axis, box[axis], box[axis + 2], box[along], box[along + 2]
                elif abs(box[axis + 2] - other[axis]) <= .003:
                    seam = axis, box[axis + 2], box[axis], box[along], box[along + 2]
            if seam is None:
                continue
            axis, shared, outside, start, end = seam
            radius = min(.012, abs(outside - shared) * .25)
            sampled = line_band(axis, start, end, outside - 2 * radius, outside + 2 * radius)
            if sampled is None:
                continue
            band, offset, across = sampled
            columns = np.flatnonzero(band.mean(0) >= .65)
            if not len(columns):
                continue
            runs = np.split(columns, np.flatnonzero(np.diff(columns) > 1) + 1)
            run = min(runs, key=lambda row: min(abs((offset + column) / across - outside) for column in row))
            if len(run) < 3:
                continue
            outer = float(band[:, run].any(1).mean())
            inside_edge = (offset + (run[0] - 2 if outside > shared else run[-1] + 3)) / across
            lo, hi = (shared - radius, inside_edge) if outside > shared else (inside_edge, shared + radius)
            interior = support(axis, start, end, lo, hi)
            if outer is None or interior is None or outer < .65 or interior > .12:
                continue
            union = [min(box[0], other[0]), min(box[1], other[1]), max(box[2], other[2]), max(box[3], other[3])]
            room['outline'] = [[union[0], union[1]], [union[2], union[1]], [union[2], union[3]], [union[0], union[3]]]
            for row in own:
                row['space_id'] = room['id']
            layout['edges'] = [edge for edge in layout['edges'] if not (
                edge['kind'] in ('wall', 'door', 'open', 'unclear')
                and all(abs(edge[key][axis] - shared) <= .003
                        and start - .003 <= edge[key][1 - axis] <= end + .003 for key in ('from', 'to')))]
            layout['spaces'] = [row for row in layout['spaces'] if row['id'] != storage['id']]
            changes.append(dict(removed_space_id=storage['id'], retained_space_id=room['id'],
                                method='narrow_fitted_storage_without_structural_partition',
                                outside_wall_support=round(outer, 4), interior_partition_support=round(interior, 4)))
            break
    return validate_layout(layout), changes


def refresh_boundary_geometry(analysis, path):
    """Reuse source-bound semantic extraction when its coordinate frame agrees."""
    if not isinstance(analysis, dict):
        return None
    path = Path(path)
    if analysis.get('source_sha256') != hashlib.sha256(path.read_bytes()).hexdigest():
        return None
    image = read_plan(path)
    box, method = geometry_crop(image)
    if analysis.get('source_pixels') != list(image.size) or analysis.get('crop_box') != list(box) or analysis.get('crop_method') != method:
        return None
    corrected, coordinate_evidence = reconcile_coordinate_frame(analysis['layout'], image.crop(box))
    corrected, evidence, changes = reconcile_outer_edges(corrected, image.crop(box), method)
    corrected, space_changes = reconcile_storage_spaces(corrected, image.crop(box))
    _, pixels = image_data(image.crop(box))
    corrected['coordinate_pixels'] = pixels
    corrected, object_changes = reconcile_nested_objects(corrected)
    corrected, pixel_changes = reconcile_bed_symbols(corrected, image.crop(box))
    return {**analysis, 'layout': corrected, 'boundary_evidence': evidence,
            'coordinate_evidence': coordinate_evidence,
            'boundary_changes': [*analysis.get('boundary_changes', []), *changes],
            'object_changes': [*analysis.get('object_changes', []), *object_changes, *pixel_changes],
            'space_changes': [*analysis.get('space_changes', []), *space_changes],
            'semantic_extraction_reused': True}


def _number(value, lower, upper):
    if (type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper):
        raise ValueError('空间识别数值无效')
    return value


def _point(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('空间识别坐标无效')
    return [min(1, max(0, _number(n, -.01, 1.01))) for n in value]


def _box(value):
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError('空间识别范围无效')
    x, y = _point(value[:2])
    xx, yy = _point(value[2:])
    if xx - x < .002 or yy - y < .002:
        raise ValueError('空间识别范围过小')
    return [x, y, xx, yy]


def _label(value, limit=80):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError('空间识别名称无效')
    return value.strip()


def validate_layout(raw):
    if (not isinstance(raw, dict) or type(raw.get('schema')) is not int or raw.get('schema') != 1
            or raw.get('coordinate_frame') != 'detail_image'):
        raise ValueError('空间识别结果格式无效')
    result = dict(schema=1, coordinate_frame='detail_image', floor_bounds=_box(raw.get('floor_bounds')),
                  space_kind=_label(raw.get('space_kind'), 40))
    if 'coordinate_pixels' in raw:
        pixels = raw['coordinate_pixels']
        if not isinstance(pixels, list) or len(pixels) != 2 or any(type(n) is not int or not 1 <= n <= 8192 for n in pixels):
            raise ValueError('空间坐标图像尺寸无效')
        result['coordinate_pixels'] = list(pixels)
    dimensions = raw.get('dimensions_mm', {})
    if not isinstance(dimensions, dict):
        raise ValueError('空间尺寸记录无效')
    result['dimensions_mm'] = {key: None if dimensions.get(key) is None else _number(dimensions[key], 100, 100_000)
                               for key in ('width', 'length', 'height')}
    spaces, edges, objects = raw.get('spaces'), raw.get('edges'), raw.get('objects')
    if (not isinstance(spaces, list) or not 1 <= len(spaces) <= 32 or not isinstance(edges, list)
            or len(edges) > 256 or not isinstance(objects, list) or len(objects) > 256):
        raise ValueError('空间识别内容不完整')
    result['spaces'], ids = [], set()
    for row in spaces:
        if not isinstance(row, dict) or not isinstance(row.get('outline'), list) or not 3 <= len(row['outline']) <= 64:
            raise ValueError('空间轮廓无效')
        key = _label(row.get('id'), 40)
        if key in ids:
            raise ValueError('空间编号重复')
        ids.add(key)
        outline = [_point(p) for p in row['outline']]
        area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(outline, outline[1:] + outline[:1]))) / 2
        if area < .00001:
            raise ValueError('空间轮廓面积无效')
        result['spaces'].append(dict(id=key, label=_label(row.get('label')), outline=outline))
    if 'area_anchors' in raw:
        anchors = raw['area_anchors']
        if not isinstance(anchors, list) or len(anchors) > 32:
            raise ValueError('空间面积记录无效')
        result['area_anchors'] = []
        for row in anchors:
            covered = row.get('space_ids') if isinstance(row, dict) else None
            if (not isinstance(covered, list) or not covered or len(covered) > 32
                    or any(key not in ids for key in covered) or len(set(covered)) != len(covered)):
                raise ValueError('空间面积对应范围无效')
            result['area_anchors'].append(dict(area_m2=_number(row.get('area_m2'), .5, 10_000), space_ids=list(covered)))
    result['edges'] = []
    for row in edges:
        if not isinstance(row, dict) or row.get('kind') not in EDGE_KINDS:
            raise ValueError('墙体或开口识别无效')
        start, end = _point(row.get('from')), _point(row.get('to'))
        if math.dist(start, end) < .002:
            raise ValueError('墙体或开口范围过小')
        result['edges'].append(dict(kind=row['kind'], **{'from': start, 'to': end},
                                   confidence=_number(row.get('confidence', .5), 0, 1)))
        if row['kind'] == 'window' and 'sill_height_mm' in row:
            result['edges'][-1]['sill_height_mm'] = None if row['sill_height_mm'] is None else _number(row['sill_height_mm'], 0, 10_000)
    result['objects'], object_ids = [], set()
    for row in objects:
        if not isinstance(row, dict) or row.get('kind') not in OBJECT_KINDS or row.get('space_id') not in ids:
            raise ValueError('物件识别无效')
        key = _label(row.get('id'), 40)
        if key in object_ids:
            raise ValueError('物件编号重复')
        object_ids.add(key)
        result['objects'].append(dict(id=key, kind=row['kind'], space_id=row['space_id'], box=_box(row.get('box'))))
    return result


def reconcile_nested_objects(layout):
    """Keep small enclosed bed symbols on the bed, not as full-height furniture."""
    layout = validate_layout(layout)
    beds = [row for row in layout['objects'] if row['kind'] == 'bed']
    changes = []
    for row in layout['objects']:
        if row['kind'] not in ('cabinet', 'other'):
            continue
        x, y, xx, yy = row['box']
        for bed in beds:
            a, b, aa, bb = bed['box']
            if (row['space_id'] == bed['space_id'] and a <= x < xx <= aa and b <= y < yy <= bb
                    and (xx - x) * (yy - y) <= (aa - a) * (bb - b) * .2):
                changes.append(dict(object_id=row['id'], before=row['kind'], after='bed_detail',
                                    parent_id=bed['id'], method='small_symbol_contained_by_bed'))
                row['kind'] = 'bed_detail'
                break
    return layout, changes


def reconcile_bed_symbols(layout, image):
    """Recover a paired head-end symbol within an already identified bed."""
    layout = validate_layout(layout)
    changes = []
    pixels = np.asarray(image.convert('RGB'))
    ink = (pixels.max(2) < 130).astype('uint8') * 255
    height, width = ink.shape
    for bed in [row for row in layout['objects'] if row['kind'] == 'bed']:
        x, y, xx, yy = bed['box']
        if (len(layout['objects']) > 254 or any(row['kind'] == 'bed_detail' and row['space_id'] == bed['space_id']
                and x <= row['box'][0] < row['box'][2] <= xx and y <= row['box'][1] < row['box'][3] <= yy
                for row in layout['objects'])):
            continue
        left, top, right, bottom = round(x * width), round(y * height), round(xx * width), round(yy * height)
        bw, bh = right - left, bottom - top
        if min(bw, bh) < 35:
            continue
        vertical = bh >= bw
        contours, _ = cv2.findContours(ink[top:bottom, left:right], cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        symbols = []
        for contour in contours:
            polygon = cv2.approxPolyDP(contour, .02 * cv2.arcLength(contour, True), True)
            a, b, w, h = cv2.boundingRect(polygon)
            short_ratio, long_ratio = (w / bw, h / bh) if vertical else (h / bh, w / bw)
            if (len(polygon) != 4 or not cv2.isContourConvex(polygon)
                    or not .18 <= short_ratio <= .48 or not .08 <= long_ratio <= .32
                    or cv2.contourArea(polygon) < (w - 1) * (h - 1) * .9):
                continue
            box = (a, b, a + w, b + h)
            if any(abs(a - old[0]) + abs(b - old[1]) + abs(a + w - old[2]) + abs(b + h - old[3]) < 12 for old in symbols):
                continue
            symbols.append(box)
        pairs = []
        for index, a in enumerate(symbols):
            for b in symbols[index + 1:]:
                ac, bc = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2), ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
                along, across = (1, 0) if vertical else (0, 1)
                long_side, short_side = (bh, bw) if vertical else (bw, bh)
                center = (ac[along] + bc[along]) / 2
                same_sizes = all(.75 <= (a[k + 2] - a[k]) / (b[k + 2] - b[k]) <= 1.33 for k in (0, 1))
                if (same_sizes and abs(ac[along] - bc[along]) <= long_side * .06
                        and short_side * .25 <= abs(ac[across] - bc[across]) <= short_side * .65
                        and min(center, long_side - center) <= long_side * .23):
                    pairs.append((center < long_side / 2, a, b))
        if not pairs or len({pair[0] for pair in pairs}) != 1:
            continue
        _, *pair = max(pairs, key=lambda row: sum((s[2] - s[0]) * (s[3] - s[1]) for s in row[1:]))
        for symbol in sorted(pair):
            box = [(left + symbol[0]) / width, (top + symbol[1]) / height,
                   (left + symbol[2]) / width, (top + symbol[3]) / height]
            key = 'bed_symbol_' + hashlib.sha256((bed['id'] + repr(box)).encode()).hexdigest()[:16]
            if any(row['id'] == key for row in layout['objects']):
                continue
            layout['objects'].append(dict(id=key, kind='bed_detail', space_id=bed['space_id'], box=box))
            changes.append(dict(object_id=key, parent_id=bed['id'], after='bed_detail',
                                method='paired_head_end_symbols_from_source_pixels'))
    return layout, changes


def analyze_plan(config_path, path):
    config = read_config(config_path)
    if config.get('provider') != 'ark':
        raise LayoutUnavailable('当前图片服务不支持空间识别')
    images = plan_images(path)
    key = Path(config['key_file']).read_text().strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError('空间识别服务密钥格式无效')
    schema = dict(schema=1, coordinate_frame='detail_image', space_kind='exhibition_booth',
                  floor_bounds=[0, 0, 1, 1], dimensions_mm=dict(width=None, length=None, height=None),
                  spaces=[dict(id='space_1', label='空间名称', outline=[[0, 0], [1, 0], [1, 1], [0, 1]])],
                  area_anchors=[],
                  edges=[dict(kind='wall', **{'from': [0, 0], 'to': [0, 1]}, confidence=.8)],
                  objects=[dict(id='object_1', kind='table', space_id='space_1', box=[.1, .1, .2, .3])])
    instruction = ('Extract the complete visible layout into JSON for architectural visualization. Image 1 is the original drawing with dimensions; image 2 is an automatically selected detailed view. '
                   'All normalized coordinates must refer to image 2, with the origin at its top left, x to the right and y down. '
                   f'Image 2 is exactly {images["detail_pixels"][0]} pixels wide and {images["detail_pixels"][1]} pixels high. '
                   f'Calculate x as the source pixel column divided by {images["detail_pixels"][0]}, and y as the source pixel row divided by {images["detail_pixels"][1]}. '
                   'Use each axis independently; the image can be wide or tall. The right edge is x=1 and the bottom edge is y=1. '
                   'Find the usable floor bounds, enumerate every depicted interior space, its outline, walls, openings, windows, doors and every individual furniture item. '
                   'The crop may contain white margins or labels; exclude those from floor_bounds. Treat drawing arrows, dimension lines, text and lighting dots as annotations, not furniture or windows. '
                   'Inspect the pixels to locate each item; give axis-aligned bounding boxes, not center-size pairs. Each chair is a separate item. '
                   'A bed includes its mattress and pillows: do not count pillows, mattress subdivisions, sofa cushions or cupboard shelves as separate cabinets. '
                   'Cabinet outlines, appliance grids, tiled floor lines and bed outlines are not partition walls. Built-in wardrobes belong to their room; include their footprint in that room outline. '
                   'For fitted storage, locate each actual cabinet footprint instead of boxing the entire closet and its walking aisle. Distinguish wardrobes, low cabinets, washing machines, dryers and appliances. '
                   'Use cooktop for a clearly depicted hob, dishwasher for a dishwasher, refrigerator for a fridge or freezer, and oven for an oven. Keep each separate device footprint. '
                   'Use appliance only when the device type cannot be read from its symbol or printed label; never infer its type from the room alone. '
                   'Base cabinets follow their actual modules, not a single solid volume that also covers a separate dishwasher, refrigerator or oven. A cooktop can overlap its supporting counter footprint. '
                   'Read dimension numbers only when printed, using the usable interior spans rather than combining unrelated outer dimensions. Use null when the width, length or height is unknown. '
                   'Window sill annotations such as BH700 are not room heights: add sill_height_mm on that window edge when printed, otherwise null. '
                   'If a floor area in square metres is printed, add area_anchors entries with area_m2 and all space_ids covered by that number. '
                   'An area applying to an open living/dining/kitchen region must cover the complete shared region, not only one sub-zone. Never guess missing area values; use an empty list when no printed area can be assigned. '
                   'Enumerate clearly distinct living, dining, kitchen and entry zones separately even when connected without partitions; their outlines share open boundaries, not invented walls. '
                   'Do not invent or remove boundary structure. Use unclear where genuinely undecidable. Include internal partitions and separate open edges. '
                   'Classify space_kind as residential, commercial, exhibition_booth or unknown from the drawing, not from the example. '
                   'Use brief Chinese space labels, using 展陈空间 for an open exhibition booth rather than naming the whole booth after its tables. Allowed edge kinds: ' + ', '.join(EDGE_KINDS) + '. Allowed object kinds: ' + ', '.join(OBJECT_KINDS)
                   + '. Return only JSON in this structure; examples illustrate the format, not the actual layout: ' + json.dumps(schema, ensure_ascii=False))
    body = dict(model=LAYOUT_MODEL, messages=[dict(role='user', content=[dict(type='text', text=instruction),
        dict(type='image_url', image_url=dict(url=images['original'], detail='high')),
        dict(type='image_url', image_url=dict(url=images['detail'], detail='high'))])],
        thinking=dict(type='enabled'), reasoning_effort='medium', max_completion_tokens=32000,
        response_format=dict(type='json_object'), stream=False)
    request = urllib.request.Request(BASE_URL + '/chat/completions', json.dumps(body, ensure_ascii=False).encode(),
                                     headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    started = time.monotonic()
    diagnostic = dict(stage='service_response', requested_model=LAYOUT_MODEL,
                      reasoning_effort='medium', source_sha256=images['source_sha256'])
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=None) as response:
            binary = response.read(2 * 1024 * 1024 + 1)
        if len(binary) > 2 * 1024 * 1024:
            raise ValueError('空间识别结果过大')
        response = json.loads(binary)
        diagnostic['stage'] = 'completion_status'
        diagnostic['returned_model'] = response.get('model', '')
        diagnostic['usage'] = {key: value for key, value in response.get('usage', {}).items()
                               if key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                               and type(value) is int and 0 <= value < 1_000_000_000}
        choice = response['choices'][0]
        diagnostic['finish_reason'] = choice.get('finish_reason')
        if choice.get('finish_reason') != 'stop':
            raise ValueError('空间识别结果不完整')
        diagnostic['stage'] = 'json_decode'
        raw = json.loads(choice['message']['content'])
        raw['coordinate_pixels'] = images['detail_pixels']
        diagnostic['stage'] = 'layout_validation'
        layout = validate_layout(raw)
        diagnostic['stage'] = 'pixel_reconciliation'
        detail = read_plan(io.BytesIO(base64.b64decode(images['detail'].split(',', 1)[1])))
        layout, coordinate_evidence = reconcile_coordinate_frame(layout, detail)
        layout, boundary_evidence, boundary_changes = reconcile_outer_edges(layout, detail, images['crop_method'])
        diagnostic['stage'] = 'space_reconciliation'
        layout, space_changes = reconcile_storage_spaces(layout, detail)
        layout, object_changes = reconcile_nested_objects(layout)
        layout, pixel_changes = reconcile_bed_symbols(layout, detail)
        object_changes.extend(pixel_changes)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        diagnostic.update(http_status=code, elapsed_seconds=round(time.monotonic() - started, 2))
        raise LayoutUnavailable('空间识别服务暂时不可用（' + str(code) + '）', diagnostic) from None
    except (OSError, ValueError, KeyError, IndexError, AttributeError, TypeError, urllib.error.URLError) as error:
        diagnostic.update(error_type=type(error).__name__, elapsed_seconds=round(time.monotonic() - started, 2))
        if isinstance(error, ValueError) and str(error).startswith(('空间', '墙体', '物件')):
            diagnostic['validation_message'] = str(error)
        raise LayoutUnavailable('空间识别尚未完成，原始平面图仍保留', diagnostic) from None
    return dict(layout=layout, source_sha256=images['source_sha256'], source_pixels=images['source_pixels'],
                crop_box=images['crop_box'], crop_method=images['crop_method'], detail_pixels=images['detail_pixels'],
                requested_model=LAYOUT_MODEL, returned_model=response.get('model', ''),
                reasoning_effort='medium', finish_reason=choice.get('finish_reason'),
                elapsed_seconds=round(time.monotonic() - started, 2), usage=response.get('usage', {}),
                coordinate_evidence=coordinate_evidence,
                boundary_evidence=boundary_evidence, boundary_changes=boundary_changes,
                object_changes=object_changes, space_changes=space_changes)


def visualization_dimensions(layout):
    """Internal viewing scale; never fill unknown measured dimensions."""
    x, y, xx, yy = layout['floor_bounds']
    px, py = layout.get('coordinate_pixels', [1, 1])
    image_width, image_depth = (xx - x) * px, (yy - y) * py
    width, length = layout['dimensions_mm']['width'], layout['dimensions_mm']['length']
    if width is not None or length is not None:
        return (width if width is not None else length * image_width / image_depth,
                length if length is not None else width * image_depth / image_width, 'printed_linear_dimensions')
    scales = []
    for anchor in layout.get('area_anchors', []):
        polygons = [row['outline'] for row in layout['spaces'] if row['id'] in anchor['space_ids']]
        area = sum(abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(p, p[1:] + p[:1]))) / 2 for p in polygons) * px * py
        if area > 0:
            scales.append(math.sqrt(anchor['area_m2'] / area))
    source = 'printed_area_visualization'
    if not scales and 'coordinate_pixels' in layout:
        for row in layout['objects']:
            if row['kind'] != 'bed':
                continue
            a, b, aa, bb = row['box']
            sides = sorted(((aa - a) * px, (bb - b) * py))
            # Conventional bed footprint is a viewing assumption, not a measurement.
            area = 3.6 if sides[0] / sides[1] >= .65 else 1.9
            scales.append(math.sqrt(area / (sides[0] * sides[1])))
        source = 'conventional_bed_visualization'
    if scales:
        scale = statistics.median(scales) * 1000
        return image_width * scale, image_depth * scale, source
    return 6000, 6000 * image_depth / image_width, 'default_visualization'


def _outline_triangles(outline):
    """Tile a simple room outline without covering concave outdoor corners."""
    points = []
    for point in outline:
        if not points or math.dist(points[-1], point) > 1e-9:
            points.append(tuple(point))
    if len(points) > 1 and math.dist(points[0], points[-1]) <= 1e-9:
        points.pop()
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    while len(points) > 3:
        index = next((i for i, point in enumerate(points)
                      if abs(cross(points[i - 1], point, points[(i + 1) % len(points)])) <= 1e-9
                      and _segment_distance(point, points[i - 1], points[(i + 1) % len(points)]) <= 1e-9), None)
        if index is None:
            break
        points.pop(index)
    if len(points) < 3:
        raise ValueError('空间轮廓无法生成顶面')
    winding = 1 if sum(a[0] * b[1] - b[0] * a[1]
                       for a, b in zip(points, points[1:] + points[:1])) > 0 else -1
    result = []
    while len(points) > 3:
        for index, b in enumerate(points):
            a, c = points[index - 1], points[(index + 1) % len(points)]
            if cross(a, b, c) * winding <= 1e-9:
                continue
            if any(all(cross(u, v, p) * winding >= -1e-9 for u, v in ((a, b), (b, c), (c, a)))
                   for i, p in enumerate(points) if i not in (index - 1 if index else len(points) - 1,
                                                             index, (index + 1) % len(points))):
                continue
            result.append([a, b, c])
            points.pop(index)
            break
        else:
            raise ValueError('空间轮廓无法生成顶面')
    result.append(points)
    return result


def scene_geometry(layout):
    """Map source-derived geometry to a visualization scale, not measured CAD."""
    from .studio_floorplan import chair
    layout = validate_layout(layout)
    x, y, xx, yy = layout['floor_bounds']
    width, length, scale_source = visualization_dimensions(layout)
    w, depth = width / 1000, length / 1000
    height = (layout['dimensions_mm']['height'] or 3000) / 1000
    def position(point):
        return (point[0] - x) / (xx - x) * w, (point[1] - y) / (yy - y) * depth
    def bounds(box):
        return [*position(box[:2]), *position(box[2:])]
    objects = [dict(name='floor', box=[-w, -depth, -.1, w * 2, depth * 2, 0], color=(212, 211, 204))]
    if layout['space_kind'] != 'exhibition_booth':
        for space in layout['spaces']:
            for triangle in _outline_triangles(space['outline']):
                objects.append(dict(name='ceiling_' + space['id'], polygon=[position(p) for p in triangle],
                                    z_range=[height, height], color=(229, 227, 220), draw_edges=False))
    thickness = min(.18, max(.08, min(w, depth) * .018))
    for index, edge in enumerate(layout['edges']):
        if edge['kind'] == 'open':
            continue
        a, b = position(edge['from']), position(edge['to'])
        dx, dy = b[0] - a[0], b[1] - a[1]
        distance = math.hypot(dx, dy)
        nx, ny = -dy / distance * thickness / 2, dx / distance * thickness / 2
        polygon = [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny),
                   (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]
        sill = min(height * .7, (edge.get('sill_height_mm') if edge.get('sill_height_mm') is not None else min(850, height * 250)) / 1000)
        head = max(sill + .1, height * .78)
        ranges = ([(0, sill), (head, height)] if edge['kind'] == 'window'
                  else [(min(2.1, height * .8), height)] if edge['kind'] == 'door'
                  else [(0, height)])
        for z, zz in ranges:
            if zz <= z:
                continue
            objects.append(dict(name='wall_' + str(index), polygon=polygon, z_range=[z, zz], color=(177, 163, 139)))
        if edge['kind'] == 'window':
            gx, gy = nx * .08, ny * .08
            glass = [(a[0] + gx, a[1] + gy), (b[0] + gx, b[1] + gy),
                     (b[0] - gx, b[1] - gy), (a[0] - gx, a[1] - gy)]
            objects.append(dict(name='window_glass_' + str(index), polygon=glass, z_range=[sill, head], color=(197, 220, 226)))
            along = min(.055, distance * .08) / distance
            for start, end in ((a, (a[0] + dx * along, a[1] + dy * along)),
                               ((b[0] - dx * along, b[1] - dy * along), b)):
                frame = [(start[0] + nx, start[1] + ny), (end[0] + nx, end[1] + ny),
                         (end[0] - nx, end[1] - ny), (start[0] - nx, start[1] - ny)]
                objects.append(dict(name='window_frame_' + str(index), polygon=frame, z_range=[sill, head], color=(219, 223, 222)))
            rail_height = min(.055, (head - sill) * .08)
            for z, zz in ((sill, sill + rail_height), (head - rail_height, head)):
                objects.append(dict(name='window_frame_' + str(index), polygon=polygon,
                                    z_range=[z, zz], color=(219, 223, 222)))
    tables = [row for row in layout['objects'] if row['kind'] in ('table', 'round_table')]
    table_centers = [position([(row['box'][0] + row['box'][2]) / 2, (row['box'][1] + row['box'][3]) / 2]) for row in tables]
    default_heights = {'table': .76, 'counter': .95, 'cabinet': 1.1, 'wardrobe': 2.1,
                       'bed': .55, 'bed_detail': .68, 'sofa': .8, 'washer': .85, 'dryer': .85,
                       'dishwasher': .85, 'refrigerator': 1.9, 'oven': .9,
                       'appliance': 1.9, 'sink': .85, 'toilet': .8, 'tub': .6, 'shower': 1.9, 'plant': 1.2, 'other': .85}
    for row in layout['objects']:
        left, top, right, bottom = bounds(row['box'])
        cx, cy = (left + right) / 2, (top + bottom) / 2
        kind, name = row['kind'], row['id']
        if kind == 'round_table':
            radius = min(right - left, bottom - top) / 2
            objects.append(dict(name=name, cylinder=[cx, cy, 0, radius, .76], color=(196, 182, 154),
                parts=[dict(cylinder=[cx, cy, .68, radius, .76]), dict(cylinder=[cx, cy, .08, min(.08, radius * .25), .68]),
                       dict(cylinder=[cx, cy, 0, radius * .45, .08])]))
        elif kind == 'chair':
            center = min(table_centers, key=lambda p: math.dist(p, (cx, cy))) if table_centers else (cx, cy + 1)
            objects.append(chair(name, cx, cy, max(.09, min(right - left, bottom - top) / 2), (cx - center[0], cy - center[1])))
        elif kind == 'table':
            top_height, leg = .76, min(.065, (right - left) * .12, (bottom - top) * .12)
            parts = [dict(box=[left, top, top_height - .08, right, bottom, top_height])]
            for lx, ly in ((left, top), (right - leg, top), (left, bottom - leg), (right - leg, bottom - leg)):
                parts.append(dict(box=[lx, ly, 0, lx + leg, ly + leg, top_height - .08]))
            objects.append(dict(name=name, parts=parts, color=(148, 117, 81)))
        elif kind == 'cooktop':
            # A visualization convention, not a measured appliance height.
            supports = [item for item in layout['objects'] if item['space_id'] == row['space_id']
                        and item['kind'] in ('counter', 'cabinet')
                        and item['box'][0] <= (row['box'][0] + row['box'][2]) / 2 <= item['box'][2]
                        and item['box'][1] <= (row['box'][1] + row['box'][3]) / 2 <= item['box'][3]]
            surface = max((default_heights[item['kind']] for item in supports), default=.95)
            objects.append(dict(name=name, box=[left, top, surface + .005, right, bottom, surface + .025],
                                color=(100, 105, 105)))
        elif kind == 'shower':
            # An open enclosure cue; its footprint is not an opaque tall block.
            trim = min(.025, (right - left) * .08, (bottom - top) * .08)
            parts = [dict(box=[left, top, 0, right, bottom, .06], color=(221, 227, 225))]
            for px, py in ((left, top), (right - trim, top), (left, bottom - trim), (right - trim, bottom - trim)):
                parts.append(dict(box=[px, py, .06, px + trim, py + trim, 1.9]))
            parts.extend(dict(box=box) for box in ([left, top, 1.875, right, top + trim, 1.9],
                [left, bottom - trim, 1.875, right, bottom, 1.9],
                [left, top, 1.875, left + trim, bottom, 1.9],
                [right - trim, top, 1.875, right, bottom, 1.9]))
            objects.append(dict(name=name, parts=parts, color=(178, 199, 203)))
        elif kind in ('washer', 'dryer'):
            focus = position(_centroid(next(space['outline'] for space in layout['spaces'] if space['id'] == row['space_id'])))
            x_front = abs(focus[0] - cx) >= abs(focus[1] - cy)
            horizontal, span = (cy, bottom - top) if x_front else (cx, right - left)
            face = (left - .002 if focus[0] < cx else right + .002) if x_front else (top - .002 if focus[1] < cy else bottom + .002)
            normal = -1 if (focus[0] < cx if x_front else focus[1] < cy) else 1
            def front_point(h, z, offset=0):
                return (face + normal * offset, h, z) if x_front else (h, face + normal * offset, z)
            radius = min(span * .36, .24)
            parts = [dict(box=[left, top, 0, right, bottom, .85], color=(212, 217, 216))]
            for size, color, offset in ((radius, (154, 164, 164), 0), (radius * .82, (67, 80, 83), .001)):
                parts.append(dict(surface=[front_point(horizontal + math.cos(i * math.tau / 32) * size,
                                                        .43 + math.sin(i * math.tau / 32) * size, offset) for i in range(32)],
                                  color=color, draw_edges=False))
            parts.append(dict(surface=[front_point(horizontal - span * .35, .75), front_point(horizontal + span * .35, .75),
                                       front_point(horizontal + span * .35, .79), front_point(horizontal - span * .35, .79)],
                              color=(105, 116, 116), draw_edges=False))
            objects.append(dict(name=name, parts=parts, color=(212, 217, 216)))
        elif kind == 'sink':
            rim = min(.045, (right - left) * .12, (bottom - top) * .12)
            parts = [dict(box=[left, top, 0, right, bottom, .77], color=(193, 188, 174)),
                     dict(box=[left, top, .77, right, bottom, .79], color=(156, 171, 171))]
            parts.extend(dict(box=box) for box in ([left, top, .79, right, top + rim, .85],
                [left, bottom - rim, .79, right, bottom, .85],
                [left, top, .79, left + rim, bottom, .85],
                [right - rim, top, .79, right, bottom, .85]))
            objects.append(dict(name=name, parts=parts, color=(226, 231, 228)))
        elif kind == 'toilet':
            focus = position(_centroid(next(space['outline'] for space in layout['spaces'] if space['id'] == row['space_id'])))
            x_front = abs(focus[0] - cx) >= abs(focus[1] - cy)
            rx, ry = (right - left) * .43, (bottom - top) * .43
            bowl = [(cx + rx * math.cos(i * math.tau / 32), cy + ry * math.sin(i * math.tau / 32)) for i in range(32)]
            if x_front:
                tank = [right - (right - left) * .2, top, .4, right, bottom, .8] if focus[0] < cx else [left, top, .4, left + (right - left) * .2, bottom, .8]
            else:
                tank = [left, bottom - (bottom - top) * .2, .4, right, bottom, .8] if focus[1] < cy else [left, top, .4, right, top + (bottom - top) * .2, .8]
            objects.append(dict(name=name, parts=[dict(polygon=bowl, z_range=[.08, .43]), dict(box=tank)], color=(227, 232, 229)))
        elif kind == 'bed':
            parts = [dict(box=[left, top, .12, right, bottom, .55])]
            details = [child for child in layout['objects'] if child['kind'] == 'bed_detail' and child['space_id'] == row['space_id']
                       and row['box'][0] <= child['box'][0] < child['box'][2] <= row['box'][2]
                       and row['box'][1] <= child['box'][1] < child['box'][3] <= row['box'][3]]
            if details:
                center = tuple(sum((child['box'][k] + child['box'][k + 2]) / 2 for child in details) / len(details) for k in (0, 1))
                head = min(('left', 'top', 'right', 'bottom'), key=lambda side:
                           abs(center[0] - row['box'][0 if side == 'left' else 2]) if side in ('left', 'right')
                           else abs(center[1] - row['box'][1 if side == 'top' else 3]))
                board = {'left': [left, top, .1, left + .08, bottom, 1.05],
                         'right': [right - .08, top, .1, right, bottom, 1.05],
                         'top': [left, top, .1, right, top + .08, 1.05],
                         'bottom': [left, bottom - .08, .1, right, bottom, 1.05]}[head]
                parts.append(dict(box=board))
            objects.append(dict(name=name, parts=parts, color=(178, 181, 174)))
        elif kind == 'sofa':
            nearby = [table for table in layout['objects'] if table['space_id'] == row['space_id'] and table['kind'] in ('table', 'round_table')]
            focus = position(_centroid(next(space['outline'] for space in layout['spaces'] if space['id'] == row['space_id'])))
            if nearby:
                table = min(nearby, key=lambda item: math.dist(position([(item['box'][0] + item['box'][2]) / 2,
                                                                           (item['box'][1] + item['box'][3]) / 2]), (cx, cy)))
                focus = position([(table['box'][0] + table['box'][2]) / 2, (table['box'][1] + table['box'][3]) / 2])
            parts = [dict(box=[left, top, .12, right, bottom, .45])]
            trim = min(.13, (right - left) * .18, (bottom - top) * .18)
            if bottom - top > right - left:
                back = [left, top, .4, left + trim, bottom, .85] if focus[0] >= cx else [right - trim, top, .4, right, bottom, .85]
                arms = [[left, top, .4, right, top + trim, .65], [left, bottom - trim, .4, right, bottom, .65]]
            else:
                back = [left, top, .4, right, top + trim, .85] if focus[1] >= cy else [left, bottom - trim, .4, right, bottom, .85]
                arms = [[left, top, .4, left + trim, bottom, .65], [right - trim, top, .4, right, bottom, .65]]
            parts.extend(dict(box=part) for part in (back, *arms))
            objects.append(dict(name=name, parts=parts, color=(178, 181, 174)))
        else:
            objects.append(dict(name=name, box=[left, top, .55 if kind == 'bed_detail' else 0, right, bottom, default_heights[kind]],
                                color=(198, 187, 163) if kind in ('counter', 'cabinet', 'wardrobe') else (178, 181, 174)))
    return dict(width_mm=width, length_mm=length, height_mm=height * 1000, view='C', scale_source=scale_source), objects, position


def _inside(point, polygon):
    x, y = point
    inside = False
    for a, b in zip(polygon, polygon[1:] + polygon[:1]):
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


def _segment_distance(point, a, b):
    vector = (b[0] - a[0], b[1] - a[1])
    squared = vector[0] ** 2 + vector[1] ** 2
    if squared < 1e-12:
        return math.dist(point, a)
    t = min(1, max(0, ((point[0] - a[0]) * vector[0] + (point[1] - a[1]) * vector[1]) / squared))
    return math.dist(point, (a[0] + t * vector[0], a[1] + t * vector[1]))


def _centroid(polygon):
    pairs = list(zip(polygon, polygon[1:] + polygon[:1]))
    crosses = [a[0] * b[1] - b[0] * a[1] for a, b in pairs]
    area6 = 3 * sum(crosses)
    if abs(area6) < 1e-12:
        return tuple(sum(p[k] for p in polygon) / len(polygon) for k in (0, 1))
    return tuple(sum((a[k] + b[k]) * cross for (a, b), cross in zip(pairs, crosses)) / area6 for k in (0, 1))


def space_openings(layout, space):
    outline = space['outline']
    segments = list(zip(outline, outline[1:] + outline[:1]))
    result = []
    for edge in layout['edges']:
        if edge['kind'] not in ('open', 'door'):
            continue
        middle = [(edge['from'][k] + edge['to'][k]) / 2 for k in (0, 1)]
        if min(_segment_distance(middle, a, b) for a, b in segments) <= .02:
            result.append(edge)
    return result


def space_has_windows(layout, space_id):
    outline = next(space['outline'] for space in layout['spaces'] if space['id'] == space_id)
    segments = list(zip(outline, outline[1:] + outline[:1]))
    return any(edge['kind'] == 'window' and any(
        max(_segment_distance(edge['from'], a, b), _segment_distance(edge['to'], a, b)) <= .02
        for a, b in segments) for edge in layout['edges'])


def _segments_cross(a, b, c, d):
    def cross(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return cross(a, b, c) * cross(a, b, d) < -1e-10 and cross(c, d, a) * cross(c, d, b) < -1e-10


def _clear_sightline(eye, target, outline, walls):
    if any(_segments_cross(eye, target, a, b) for a, b in walls):
        return False
    return all(_inside(tuple(eye[k] + (target[k] - eye[k]) * t for k in (0, 1)), outline)
               for t in (.1, .25, .5, .75, .9))


def _geometry_boxes(obj):
    if 'parts' in obj:
        return [box for part in obj['parts'] for box in _geometry_boxes(part)]
    if 'box' in obj:
        return [obj['box']]
    if 'cylinder' in obj:
        x, y, z, radius, top = obj['cylinder']
        return [[x - radius, y - radius, z, x + radius, y + radius, top]]
    if 'surface' in obj:
        points = obj['surface']
        return [[min(p[k] for p in points) for k in range(3)] + [max(p[k] for p in points) for k in range(3)]]
    points, (z, top) = obj['polygon'], obj['z_range']
    return [[min(p[0] for p in points), min(p[1] for p in points), z,
             max(p[0] for p in points), max(p[1] for p in points), top]]


def _segment_hits_box(eye, target, box):
    """Check a finite 3-D sightline, without treating a low object as a wall."""
    start, end = 0., 1.
    for axis in range(3):
        delta = target[axis] - eye[axis]
        if abs(delta) < 1e-10:
            if not box[axis] <= eye[axis] <= box[axis + 3]:
                return False
            continue
        near, far = sorted(((box[axis] - eye[axis]) / delta,
                            (box[axis + 3] - eye[axis]) / delta))
        start, end = max(start, near), min(end, far)
        if start > end:
            return False
    return end > 1e-6 and start < 1 - 1e-6


def _coverage_camera(eye, target, eye_height, primary):
    target_height = max(.9, eye_height - math.dist(eye, target) * math.tan(math.radians(18))) if primary else eye_height
    return dict(eye=[*eye, eye_height], target=[*target, min(eye_height, target_height)], fov_degrees=96)


def _rendered_coverage_choices(spec, geometry, choices, room_objects, primary_kinds, window_indices, eye_height):
    """Rank a bounded shortlist by the furnishings actually visible in its frames."""
    from .studio_floorplan import perspective
    per_eye = {}
    for row in sorted(choices, key=lambda row: row[0], reverse=True):
        alternatives = per_eye.setdefault(row[1], [])
        direction = tuple((row[3][k] - row[2][k]) / math.dist(row[2], row[3]) for k in (0, 1))
        if len(alternatives) < 2 and all(sum(direction[k] * previous[k] for k in (0, 1)) < .94
                                       for _, previous in alternatives):
            alternatives.append((row, direction))
    positions = list(per_eye.values())[:32]
    shortlist = [rows[0][0] for rows in positions] + [rows[1][0] for rows in positions[:16] if len(rows) > 1]
    weights = {row['id']: (6 if row['kind'] in primary_kinds else
                          3 if row['kind'] in ('cooktop', 'sink', 'toilet', 'shower') else
                          2 if row['kind'] in ('refrigerator', 'washer', 'dryer', 'dishwasher', 'oven') else 1)
               for row in room_objects}
    primary_ids = {row['id'] for row in room_objects if row['kind'] in primary_kinds}
    tall_ids = {row['id'] for row in room_objects if row['kind'] in ('refrigerator', 'wardrobe', 'appliance')}
    frame_pixels = 320 * 240
    result = []
    for score, point, eye, target in shortlist:
        visible = {}
        camera = _coverage_camera(eye, target, eye_height, bool(primary_ids))
        perspective(spec, geometry, annotate=False, camera=camera, ownership=visible, resolution=(320, 240), plan_coordinates=True)
        coverage, frame_score = {}, 0.
        for row in room_objects:
            item = visible.get(row['id'], {})
            fraction = item.get('pixels', 0) / frame_pixels
            goal = .09 if row['id'] in primary_ids else .003 if row['kind'] == 'cooktop' else .008 if row['kind'] == 'chair' else .025
            amount = min(1., fraction / goal)
            bounds = item.get('bounds', [1, 1, 0, 0])
            if row['id'] in primary_ids and (any(value <= .005 for value in bounds[:2])
                                             or any(value >= .995 for value in bounds[2:])):
                amount *= .7
            coverage[row['id']] = amount * weights[row['id']]
            frame_score += coverage[row['id']] * 2
            if row['id'] not in primary_ids:
                frame_score -= max(0., fraction - .25) * 16
        # A tall foreground group can hide the work area even when its centre
        # rays and several other object centres technically remain visible.
        tall_fraction = sum(visible.get(name, {}).get('pixels', 0) for name in tall_ids) / frame_pixels
        if len(room_objects) > len(tall_ids):
            frame_score -= max(0., tall_fraction - .25) * 30
        for index in window_indices:
            fraction = visible.get('window_glass_' + str(index), {}).get('pixels', 0) / frame_pixels
            frame_score += min(1., fraction / .018) * 1.5
        result.append((frame_score + score * .1, point, eye, target, coverage))
    return result


def coverage_views(layout, *, per_space=2, rendered_framing=False):
    """Choose usable room views, with clear sightlines and visible furnishings."""
    if type(per_space) is not int or not 1 <= per_space <= 8:
        raise ValueError('空间视角数量无效')
    if type(rendered_framing) is not bool:
        raise ValueError('结构预览选项无效')
    layout = validate_layout(layout)
    spec, geometry, position = scene_geometry(layout)
    eye_height = min(1.65, spec['height_mm'] / 1000 * .62)
    object_ids = {row['id'] for row in layout['objects']}
    object_geometry = {row['name']: _geometry_boxes(row) for row in geometry if row['name'] in object_ids}
    occluders = [(name, box) for name, boxes in object_geometry.items() for box in boxes]
    walls = [(position(row['from']), position(row['to'])) for row in layout['edges'] if row['kind'] in ('wall', 'unclear')]
    furniture_boxes = [[*position(row['box'][:2]), *position(row['box'][2:])] for row in layout['objects']]
    views = []
    for space in layout['spaces']:
        outline = space['outline']
        x, y = min(p[0] for p in outline), min(p[1] for p in outline)
        xx, yy = max(p[0] for p in outline), max(p[1] for p in outline)
        center = _centroid(outline)
        room_polygon = [position(point) for point in outline]
        room_objects = [row for row in layout['objects'] if row['space_id'] == space['id'] and row['kind'] != 'bed_detail']
        primary_kinds = {'bed'} if any(row['kind'] == 'bed' for row in room_objects) else {'sofa', 'table', 'round_table'}
        primary = [row for row in room_objects if row['kind'] in primary_kinds]
        subjects = [(position([(row['box'][0] + row['box'][2]) / 2, (row['box'][1] + row['box'][3]) / 2]),
                     6 if row['kind'] in primary_kinds else 1, row['id']) for row in room_objects]
        candidates = []
        for fx in (.06, .16, .3, .5, .7, .84, .94):
            for fy in (.06, .16, .3, .5, .7, .84, .94):
                point = (x + (xx - x) * fx, y + (yy - y) * fy)
                actual = position(point)
                if (not _inside(point, outline)
                        or any(b[0] - .06 <= actual[0] <= b[2] + .06 and b[1] - .06 <= actual[1] <= b[3] + .06 for b in furniture_boxes)
                        or any(_segment_distance(actual, a, b) < .13 for a, b in walls)
                        or min(_segment_distance(actual, a, b) for a, b in zip(room_polygon, room_polygon[1:] + room_polygon[:1])) < .08):
                    continue
                candidates.append(point)
        if not candidates:
            # A coarse symbol may cover most of a tiny closet. Keep a usable
            # in-room fallback rather than silently dropping the space.
            candidates = [(x + (xx - x) * fx, y + (yy - y) * fy) for fx in (.08, .5, .92) for fy in (.08, .5, .92)
                          if _inside((x + (xx - x) * fx, y + (yy - y) * fy), outline)]
        if not candidates:
            candidates = [center]
        if not _inside(center, outline):
            center = min(candidates, key=lambda p: math.dist(p, center))
        room_edges = space_openings(layout, space)
        def open_distance(point):
            if not room_edges:
                return 0
            def distance(edge):
                a, b, p = position(edge['from']), position(edge['to']), position(point)
                vector = (b[0] - a[0], b[1] - a[1])
                t = min(1, max(0, ((p[0] - a[0]) * vector[0] + (p[1] - a[1]) * vector[1]) / (vector[0] ** 2 + vector[1] ** 2)))
                return math.dist(p, (a[0] + t * vector[0], a[1] + t * vector[1]))
            return min(distance(edge) for edge in room_edges)
        focal_subjects = [(position([(row['box'][0] + row['box'][2]) / 2,
                                    (row['box'][1] + row['box'][3]) / 2]), 1) for row in primary] if primary else [(p, w) for p, w, _ in subjects]
        targets = [p for p, _ in focal_subjects if _inside(p, room_polygon)]
        if not primary:
            targets.insert(0, position(center))
        if focal_subjects:
            weight = sum(value for _, value in focal_subjects)
            focus = tuple(sum(p[k] * value for p, value in focal_subjects) / weight for k in (0, 1))
            if _inside(focus, room_polygon):
                targets.append(focus)
        choices = []
        for point in candidates:
            eye = position(point)
            visibility = {}
            for subject, _, name in subjects:
                boxes = object_geometry[name]
                bounds = [min(box[k] for box in boxes) for k in range(3)] + [max(box[k] for box in boxes) for k in range(3, 6)]
                z = min(eye_height, bounds[2] + (bounds[5] - bounds[2]) * .72)
                samples = [(subject[0], subject[1], z),
                           (bounds[0] * .68 + bounds[3] * .32, bounds[1] * .68 + bounds[4] * .32, z),
                           (bounds[0] * .32 + bounds[3] * .68, bounds[1] * .32 + bounds[4] * .68, z)]
                visibility[name] = sum(not any(other != name and _segment_hits_box((*eye, eye_height), sample, box)
                                               for other, box in occluders) for sample in samples) / len(samples)
            if primary and not any(visibility[row['id']] for row in primary):
                continue
            for target in targets:
                distance = math.dist(eye, target)
                if distance < .25 or not _clear_sightline(eye, target, room_polygon, walls):
                    continue
                direction = ((target[0] - eye[0]) / distance, (target[1] - eye[1]) / distance)
                visible = 0
                for subject, weight, name in subjects:
                    d = math.dist(eye, subject)
                    if d < .05 or any(_segments_cross(eye, subject, a, b) for a, b in walls):
                        continue
                    forward = sum((subject[k] - eye[k]) * direction[k] for k in (0, 1)) / d
                    if forward >= math.cos(math.radians(40)):
                        visible += weight * visibility[name] * min(1, d / (2.2 if primary else 1.5))
                score = visible * 2 + min(distance, 5) * .25 - min(open_distance(point), 3) * .12
                choices.append((score, point, eye, target))
        if not choices:
            eye = position(candidates[0])
            target = position(center)
            if math.dist(eye, target) < .01:
                target = (eye[0] + .1, eye[1] + .1)
            choices = [(0, candidates[0], eye, target)]
        segments = list(zip(outline, outline[1:] + outline[:1]))
        window_indices = [index for index, edge in enumerate(layout['edges']) if edge['kind'] == 'window'
                          and any(max(_segment_distance(edge['from'], a, b), _segment_distance(edge['to'], a, b)) <= .02
                                  for a, b in segments)]
        if rendered_framing:
            choices = _rendered_coverage_choices(spec, geometry, choices, room_objects, primary_kinds, window_indices, eye_height)
        else:
            choices = [(*row, {}) for row in choices]
        ordered = [max(choices, key=lambda row: row[0])]
        for _ in range(1, per_space):
            available = [row for row in choices if row[1] not in {chosen[1] for chosen in ordered}]
            if not available:
                break
            def complementary_score(row):
                novelty = sum(max(0., value - max(other[4].get(name, 0.) for other in ordered))
                              for name, value in row[4].items())
                return row[0] + 1.5 * min(math.dist(row[2], other[2]) for other in ordered) + novelty * 2
            ordered.append(max(available, key=complementary_score))
        for index, (_, point, eye, target, _) in enumerate(ordered):
            views.append(dict(id=space['id'] + '_view_' + str(index + 1), space_id=space['id'],
                layout_version=9,
                label=space['label'] + (' · 主视角' if index == 0 else ' · 另一侧'),
                camera=_coverage_camera(eye, target, eye_height, bool(primary))))
    return views


def render_view(layout, view, *, depth=False, ownership=None):
    from .studio_floorplan import perspective
    spec, objects, _ = scene_geometry(layout)
    return perspective(spec, objects, annotate=False, depth=depth, camera=view['camera'], ownership=ownership,
                       plan_coordinates=view.get('layout_version', 8) >= 9)


OBJECT_LABELS = dict(round_table='圆桌', table='长方桌', chair='椅子', sofa='沙发', counter='台面',
                     cabinet='柜体', wardrobe='衣柜', bed='床', bed_detail='枕头', sink='洗手池',
                     toilet='马桶', tub='浴缸', shower='淋浴区', washer='洗衣机', dryer='烘干机',
                     cooktop='灶台', dishwasher='洗碗机', refrigerator='冰箱或冷冻柜', oven='烤箱',
                     appliance='电器', plant='绿植', other='未辨认物件')


def _visible_position(position):
    return bool(position and position['pixels'] >= 256
                and position['bounds'][2] - position['bounds'][0] >= .004
                and position['bounds'][3] - position['bounds'][1] >= .004)


def view_object_anchors(layout, view, *, include_neighbors=False):
    """Locate visible source objects in the same camera's unaltered guide."""
    visible = {}
    render_view(layout, view, ownership=visible)
    spaces = {space['id']: space for space in layout['spaces']}
    result = []
    for row in layout['objects']:
        position = visible.get(row['id'])
        current = row['space_id'] == view['space_id']
        if not _visible_position(position) or not (current or include_neighbors):
            continue
        result.append(dict(id=row['id'], kind=row['kind'], label=OBJECT_LABELS[row['kind']],
                           space_id=row['space_id'], space_label=spaces[row['space_id']]['label'],
                           is_current_space=current, **position))
    for index, edge in enumerate(layout['edges']):
        position = visible.get('window_glass_' + str(index))
        if edge['kind'] != 'window' or not _visible_position(position):
            continue
        bordering = [space for space in layout['spaces']
                     if any(max(_segment_distance(edge['from'], a, b), _segment_distance(edge['to'], a, b)) <= .02
                            for a, b in zip(space['outline'], space['outline'][1:] + space['outline'][:1]))]
        current = next((space for space in bordering if space['id'] == view['space_id']), None)
        space = current or (bordering[0] if include_neighbors and bordering else None)
        if space:
            result.append(dict(id='window_glass_' + str(index), kind='window', label='玻璃窗',
                               space_id=space['id'], space_label=space['label'],
                               is_current_space=bool(current), **position))
    result.extend(view_opening_anchors(layout, view))
    return result


def view_opening_anchors(layout, view):
    """Measure visible apertures in a diagnostic render, never in the guide."""
    from .studio_floorplan import perspective
    spec, geometry, position = scene_geometry(layout)
    space = next(row for row in layout['spaces'] if row['id'] == view['space_id'])
    openings = space_openings(layout, space)
    if not openings:
        return []
    labels = {'door': '门洞', 'open': '开放边界'}
    names, surfaces = [], []
    existing = {row['name'] for row in geometry}
    for index, edge in enumerate(openings):
        name = 'opening_anchor_' + str(index)
        while name in existing:
            name = '_' + name
        existing.add(name)
        a, b = position(edge['from']), position(edge['to'])
        height = spec['height_mm'] / 1000
        if edge['kind'] == 'door':
            height = min(2.1, height * .8)
        surfaces.append(dict(name=name, surface=[(*a, 0), (*b, 0), (*b, height), (*a, height)], color=(200, 222, 222)))
        names.append((name, edge['kind']))
    visible = {}
    perspective(spec, geometry + surfaces, annotate=False, camera=view['camera'], ownership=visible,
                plan_coordinates=view.get('layout_version', 8) >= 9)
    return [dict(id=name, kind=kind, label=labels[kind], space_id=space['id'], space_label=space['label'],
                 is_current_space=True, **visible[name]) for name, kind in names if _visible_position(visible.get(name))]


def view_anchor_text(layout, view, *, anchors=None):
    if anchors is None:
        anchors = view_object_anchors(layout, view, include_neighbors=True)
    openings = [row for row in anchors if row['kind'] in ('door', 'open')]
    current = sorted((row for row in anchors if row.get('is_current_space', True) and row['kind'] not in ('door', 'open')),
                     key=lambda row: row['pixels'], reverse=True)[:10]
    neighbors = sorted((row for row in anchors if not row.get('is_current_space', True)),
                       key=lambda row: (row['kind'] not in ('bed', 'sofa', 'table', 'round_table'), -row['pixels']))[:4]
    anchors = current + neighbors + openings
    if not anchors:
        return ''
    entries = []
    for row in anchors:
        x, y = row['center']
        label = row['label'] if row.get('is_current_space', True) else '开口外' + row['space_label'] + '的' + row['label']
        entry = f"{label}可见部分在画面横向{round(x * 100)}%、纵向{round(y * 100)}%附近"
        left, top, right, bottom = row['bounds']
        if row['kind'] in ('door', 'open'):
            entry += f'，可见范围横向{round(left * 100)}%至{round(right * 100)}%、纵向{round(top * 100)}%至{round(bottom * 100)}%'
        if right - left < .08 and (left < .04 or right > .96):
            entry += '，仅在画面' + ('左' if left < .04 else '右') + '侧边缘露出局部'
        entries.append(entry)
    return ('图1物件对应：' + '；'.join(entries) + '。百分比仅说明图1内的位置，'
            '横向从左到右、纵向从上到下；开口外的物件继续留在原空间，保留开口的通透关系。'
            '沿用图示位置和可见范围，替换饰面、织物及灯光，成图不显示这些文字或数字。')


def finish_palette(style_label):
    """Shared, editable material defaults; changing camera must not change them."""
    palettes = {
        '自然写实': '米白细砂质墙面、浅米色哑光石材地面、浅橡木家具、米灰织物座椅',
        '现代简约': '暖白平整墙面、浅灰哑光石材地面、浅橡木家具、浅灰织物座椅',
        '现代木作': '暖米白墙面、浅米色哑光石材地面、自然浅橡木家具、米灰亚麻座椅',
        '现代轻奢': '暖灰墙面、浅米色细纹石材地面、浅胡桃木家具、米色织物座椅与少量香槟金收口',
        '中式雅致': '米白墙面、浅灰自然石材地面、自然胡桃木家具、浅米色织物座椅',
    }
    return palettes.get(style_label, '暖白墙面、浅米色哑光石材地面、自然浅橡木家具、米灰织物座椅')


def validate_finish_scheme(value, style_label=None):
    """Read saved material data without resolving current style defaults."""
    fields = {'wall', 'floor', 'wood', 'fabric', 'trim', 'window_frame'}
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1
            or not isinstance(value.get('style_label'), str) or not value['style_label'].strip()
            or len(value['style_label']) > 100
            or style_label is not None and value['style_label'] != style_label
            or not isinstance(value.get('materials'), dict) or set(value['materials']) - fields):
        raise ValueError('这套图的材质记录未能读取，已有结果仍保留')
    materials = {}
    for key, item in value['materials'].items():
        if (not isinstance(item, str) or not item.strip() or len(item) > 200
                or any(ord(char) < 32 for char in item)):
            raise ValueError('这套图的材质记录未能读取，已有结果仍保留')
        materials[key] = item
    return dict(schema=1, style_label=value['style_label'], materials=materials)


def resolve_finish_scheme(style_label):
    """Resolve one batch's defaults; custom styles keep unspecified finishes open."""
    materials = {}
    if style_label in ('自然写实', '现代简约', '现代木作', '现代轻奢', '中式雅致'):
        finishes = finish_palette(style_label).split('、')
        materials = dict(zip(('wall', 'floor', 'wood', 'fabric'), finishes[:4]))
        if len(finishes) > 4:
            materials['trim'] = '、'.join(finishes[4:])
        materials['window_frame'] = '与墙面同色的哑光窗框'
    return validate_finish_scheme(dict(schema=1, style_label=style_label, materials=materials), style_label)


def seating_groups(layout, space_id, *, visible_ids=None):
    """Describe the source-derived seating count of each nearby table group."""
    _, _, position = scene_geometry(layout)
    tables = [row for row in layout['objects'] if row['space_id'] == space_id and row['kind'] in ('table', 'round_table')
              and (visible_ids is None or row['id'] in visible_ids)]
    if not tables:
        return ''
    def center(row):
        box = row['box']
        return position([(box[0] + box[2]) / 2, (box[1] + box[3]) / 2])
    counts = {row['id']: 0 for row in tables}
    for chair_row in (row for row in layout['objects'] if row['space_id'] == space_id and row['kind'] == 'chair'):
        table = min(tables, key=lambda row: math.dist(center(row), center(chair_row)))
        diagonal = math.dist(position(table['box'][:2]), position(table['box'][2:]))
        if math.dist(center(table), center(chair_row)) <= max(1.2, diagonal * .85):
            counts[table['id']] += 1
    return '；'.join(('圆桌' if row['kind'] == 'round_table' else '长方桌') + str(index + 1)
                    + '配' + str(counts[row['id']]) + '把椅子' for index, row in enumerate(tables) if counts[row['id']])


def photographic_text(layout, view, style_label, style_description='', change='', *, finish_scheme=None, anchors=None):
    if finish_scheme is None:
        materials = finish_palette(style_label)
    else:
        scheme = validate_finish_scheme(finish_scheme, style_label)
        materials = '、'.join(scheme['materials'].values())
    finish_text = ('本套默认材质：' + materials + '。' if materials else
                   '本套材质按所选风格与补充参考确定，未指定的材质不套用其他风格的默认值。')
    finish_text += ('补充照片或设计要求明确指定的材质优先；同套所有视角沿用同一方案，'
                    '不因机位变化改用另一种地面、木色或座椅面料。')
    visible_anchors = view_object_anchors(layout, view, include_neighbors=True) if anchors is None else anchors
    visible_ids = {row['id'] for row in visible_anchors if row['is_current_space']}
    counts = {kind: sum(row['kind'] == kind and row['id'] in visible_ids for row in layout['objects'])
              for kind in OBJECT_KINDS if kind != 'bed_detail'}
    furniture = '、'.join(str(number) + '个' + OBJECT_LABELS[kind] for kind, number in counts.items() if number)
    booth = layout['space_kind'] == 'exhibition_booth'
    open_shared_space = len(layout['spaces']) == 1 and sum(edge['kind'] == 'open' for edge in layout['edges']) >= 2
    lines = [
        '把图1转换为完成装修后的室内建筑摄影照片。图1是从原始平面图整理的当前视角结构，只确定墙体、开放边界、家具位置和相机，不是最终材质或造型。',
        '保持图1的眼平取景、透视和各物体前后关系，以真实的木石纹理、织物、接触阴影、自然反射及合理灯光完成设计。',
        '当前空间和视角：' + view['label'] + '。图1当前空间可见的主要物件：' + (furniture or '按图1呈现当前空间') + '。主要家具沿用图1的位置与数量，按取景自然遮挡；本机位被挡住或在视野之外的家具继续留在原位，无需移到画面内展示。结合风格完成合适的照明、家具饰面及小件陈设。',
        '实体墙仍是实体墙，开放边界保持整面开放；不把墙面替换为玻璃，不把开放边界缩成门洞或加隔墙。',
        '风格：' + style_label + '。' + style_description,
        '设计要求：' + change if change else '在现有布局上完成整体装修和陈设。',
        finish_text,
        '输出一张完整照片，不保留灰模材质、轮廓线、尺寸或图纸文字，不输出俯视模型、剖切空间或多图拼贴。',
    ]
    groups = seating_groups(layout, view['space_id'], visible_ids=visible_ids)
    anchors = view_anchor_text(layout, view, anchors=visible_anchors)
    if anchors:
        lines.insert(3, anchors)
    if any(row['kind'] == 'window' for row in visible_anchors):
        lines.insert(3, '图1实际可见的浅蓝面是透光玻璃窗：保留窗洞、窗台和遮挡，用真实玻璃、窗框及窗外自然光完成，不做成柜门或墙面。属于相邻空间的窗户仍留在相邻空间，不移到当前房间。')
    elif any(row['kind'] == 'window_frame' for row in visible_anchors):
        lines.insert(3, '图1仅露出的窗框沿用原有遮挡和裁切，不补成完整窗洞，也不在其他墙面增加窗户。')
    else:
        lines.insert(3, '本机位没有可见窗洞，沿用图1已有墙面和开口，不为采光另添窗户；光线可以来自画面外的原有窗户或室内灯光。')
    if any(row['kind'] == 'bed_detail' for row in visible_anchors):
        lines.insert(3, '图1床面靠背旁的低矮小块表示枕头，决定床头所在的一端；床品与床头设计沿用这个方向。')
    if any(row['kind'] == 'bed' for row in visible_anchors):
        lines.insert(3, '同套卧室的床架与图示板式床头沿用本套家具木色，床品沿用本套织物；机位变化时保留同款床体。补充照片或设计要求明确指定床体款式时，以指定款式为准。')
    if counts.get('shower'):
        lines.insert(3, '图1里的细框和低矮底盘表示淋浴隔屏，框内是透明玻璃而不是实心柜体；隔屏内外属于同一浴室，沿用图示占地。')
    if counts.get('washer') or counts.get('dryer'):
        lines.insert(3, '图1设备上的深色圆面是洗衣或烘干设备的前面板，沿用各设备的位置和朝向，完成真实金属外壳与玻璃舱门。')
    if groups:
        lines.insert(3, '原图的桌椅分组：' + groups + '。图1决定各组的位置和朝向；保留这份分组数量，不按常见餐厅摆法补齐更多椅子。机位遮挡的椅子无需全部露出。')
    if not booth:
        lines.insert(3, '图1上方的浅色面表示实体天花，保留它与墙面的交接和覆盖范围；用不透光的饰面和符合风格的灯光完成，顶部仍是室内天花，不是玻璃采光口、挑空或上层房间。')
    if booth:
        lines.insert(3, '这是开放展厅里的展陈洽谈展位，不是餐厅或封闭住宅。图1没有墙体的方向连接宽阔、空置的展厅过道；远处展厅背景从简，不另建相邻房间、门框、落地窗或其他洽谈桌椅。不要在桌上铺餐桌布或布置餐具。')
    elif open_shared_space:
        lines.insert(3, '这是两侧开放的空间。图1没有墙体的方向延续为宽阔、空置的公共区域；外部背景保持简洁，不在开放边界另建房间、门框、柜墙或其他桌椅组。空间内仍只保留图1给出的物件。')
    return '\n'.join(lines)
