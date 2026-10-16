"""Explicit rectangular-space trial geometry, not automatic CAD reconstruction."""
import hashlib
import io
import json
import math
import uuid
from pathlib import Path

from PIL import Image, ImageDraw
import numpy as np

from .studio_dimensions import SOURCES, cjk_font
from .studio_store import clean, ident, stamp

SIDES = ('north', 'east', 'south', 'west')
VIEWS = {'A': '入口斜俯视', 'B': '入口平视', 'C': '侧向空间', 'D': '后区回望'}
EXAMPLE = '26年春季佛事展设计方案/室内平面尺寸图.jpg'
GUIDE_VERSION = 4


def spec_value(value):
    if not isinstance(value, dict):
        raise ValueError('空间参数格式无效')
    result = {}
    for key, lower, upper in (('width_mm', 1000, 40_000), ('length_mm', 1000, 40_000), ('height_mm', 1800, 8000)):
        number = value.get(key)
        if not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number) or not lower <= number <= upper:
            raise ValueError('净宽、净长或层高超出试做范围')
        result[key] = round(number, 3)
        result[key + '_source'] = clean(value.get(key + '_source'), 200)
        if not result[key + '_source']:
            raise ValueError('请填写每项尺寸的依据')
        if key != 'height_mm':
            kind = value.get(key + '_kind')
            if kind not in SOURCES:
                raise ValueError('请明确净宽、净长的来源类型')
            result[key + '_kind'] = kind
    if value.get('height_kind') not in ('known', 'assumed'):
        raise ValueError('请说明层高是已知值还是试做假设')
    if value['height_kind'] == 'known':
        if value.get('height_mm_kind') not in SOURCES:
            raise ValueError('请明确已知层高的来源类型')
        result['height_mm_kind'] = value['height_mm_kind']
    if value['height_kind'] == 'assumed' and value.get('accept_assumption') is not True:
        raise ValueError('请确认试做层高假设，不会标作实测尺寸')
    open_sides = value.get('open_sides')
    if (not isinstance(open_sides, list) or len(open_sides) > 4 or any(v not in SIDES for v in open_sides)
            or len(set(open_sides)) != len(open_sides)):
        raise ValueError('开口边设置无效')
    if value.get('view') not in VIEWS or value.get('layout') not in ('empty', 'booth'):
        raise ValueError('请确认视角和空间模板')
    if value['layout'] == 'booth' and (result['width_mm'] < 5500 or result['length_mm'] < 7000):
        raise ValueError('展位布置模板需至少5.5×7米，小空间请选空矩形模板')
    result.update(height_kind=value['height_kind'], open_sides=sorted(open_sides), view=value['view'], layout=value['layout'],
                  layout_source='源图目视整理；家具尺寸和位置为近似布置，椅座高480mm、靠背高880mm为试做假设，非CAD测量' if value['layout'] == 'booth' else '空矩形空间，布置待设计',
                  schema=1)
    return result


def view_issue(spec):
    if isinstance(spec, dict) and spec.get('view') == 'B' and (
            not isinstance(spec.get('open_sides'), list) or 'north' not in spec['open_sides']):
        return 'B 平视机位在平面图上边外侧，当前完整墙面会挡住室内。请按真实开口核对上边，或改选 C、D 室内视角；不要为出图虚设开口。'
    return ''


def chair(name, x, y, radius, outward):
    color = (175, 169, 157)
    parts = [dict(box=[x - radius, y - radius, .43, x + radius, y + radius, .48])]
    dx, dy = outward
    if abs(dx) > abs(dy):
        bx, bxx = (x + radius - .05, x + radius) if dx > 0 else (x - radius, x - radius + .05)
        back = [bx, y - radius, .48, bxx, y + radius, .88]
    else:
        by, byy = (y + radius - .05, y + radius) if dy > 0 else (y - radius, y - radius + .05)
        back = [x - radius, by, .48, x + radius, byy, .88]
    parts.append(dict(box=back))
    inset, half = radius - .055, .0175
    for lx, ly in ((-inset, -inset), (inset, -inset), (-inset, inset), (inset, inset)):
        parts.append(dict(box=[x + lx - half, y + ly - half, 0, x + lx + half, y + ly + half, .43]))
    return dict(name=name, box=[x - radius, y - radius, 0, x + radius, y + radius, .88], color=color, parts=parts)


def geometry(spec):
    w, length, h = (spec[k] / 1000 for k in ('width_mm', 'length_mm', 'height_mm'))
    objects = [dict(name='地面', box=[0, 0, -.12, w, length, 0], color=(222, 221, 212))]
    walls = {'north': [0, -.2, 0, w, 0, h], 'east': [w, 0, 0, w + .2, length, h],
             'south': [0, length, 0, w, length + .22, h], 'west': [-.2, 0, 0, 0, length, h]}
    for side, box in walls.items():
        if side not in spec['open_sides']:
            objects.append(dict(name=side + '墙', box=box, color=(179, 190, 184)))
    if spec['layout'] == 'booth':
        for index in range(10):
            y = length * (.025 + index * .094)
            objects.append(dict(name='展示板' + str(index + 1), box=[.08, y, .16, .22, y + length * .08, min(h - .16, 2.65)], color=(200, 195, 177)))
        spacing = max(length * .20, 2.26)
        first = max(length * .65 - spacing / 2, length * .19 + .75 + .22 + 1.08 + .10)
        for index, y in enumerate((first, first + spacing), 1):
            x, radius = max(w * .20, 1.40), min(.55, w * .075)
            objects.append(dict(name='洽谈桌' + str(index), cylinder=[x, y, 0, radius, .76], color=(160, 145, 117),
                                parts=[dict(cylinder=[x, y, .66, radius, .76]),
                                       dict(cylinder=[x, y, .10, .075, .66]),
                                       dict(cylinder=[x, y, 0, .25, .10])]))
            for dx, dy in ((-.85, 0), (.85, 0), (0, -.85), (0, .85)):
                objects.append(chair('洽谈椅', x + dx, y + dy, .23, (dx, dy)))
        x, y = w * .69, length * .19
        objects.append(dict(name='服务台', box=[x - .9, y - .43, 0, x + .9, y + .43, .76], color=(137, 157, 150)))
        for dx, dy in ((-.52, -.75), (.52, -.75), (-.52, .75), (.52, .75)):
            objects.append(chair('服务台椅', x + dx, y + dy, .22, (0, dy)))
        objects.append(dict(name='产品台', box=[w * .40, length - .98, 0, min(w - .3, w * .40 + 3.31), length - .28, .9], color=(208, 196, 170)))
    return objects


def faces(objects, *, outline_only=False):
    result = []
    for obj in objects:
        if outline_only and not obj.get('draw_edges', True):
            continue
        color = obj['color']
        if 'parts' in obj:
            result.extend(faces([dict(name=obj['name'], color=color) | part for part in obj['parts']],
                                outline_only=outline_only))
            continue
        if 'surface' in obj:
            result.append((obj['name'], obj['surface'], color))
            continue
        if 'polygon' in obj:
            z, zz = obj['z_range']
            lower = [(x, y, z) for x, y in obj['polygon']]
            upper = [(x, y, zz) for x, y in obj['polygon']]
            result.append((obj['name'], upper, color))
            if z == zz:
                continue
            for a, b, aa, bb in zip(lower, lower[1:] + lower[:1], upper, upper[1:] + upper[:1]):
                result.append((obj['name'], [a, b, bb, aa], tuple(int(c * .82) for c in color)))
            continue
        if 'box' in obj:
            x, y, z, xx, yy, zz = obj['box']
            vertices = [(x, y, z), (xx, y, z), (xx, yy, z), (x, yy, z),
                        (x, y, zz), (xx, y, zz), (xx, yy, zz), (x, yy, zz)]
            indices = [(0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7), (4, 5, 6, 7)]
            result.extend((obj['name'], [vertices[i] for i in ids], tuple(int(c * shade) for c in color))
                          for ids, shade in zip(indices, (.8, .9, .83, .75, 1)))
        else:
            x, y, z, radius, zz = obj['cylinder']
            ring = [(x + math.cos(i * math.tau / 32) * radius, y + math.sin(i * math.tau / 32) * radius, zz) for i in range(32)]
            result.append((obj['name'], ring, color))
            for a, b in zip(ring, ring[1:] + ring[:1]):
                result.append((obj['name'], [a, b, (b[0], b[1], z), (a[0], a[1], z)], tuple(int(c * .8) for c in color)))
    return result


def clip_polygon(points, plane):
    if not points:
        return []
    def distance(point):
        return sum(a * b for a, b in zip(point, plane[:3])) + plane[3]
    result = []
    previous, before = points[-1], distance(points[-1])
    for current in points:
        after = distance(current)
        if (before >= 0) != (after >= 0):
            t = before / (before - after)
            result.append(tuple(a + t * (b - a) for a, b in zip(previous, current)))
        if after >= 0:
            result.append(current)
        previous, before = current, after
    return result


def perspective(spec, objects, *, depth=False, annotate=True, camera=None, ownership=None, resolution=None,
                plan_coordinates=False):
    width, height = 1600, 1200
    if resolution is not None:
        if (camera is None or annotate or not isinstance(resolution, (tuple, list)) or len(resolution) != 2
                or any(type(value) is not int for value in resolution)
                or not 64 <= resolution[0] <= 1600 or not 48 <= resolution[1] <= 1200
                or resolution[0] * 3 != resolution[1] * 4):
            raise ValueError('结构预览尺寸无效')
        width, height = resolution
    w, length = spec['width_mm'] / 1000, spec['length_mm'] / 1000
    room = max(w, length)
    views = {'A': ((w + room * .9, -room * .55, max(room * .82, spec['height_mm'] / 1000 * 1.2)), (w * .42, length * .50, .8)),
             'B': ((w * .5, -room * .65, 1.8), (w * .5, length * .58, 1.35)),
             'C': ((w * .3, length * .03, 1.75), (w * .22, length * .9, 1.15)),
             'D': ((w * .75, length * .95, 1.75), (w * .4, length * .12, 1.15))}
    eye, target = views[spec['view']]
    if camera is not None:
        if annotate or not isinstance(camera, dict) or set(camera) != {'eye', 'target', 'fov_degrees'}:
            raise ValueError('自定义机位仅用于无文字的结构参考')
        eye, target = camera['eye'], camera['target']
        if any(not isinstance(point, (tuple, list)) or len(point) != 3 or any(
                not isinstance(n, (int, float)) or isinstance(n, bool) or not math.isfinite(n)
                for n in point) for point in (eye, target)):
            raise ValueError('结构参考机位无效')
        fov = camera['fov_degrees']
        if (not isinstance(fov, (int, float)) or isinstance(fov, bool) or not math.isfinite(fov)
                or not 30 <= fov <= 140 or math.hypot(target[0] - eye[0], target[1] - eye[1]) < 1e-6):
            raise ValueError('结构参考视角无效')
    def norm(v):
        divisor = math.sqrt(sum(c * c for c in v)); return tuple(c / divisor for c in v)
    def dot(a, b): return sum(x * y for x, y in zip(a, b))
    forward = norm(tuple(b - a for a, b in zip(eye, target)))
    # A plan image has rightward X and downward Y, with upward scene height.
    # Keep the original Cartesian convention for older stored view bindings.
    right = norm((-forward[1], forward[0], 0) if plan_coordinates else (forward[1], -forward[0], 0))
    up = (right[1] * forward[2], -right[0] * forward[2], right[0] * forward[1] - right[1] * forward[0])
    if plan_coordinates:
        up = tuple(-value for value in up)
    def camera_point(point):
        delta = tuple(p - e for p, e in zip(point, eye)); z = dot(delta, forward)
        return dot(delta, right), -dot(delta, up), z
    interior = camera is not None or spec['view'] in ('C', 'D')
    horizontal = math.tan(math.radians(camera['fov_degrees'] / 2 if camera is not None else 40))
    focal = (width if camera is not None else 1400) / (2 * horizontal)
    vertical = (height if camera is not None else 900) / (2 * focal)
    planes = [(0, 0, 1, -.05)]
    if interior:
        planes += [(1, 0, horizontal, 0), (-1, 0, horizontal, 0),
                   (0, 1, vertical, 0), (0, -1, vertical, 0)]
    polygons = []
    outlined = {(name, tuple(points)) for name, points, _ in faces(objects, outline_only=True)}
    for name, points, color in faces(objects):
        clipped = [camera_point(point) for point in points]
        for plane in planes:
            clipped = clip_polygon(clipped, plane)
        if len(clipped) < 3:
            continue
        projected = [(x / max(.05, z), y / max(.05, z), max(.05, z)) for x, y, z in clipped]
        polygons.append((sum(p[2] for p in projected) / len(projected), name, projected, color,
                         (name, tuple(points)) in outlined))
    bounds = [point for _, _, points, _, _ in polygons for point in points]
    if not bounds:
        raise ValueError('视角未覆盖空间，请换一个视角')
    if interior:
        minx, maxx, miny, maxy = -horizontal, horizontal, -vertical, vertical
        scale = focal
    else:
        minx, maxx = min(v[0] for v in bounds), max(v[0] for v in bounds)
        miny, maxy = min(v[1] for v in bounds), max(v[1] for v in bounds)
        scale = min(1400 / max(1e-9, maxx - minx), 900 / max(1e-9, maxy - miny))
    image = Image.new('RGB', (width, height), '#f5f3ed' if not depth else '#ffffff')
    pixels = np.array(image)
    zbuffer = np.full((height, width), np.inf, dtype=np.float64)
    owner_ids = {name: index + 1 for index, name in enumerate(dict.fromkeys(row[1] for row in polygons))}
    owners = np.zeros(zbuffer.shape, dtype=np.int32) if ownership is not None else None
    projected_faces = []
    for _, name, points, color, draw_edges in polygons:
        projected = np.array([(width / 2 + (p[0] - (minx + maxx) / 2) * scale,
                               (height / 2 if camera is not None else 622) + (p[1] - (miny + maxy) / 2) * scale, p[2]) for p in points])
        if draw_edges:
            projected_faces.append(projected)
        for index in range(1, len(projected) - 1):
            triangle = projected[[0, index, index + 1]]
            a, b, c = triangle
            xmin, xmax = max(0, math.floor(triangle[:, 0].min())), min(width - 1, math.ceil(triangle[:, 0].max()))
            ymin, ymax = max(0, math.floor(triangle[:, 1].min())), min(height - 1, math.ceil(triangle[:, 1].max()))
            denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if abs(denominator) < 1e-8 or xmin > xmax or ymin > ymax:
                continue
            ys, xs = np.mgrid[ymin:ymax + 1, xmin:xmax + 1]
            u = ((b[1] - c[1]) * (xs - c[0]) + (c[0] - b[0]) * (ys - c[1])) / denominator
            v = ((c[1] - a[1]) * (xs - c[0]) + (a[0] - c[0]) * (ys - c[1])) / denominator
            t = 1 - u - v
            reciprocal = u / a[2] + v / b[2] + t / c[2]
            distances = 1 / np.maximum(reciprocal, 1e-12)
            old = zbuffer[ymin:ymax + 1, xmin:xmax + 1]
            visible = (u >= -1e-6) & (v >= -1e-6) & (t >= -1e-6) & (distances < old)
            old[visible] = distances[visible]
            pixels[ymin:ymax + 1, xmin:xmax + 1][visible] = color
            if owners is not None:
                owners[ymin:ymax + 1, xmin:xmax + 1][visible] = owner_ids[name]
    if ownership is not None:
        ownership.clear()
        for name, code in owner_ids.items():
            ys, xs = np.nonzero(owners == code)
            if len(xs):
                ownership[name] = dict(pixels=len(xs),
                    bounds=[float(xs.min()) / width, float(ys.min()) / height,
                            float(xs.max() + 1) / width, float(ys.max() + 1) / height],
                    center=[float(xs.mean()) / width, float(ys.mean()) / height])
    if depth:
        visible = np.isfinite(zbuffer)
        if not visible.any():
            raise ValueError('视角未覆盖空间，请换一个视角')
        near, far = zbuffer[visible].min(), zbuffer[visible].max()
        shade = np.zeros(zbuffer.shape, dtype=np.uint8)
        shade[visible] = (50 + 170 * (zbuffer[visible] - near) / max(.001, far - near)).astype(np.uint8)
        pixels[visible] = np.repeat(shade[visible, None], 3, axis=1)
    else:
        for face in projected_faces:
            for a, b in zip(face, np.roll(face, -1, axis=0)):
                t = np.linspace(0, 1, max(2, int(np.max(np.abs(a[:2] - b[:2]))) + 1))
                xs = np.rint(a[0] + (b[0] - a[0]) * t).astype(int)
                ys = np.rint(a[1] + (b[1] - a[1]) * t).astype(int)
                distance = 1 / ((1 - t) / a[2] + t / b[2])
                valid = (xs >= 0) & (xs < width) & (ys >= 0) & (ys < height)
                xs, ys, distance = xs[valid], ys[valid], distance[valid]
                front = distance <= zbuffer[ys, xs] + .025
                pixels[ys[front], xs[front]] = (114, 121, 112)
    image = Image.fromarray(pixels)
    draw = ImageDraw.Draw(image)
    if not depth and annotate:
        draw.text((62, 24), f'结构底稿 / {spec["view"]} · {VIEWS[spec["view"]]}', font=cjk_font(32), fill='#29433b')
        draw.text((62, 73), f'净宽 {spec["width_mm"]:g} × 净长 {spec["length_mm"]:g} mm', font=cjk_font(23), fill='#70786b')
        label = '已知层高' if spec['height_kind'] == 'known' else '层高假设 · 待确认'
        draw.text((62, 1134), f'{label} {spec["height_mm"]:g} mm；家具尺寸为布置假设，非施工模型。', font=cjk_font(23), fill='#737966')
    return image


def plan_sheet(spec, objects):
    image = Image.new('RGB', (1400, 1600), '#f7f4ec')
    draw = ImageDraw.Draw(image)
    w, length = spec['width_mm'] / 1000, spec['length_mm'] / 1000
    scale = min(950 / w, 1190 / length)
    left, top = (1400 - w * scale) / 2, 180
    def point(x, y): return left + x * scale, top + y * scale
    draw.rectangle([point(0, 0), point(w, length)], fill='#e5e4d8', outline='#a4aa9d', width=2)
    for obj in objects[1:]:
        if 'box' in obj:
            x, y, _, xx, yy, _ = obj['box']
            draw.rectangle([point(x, y), point(xx, yy)], fill=obj['color'], outline='#6e796f', width=2)
        else:
            x, y, _, radius, _ = obj['cylinder']
            draw.ellipse([point(x - radius, y - radius), point(x + radius, y + radius)], fill=obj['color'], outline='#6e796f', width=2)
    draw.text((65, 32), '平面复核底稿', font=cjk_font(35), fill='#29433b')
    origins = f'净宽：{spec.get("width_mm_kind", "来源未记录")}；净长：{spec.get("length_mm_kind", "来源未记录")}。'
    layout_note = '家具布置、尺寸为试做假设。' if spec['layout'] == 'booth' else '未预设家具。'
    draw.text((65, 92), origins + layout_note, font=cjk_font(24), fill='#71786c')
    draw.text((left + w * scale / 2 - 95, top - 48), f'{spec["width_mm"]:g} mm', font=cjk_font(27), fill='#29433b')
    draw.text((left + w * scale + 25, top + length * scale / 2), f'{spec["length_mm"]:g} mm', font=cjk_font(27), fill='#29433b')
    draw.text((65, 1480), '开口边：' + ' / '.join({'north': '上', 'east': '右', 'south': '下', 'west': '左'}[v] for v in spec['open_sides']), font=cjk_font(24), fill='#29433b')
    draw.text((65, 1522), '不是自动识图结果；非矩形、多房间、门窗及细部需另行建模。', font=cjk_font(22), fill='#737966')
    return image


def prepare_guides(task, data, output):
    source_id = ident(data.get('source_id'))
    source = next((row for row in task.get('edit_assets', []) if row['id'] == source_id), None)
    if source is None:
        raise ValueError('请先上传当前设计的平面图')
    output = Path(output).resolve(strict=True)
    root = output / '产品设计' / ident(task['id'])
    path = (output / source['file']).resolve(strict=True)
    if not path.is_relative_to(root.resolve()):
        raise ValueError('平面图来源无效')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if data.get('source_sha256') != digest:
        raise ValueError('平面图已变化，请重新确认')
    spec = spec_value(data.get('spec'))
    if issue := view_issue(spec):
        raise ValueError(issue)
    fingerprint = hashlib.sha256(json.dumps({'source': digest, 'spec': spec, 'guide_version': GUIDE_VERSION}, sort_keys=True).encode()).hexdigest()
    existing = task.get('floorplan')
    if existing and existing.get('fingerprint') == fingerprint:
        try:
            complete = len(existing['guides']) == 3 and all(
                not (output / v['file']).is_symlink()
                and (output / v['file']).resolve(strict=True).is_relative_to((output / '产品设计' / ident(task['id'])).resolve())
                and hashlib.sha256((output / v['file']).read_bytes()).hexdigest() == v['source_sha256']
                for v in existing['guides'])
        except (OSError, KeyError, TypeError):
            complete = False
        if complete:
            return False
    objects = geometry(spec)
    renders = [('plan', '平面复核底稿', plan_sheet(spec, objects)),
               ('structure', '结构底稿 · ' + VIEWS[spec['view']], perspective(spec, objects)),
               ('depth', '结构深度底稿 · ' + VIEWS[spec['view']], perspective(spec, objects, depth=True))]
    batch = uuid.uuid4().hex
    folder = root / batch
    for parent in (root.parent, root, folder):
        if parent.is_symlink():
            raise ValueError('结构底稿目录无效')
    folder.mkdir(parents=True, mode=0o700)
    guides = []
    try:
        for kind, name, image in renders:
            target = folder / (kind + '.png')
            with target.open('xb') as stream:
                target.chmod(0o600); image.save(stream, format='PNG')
            guides.append(dict(id=uuid.uuid4().hex, file=target.relative_to(output).as_posix(), name=name, guide_kind=kind,
                               source_sha256=hashlib.sha256(target.read_bytes()).hexdigest(), width=image.width, height=image.height,
                               upload_mode='reference', created=stamp()))
    except Exception:
        for child in folder.iterdir():
            if child.is_file(): child.unlink()
        folder.rmdir()
        raise
    if existing:
        task.setdefault('floorplan_history', []).append(existing)
    task.setdefault('edit_assets', []).extend(guides)
    task['floorplan'] = dict(spec=spec, source_id=source_id, source_sha256=digest, guides=guides, guide_version=GUIDE_VERSION,
                            geometry=objects, fingerprint=fingerprint, created=stamp())
    return True


def constraints(record):
    spec = record['spec']
    names = {'north': '平面图上边', 'east': '平面图右边', 'south': '平面图下边', 'west': '平面图左边'}
    opening = '、'.join(names[side] for side in spec['open_sides']) or '无整面开口'
    return (f'空间净宽{spec["width_mm"]:g}mm、净长{spec["length_mm"]:g}mm；层高{spec["height_mm"]:g}mm'
            + ('为试做假设，待确认。' if spec['height_kind'] == 'assumed' else '，依据已记录。')
            + f'开口边为{opening}，其他边为墙面。按结构底稿的{VIEWS[spec["view"]]}相机出图，保持墙体与开口关系；不要新增门窗。'
            + ('保留已确认的整面开口，不将开放空间封闭。' if spec['open_sides'] else '四边均为完整墙面，不新增对外开口。')
            + ('家具数量和轮廓按灰模。以下位置均按原平面图的方向描述，不是当前镜头的画面左右；换视角时不要镜像、搬移或复制家具。'
               '原平面图左边靠墙一整列10块竖向展示板；原平面图左后方恰好两组圆形洽谈桌，每组四把椅子；'
               '原平面图右前方是一张长方形服务台，保持方形轮廓和四把椅子，严禁变成第三组圆桌；'
               '原平面图后墙产品台保持灰模中的长度与位置，不扩展为整墙长柜。'
               '这些是整个布置的总数；当前视角有遮挡时，不为凑齐可见数量而新增家具。保持中部通道空旷。家具为近似布置。'
               if spec['layout'] == 'booth' else '底稿未预设家具。是否添加家具及其数量、位置以本次设计要求为准；未要求时保持为空，不从风格参考图复制家具或门窗。')
            + '平面图、灰模和深度底稿是结构参考，不能当成最终材质。其他风格图只参考材质与光线，不参考其房间布局。按本次设计说明与所选风格输出一张完整空间效果图，不输出平面图、图纸、文字或拼图。')


def validate_basis(record, task, output):
    output = Path(output).resolve(strict=True)
    rows = {row['id']: row for row in task.get('edit_assets', [])}
    ids = [record['source_id'], *record['guide_ids']]
    if len(record['guide_ids']) != 3 or len(set(ids)) != 4:
        raise ValueError('结构底稿记录无效')
    for asset_id in ids:
        row = rows.get(ident(asset_id))
        if row is None:
            raise ValueError('结构底稿已缺失，请重新准备')
        path = (output / row['file']).resolve(strict=True)
        if not path.is_relative_to((output / '产品设计' / ident(task['id'])).resolve()):
            raise ValueError('结构底稿路径无效')
        digest = record['source_sha256'] if asset_id == record['source_id'] else row['source_sha256']
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('原图或结构底稿已变化，未继续出图')


def structure_anchor(record, task, output):
    validate_basis(record, task, output)
    if issue := view_issue(record.get('spec')):
        raise ValueError(issue + ' 请重新准备视角明确的底稿；已有记录保留。')
    anchors = [row for row in task.get('edit_assets', [])
               if row['id'] in record['guide_ids'] and row.get('guide_kind') == 'structure']
    if len(anchors) != 1:
        raise ValueError('本版结构灰模已缺失，请重新准备底稿')
    return anchors[0]
