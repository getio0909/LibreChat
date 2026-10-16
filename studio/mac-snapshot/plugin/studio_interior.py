"""Photographic interior requests with an explicitly bound original floor plan."""
import hashlib
import io
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from .studio_store import clean, ident
from .studio_layout import (OBJECT_LABELS, analyze_plan, coverage_views, refresh_boundary_geometry,
                            render_view, validate_finish_scheme, validate_layout, view_object_anchors)

LAYOUT_VERSION = 9


def reference_digest(path, verified_files=None):
    path = Path(path).resolve(strict=True)
    if verified_files is None:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if path not in verified_files:
        verified_files[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return verified_files[path]


def interior_plan(task, value, output, *, verified_files=None):
    source_id = ident(value.get('source_id'))
    digest = value.get('source_sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('请重新核对上传的平面图')
    source = next((row for row in task.get('edit_assets', []) if row['id'] == source_id), None)
    if source is None or source.get('guide_kind'):
        raise ValueError('请选择上传的原始平面图，不能使用灰模或深度底稿')
    relative = Path(source['file'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('平面图文件路径无效')
    path = Path(output) / relative
    folder = Path(output) / '产品设计' / ident(task['id'])
    if folder.is_symlink() or folder.parent.is_symlink() or path.is_symlink() or not path.resolve(strict=True).is_relative_to(folder.resolve(strict=True)):
        raise ValueError('平面图文件归属无效')
    if reference_digest(path, verified_files) != digest:
        raise ValueError('平面图已变化，未提交请求，请重新核对原图')
    return path.resolve(strict=True)


def interior_layout_reference(task, value, output, *, verified_files=None):
    """Resolve the immutable view guide, or the original plan for older versions."""
    original = interior_plan(task, value, output, verified_files=verified_files)
    if value.get('reference_kind') != 'derived_structure':
        return original
    asset_id = ident(value.get('structure_id'))
    digest = value.get('structure_sha256')
    asset = next((row for row in task.get('edit_assets', []) if row['id'] == asset_id), None)
    if (not asset or not asset.get('internal_layout') or asset.get('guide_kind') != 'interior_structure'
            or asset.get('layout_id') != value.get('layout_id')
            or asset.get('plan_sha256') != value['source_sha256']
            or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)):
        raise ValueError('这一版的空间布局依据未能读取，已保留原图和已有结果')
    relative = Path(asset['file'])
    folder = Path(output) / '产品设计' / ident(task['id'])
    path = Path(output) / relative
    if (relative.is_absolute() or '..' in relative.parts or path.is_symlink()
            or any(p.is_symlink() for p in path.parents if p != Path(output).parent)
            or not path.resolve(strict=True).is_relative_to(folder.resolve(strict=True))
            or reference_digest(path, verified_files) != digest):
        raise ValueError('这一版的空间布局依据已变化，未重新生成')
    return path.resolve(strict=True)


def layout_digest(layout):
    value = validate_layout(layout)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


OBJECT_FORMS = {
    'bed': '低矮板式床架与平直矩形床头',
    'sofa': '直线型软包沙发，连续靠背、简洁扶手与木质底座',
    'chair': '简洁座面与靠背、木质框架',
    'table': '矩形平直桌面与简洁直腿',
    'round_table': '圆形平直桌面与简洁中央支座',
    'cabinet': '平直矩形柜体与齐平柜门',
    'wardrobe': '平直矩形衣柜与齐平柜门',
}
OBJECT_TARGETS = {
    'bed': ('床', '床头', '床架', '床体', '床品'),
    'sofa': ('沙发', '沙发扶手', '沙发面料'),
    'chair': ('椅子', '座椅', '餐椅'),
    'table': ('长方桌', '长方形桌'),
    'round_table': ('圆桌', '圆形桌'),
    'cabinet': ('柜体', '柜子'),
    'wardrobe': ('衣柜',),
}


def _object_reference_scope(layout, target):
    if not target:
        return 'global', None
    spaces = {row['id']: row['label'] for row in layout['spaces']}
    terms = {name for names in OBJECT_TARGETS.values() for name in names} | set(OBJECT_LABELS) | {'餐桌', '茶几'}
    reserved = terms | {label + separator + name for label in spaces.values()
                        for separator in ('', '的') for name in terms}
    matches = []
    for row in layout['objects']:
        names = OBJECT_TARGETS.get(row['kind'], ()) + (row['kind'],)
        label = spaces[row['space_id']]
        if row['kind'] in ('table', 'round_table'):
            if label in ('餐厅', '餐区', '餐室', '用餐区'):
                names += ('餐桌',)
            elif label in ('客厅', '起居室', '会客厅', '会客区'):
                names += ('茶几',)
        qualified = any(target in (label + name, label + '的' + name) for name in names)
        if qualified and list(spaces.values()).count(label) > 1:
            return 'unresolved', None
        if target in names or qualified:
            matches.append(row['id'])
    if target not in reserved:
        matches.extend(row['id'] for row in layout['objects'] if row['id'] == target)
    return ('object', matches[0]) if len(matches) == 1 else ('unresolved', None)


def resolve_interior_object_scheme(layout, style_label, edits, finish_scheme):
    """Freeze chosen object defaults and explicit reference assignments per batch."""
    layout = validate_layout(layout)
    finishes = validate_finish_scheme(finish_scheme, style_label)['materials']
    objects = []
    for row in layout['objects']:
        defaults = {}
        if style_label in ('自然写实', '现代简约', '现代木作', '现代轻奢', '中式雅致') and row['kind'] in OBJECT_FORMS:
            keys = ('wood', 'fabric') if row['kind'] in ('bed', 'sofa', 'chair') else ('wood',)
            defaults = dict(form=OBJECT_FORMS[row['kind']],
                            materials={key: finishes[key] for key in keys if key in finishes})
        objects.append(dict(id=row['id'], kind=row['kind'], space_id=row['space_id'], defaults=defaults))
    references = []
    for slot, row in enumerate(edits[1:], 1):
        scope, object_id = _object_reference_scope(layout, row.get('target', ''))
        references.append(dict(slot=slot, source=row['source'], id=row['id'], scope=scope, object_id=object_id))
    return validate_interior_object_scheme(dict(schema=1, layout_sha256=layout_digest(layout),
        style_label=style_label, objects=objects, references=references), layout, style_label, edits)


def validate_interior_object_scheme(value, layout, style_label, edits=None):
    """Read frozen object data without resolving current design defaults."""
    message = '这套图的家具对应记录未能读取，已有结果仍保留'
    layout = validate_layout(layout)
    if (not isinstance(value, dict) or set(value) != {'schema', 'layout_sha256', 'style_label', 'objects', 'references'}
            or type(value.get('schema')) is not int or value['schema'] != 1
            or value.get('layout_sha256') != layout_digest(layout) or value.get('style_label') != style_label
            or not isinstance(value.get('objects'), list) or not isinstance(value.get('references'), list)
            or len(value['objects']) != len(layout['objects'])):
        raise ValueError(message)
    objects, identities = [], {row['id']: row for row in layout['objects']}
    seen = set()
    for row in value['objects']:
        if not isinstance(row, dict) or set(row) != {'id', 'kind', 'space_id', 'defaults'}:
            raise ValueError(message)
        if not isinstance(row['id'], str):
            raise ValueError(message)
        source = identities.get(row.get('id'))
        if (source is None or row['id'] in seen or any(row[key] != source[key] for key in ('kind', 'space_id'))
                or not isinstance(row['defaults'], dict) or set(row['defaults']) - {'form', 'materials'}):
            raise ValueError(message)
        seen.add(row['id'])
        defaults = row['defaults']
        if defaults:
            if set(defaults) != {'form', 'materials'} or not isinstance(defaults['materials'], dict):
                raise ValueError(message)
            if set(defaults['materials']) - {'wood', 'fabric'}:
                raise ValueError(message)
            validate_finish_scheme(dict(schema=1, style_label=style_label, materials=defaults['materials']), style_label)
            if (not isinstance(defaults['form'], str) or not defaults['form'].strip() or len(defaults['form']) > 200
                    or any(ord(char) < 32 for char in defaults['form'])):
                raise ValueError(message)
        objects.append(dict(id=row['id'], kind=row['kind'], space_id=row['space_id'],
                            defaults=json.loads(json.dumps(defaults))))
    references = []
    for slot, row in enumerate(value['references'], 1):
        if (not isinstance(row, dict) or set(row) != {'slot', 'source', 'id', 'scope', 'object_id'}
                or type(row.get('slot')) is not int or row['slot'] != slot
                or row.get('source') not in ('upload', 'reference', 'material', 'image')
                or not isinstance(row.get('id'), str) or not row['id']
                or row.get('scope') not in ('global', 'object', 'unresolved')
                or (not isinstance(row['object_id'], str) or row['object_id'] not in identities
                    if row['scope'] == 'object' else row['object_id'] is not None)):
            raise ValueError(message)
        references.append(dict(row))
    if edits is not None:
        if len(references) != len(edits) - 1:
            raise ValueError(message)
        for reference, row in zip(references, edits[1:]):
            if reference['source'] != row['source'] or reference['id'] != row['id']:
                raise ValueError(message)
    return dict(schema=1, layout_sha256=value['layout_sha256'], style_label=style_label,
                objects=objects, references=references)


def interior_object_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def interior_object_text(value, layout, view, edits, style_label, *, anchors=None):
    scheme = validate_interior_object_scheme(value, layout, style_label, edits)
    if anchors is None:
        anchors = view_object_anchors(layout, view, include_neighbors=True)
    anchors = {row['id']: row for row in anchors}
    spaces = {row['id']: row['label'] for row in layout['spaces']}
    lines = []
    for row in scheme['objects']:
        if row['id'] not in anchors:
            continue
        anchor = anchors[row['id']]
        name = spaces[row['space_id']] + '的' + OBJECT_LABELS[row['kind']]
        x, y = anchor['center']
        pieces = [f"同一物件 {row['id']}（{name}），本机位可见部分在横向{round(x * 100)}%、纵向{round(y * 100)}%附近"]
        if row['defaults']:
            pieces.append('本套选定款式：' + row['defaults']['form'])
            materials = row['defaults']['materials']
            if materials:
                pieces.append('本套选定饰面：' + '、'.join(materials.values()))
        if not anchor['is_current_space']:
            pieces.append('仍属于相邻空间，保留当前遮挡和裁切')
        lines.append('；'.join(pieces) + '。')
    _, indices = interior_reference_layout(edits)
    for reference in scheme['references']:
        if reference['scope'] != 'object':
            continue
        source = next(row for row in scheme['objects'] if row['id'] == reference['object_id'])
        edit = edits[reference['slot']]
        images = '、'.join('图' + str(number) for number in indices[reference['slot']])
        name = spaces[source['space_id']] + '的' + OBJECT_LABELS[source['kind']]
        role = edit.get('take') or {'style': '配色、装饰语言与空间氛围', 'material': '材质颜色和纹理',
            'object': '指定家具或陈设的形态', 'shape': '指定造型细节', 'pattern': '指定纹样',
            'reference': '设计说明指定的内容'}[edit.get('use', 'reference')]
        lines.append(f"{images}只向同一物件 {source['id']}（{name}）提供{role}，"
                     f"取用部位仍为{edit.get('target', name)}；换机位后沿用这一对应，不应用到其他同类家具。"
                     + ('本机位没有露出该物件，不为展示参考而将它加入画面。' if source['id'] not in anchors else ''))
    if not lines:
        return ''
    return ('本套家具对应：同一物件在各机位中沿用同一款式与饰面；以下款式是选定的设计默认值，'
            '不是平面图测量结果。补充照片、取用部位和设计要求明确指定的部分优先，其他部分沿用本套默认值。'
            '内部编号只用于物件对应，成图不显示编号、文字或位置数字。' + ''.join(lines))


def interior_view_binding(record, view):
    layout = validate_layout(record['analysis']['layout'])
    space = next(row for row in layout['spaces'] if row['id'] == view['space_id'])
    return dict(layout_id=ident(record['id']), layout_version=record['version'],
                layout_sha256=layout_digest(layout), layout_snapshot=layout,
                space_id=view['space_id'], space_label=space['label'], view_id=view['id'], view_label=view['label'],
                camera=view['camera'], structure_id=ident(view['asset_id']), structure_sha256=view['asset_sha256'],
                reference_kind='derived_structure', original_plan_sent=False)


def room_first_views(views):
    """Generate one view of each room before returning for its other views."""
    rooms = {}
    for view in views:
        rooms.setdefault(view['space_id'], []).append(view)
    return [room[index] for index in range(max(map(len, rooms.values()), default=0))
            for room in rooms.values() if index < len(room)]


def interior_unresolved_states(task, concept_ids):
    """Retained requests remain unresolved until their outcome is recorded."""
    concept_ids = set(concept_ids)
    states = {}
    priority = {'running': 1, 'uncertain': 2, 'registration-pending': 3}
    history = task.get('cloud_job_history') or []
    if not isinstance(history, list):
        raise ValueError('生成记录未能读取，已有图片仍保留')
    def timestamp(value):
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            return None
    created = {row['id']: timestamp(row.get('created')) for row in task.get('concepts', [])}
    for job in [task.get('job') or {}, *history]:
        if not isinstance(job, dict) or job.get('items') is not None and not isinstance(job.get('items'), dict):
            raise ValueError('生成记录未能读取，已有图片仍保留')
        items = job.get('items') or {}
        for concept_id, state in items.items():
            if concept_id in concept_ids and state in priority and priority[state] > priority.get(states.get(concept_id), 0):
                states[concept_id] = state
        if job.get('action') == 'design' and job.get('status') == 'uncertain' and not items:
            if 'concept_ids' in job:
                scope = job['concept_ids']
                if not isinstance(scope, list) or any(not isinstance(value, str) for value in scope):
                    raise ValueError('生成记录未能读取，已有图片仍保留')
                affected = concept_ids.intersection(scope)
            else:
                cutoffs = [timestamp(job.get(key)) for key in ('finished', 'retained_at', 'continued_at')]
                cutoffs = [value for value in cutoffs if value is not None]
                cutoff = min(cutoffs) if cutoffs else None
                affected = {value for value in concept_ids
                            if cutoff is None or created.get(value) is None or created[value] <= cutoff}
            for concept_id in affected:
                states.setdefault(concept_id, 'uncertain')
    return states


def interior_resume_concepts(task, batch_id):
    """Select missing views without resending an unresolved request."""
    batch_id = ident(batch_id)
    concepts = [row for row in task.get('concepts', []) if row.get('batch') == batch_id
                and row.get('reference_design') and row.get('interior_photographic')
                and isinstance(row.get('interior_view'), dict) and row['interior_view']]
    if not concepts:
        raise ValueError('找不到这套室内视角，请刷新后再试')
    completed = {row['concept_id'] for row in task.get('images', [])}
    missing = [row for row in concepts if row['id'] not in completed]
    if not missing:
        raise ValueError('这套图已经生成完成')
    unresolved = interior_unresolved_states(task, (row['id'] for row in missing))
    eligible = [row for row in missing if row['id'] not in unresolved]
    if not eligible:
        raise ValueError('未完成图片的结果尚未确认，请先恢复已有结果')
    return eligible


def validate_frozen_design_references(task, concept, resolved, output, expected_edits=None, *, verified_files=None):
    """Keep a saved round's reference contents fixed during retry or resume."""
    snapshot = concept.get('reference_snapshot') or []
    if not isinstance(snapshot, list) or any(not isinstance(row, dict) for row in snapshot):
        raise ValueError('这套图的参考记录未能读取，已有结果仍保留')
    expected_catalog = [(row.get('id'), row.get('content_sha256')) for row in snapshot]
    actual_catalog = [(ref.get('id'), ref.get('content_sha256'))
                      for row in resolved for ref in row.get('references', [])]
    if (any(not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
            for _, digest in expected_catalog) or actual_catalog != expected_catalog):
        raise ValueError('这套图使用的参考照片已变化，请保留已有结果并重新选择参考')
    if expected_edits is not None and (not isinstance(expected_edits, list) or len(expected_edits) != len(resolved)
                                       or any(not isinstance(row, dict) for row in expected_edits)):
        raise ValueError('这套图的参考设置未能读取，已有结果仍保留')
    for index, row in enumerate(resolved):
        paths = [Path(path) for path in row['paths']]
        if row['source'] in ('reference', 'material'):
            if len(paths) != len(row.get('references', [])):
                raise ValueError('这套图的参考设置未能读取，已有结果仍保留')
            for path, ref in zip(paths, row['references']):
                if reference_digest(path, verified_files) != ref['content_sha256']:
                    raise ValueError('这套图使用的参考照片已变化，请重新选择参考')
            continue
        original = next((entry for entry in task.get('edit_assets' if row['source'] == 'upload' else 'images', [])
                         if entry['id'] == row['id']), None)
        saved = expected_edits[index] if expected_edits is not None else {}
        if saved and (saved.get('source'), saved.get('id')) != (row['source'], row['id']):
            raise ValueError('这套图的参考设置已变化，已有结果仍保留')
        digest = saved.get('source_file_sha256') or (original or {}).get('content_sha256')
        if (len(paths) != 1 or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest)
                or original is None or paths[0].resolve(strict=True) != (Path(output) / original['file']).resolve(strict=True)
                or reference_digest(paths[0], verified_files) != digest):
            raise ValueError('这套图使用的上传照片已变化，请保留已有结果并重新选择参考')


def cached_interior_layout(task, snapshot, output):
    record = task.get('interior_layout')
    if (not record or record.get('schema') != 1 or record.get('version') != LAYOUT_VERSION
            or record.get('source_id') != snapshot['source_id']
            or record.get('source_sha256') != snapshot['source_sha256']):
        return None
    layout = validate_layout(record['analysis']['layout'])
    views = record.get('views', [])
    if not views or {row['space_id'] for row in views} != {row['id'] for row in layout['spaces']}:
        raise ValueError('空间视角记录未能读取，原图和已有结果仍保留')
    for view in views:
        interior_layout_reference(task, {**snapshot, **interior_view_binding(record, view)}, output)
    return record


def build_interior_layout(task, snapshot, output, config_path, *, refresh=False):
    """Create operational geometry and private PNG guides; never persist requests."""
    original = interior_plan(task, snapshot, output)
    previous = None if refresh else cached_interior_layout(task, snapshot, output)
    if previous:
        return previous, []
    analysis = None
    old = task.get('interior_layout')
    if (not refresh and isinstance(old, dict) and old.get('schema') == 1 and old.get('version') in (1, 2, 3, 4, 5, 6, 7, 8)
            and old.get('source_id') == snapshot['source_id'] and old.get('source_sha256') == snapshot['source_sha256']):
        analysis = refresh_boundary_geometry(old.get('analysis'), original)
        if (analysis is not None and old['version'] < 8
                and any(row['kind'] == 'appliance' for row in analysis['layout']['objects'])):
            # Read ambiguous device symbols again; IDs are not semantic evidence.
            analysis = None
    if analysis is None:
        analysis = analyze_plan(config_path, original)
    if analysis['source_sha256'] != snapshot['source_sha256']:
        raise ValueError('平面图已变化，原图和已有结果仍保留')
    layout = validate_layout(analysis['layout'])
    record = dict(schema=1, id=uuid.uuid4().hex, version=LAYOUT_VERSION,
                  source_id=snapshot['source_id'], source_sha256=snapshot['source_sha256'],
                  analysis=analysis, views=coverage_views(layout), space_count=len(layout['spaces']))
    folder = Path(output) / '产品设计' / ident(task['id'])
    if not folder.is_dir() or any(p.is_symlink() for p in (folder, *folder.parents)):
        raise ValueError('空间视角保存目录无效')
    target_folder = folder / ('layout-' + record['id'])
    target_folder.mkdir(mode=0o700)
    assets = []
    created = []
    try:
        for view in record['views']:
            asset_id = uuid.uuid4().hex
            binary = io.BytesIO()
            render_view(layout, view).save(binary, format='PNG')
            data = binary.getvalue()
            path = target_folder / (asset_id + '.png')
            with path.open('xb') as stream:
                created.append(path)
                os.chmod(path, 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            digest = hashlib.sha256(data).hexdigest()
            view.update(asset_id=asset_id, asset_sha256=digest)
            assets.append(dict(id=asset_id, name=view['label'] + ' · 布局依据',
                               file=str(path.relative_to(Path(output))), source_sha256=digest,
                               guide_kind='interior_structure', internal_layout=True,
                               layout_id=record['id'], plan_sha256=snapshot['source_sha256']))
        record['view_count'] = len(record['views'])
        return record, assets
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        target_folder.rmdir()
        raise


def interior_input(task, value, edits, output):
    if not isinstance(value, dict) or type(value.get('schema', 1)) is not int or value.get('schema', 1) != 1:
        raise ValueError('室内设计来源记录无效')
    source_id = ident(value.get('source_id'))
    digest = value.get('source_sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('请重新核对上传的平面图')
    if task.get('floorplan'):
        raise ValueError('矩形结构试例和室内实景设计分别保存，请创建新的室内设计')
    matches = [row for row in edits if row['source'] == 'upload' and row['id'] == source_id]
    if len(matches) != 1:
        raise ValueError('本次必须完整选用一份原始平面图，不能移除或重复选用')
    plan = matches[0]
    if plan.get('source_selection') or plan.get('fidelity') != 'reference' or plan.get('use') != 'reference':
        raise ValueError('平面图只作为完整空间布局依据，不能裁成局部或当作材质物件')
    interior_plan(task, value, output)
    if plan['source_file_sha256'] != digest:
        raise ValueError('平面图已变化，未提交生图，请重新核对原图')
    label = clean(value.get('style_label', ''), 100)
    if not label:
        raise ValueError('请选择室内风格')
    record = dict(schema=1, mode='floorplan_photographic', source_id=source_id,
                  source_sha256=digest, style_label=label)
    if 'style_description' in value:
        record['style_description'] = clean(value['style_description'], 600)
    return record


def interior_reference_order(snapshot, edits):
    plan = next(row for row in edits if row['source'] == 'upload' and row['id'] == snapshot['source_id'])
    return [plan, *[row for row in edits if row is not plan]]


def interior_reference_layout(edits):
    paths, indices = [], []
    for row in edits:
        numbers = []
        for path in row['paths']:
            item = {'path': path, 'selection': row['source_selection']} if row.get('source_selection') else path
            if item not in paths:
                paths.append(item)
            numbers.append(paths.index(item) + 1)
        indices.append(numbers)
    return paths, indices


def interior_extra_text(edits):
    lines = []
    _, indices = interior_reference_layout(edits)
    for row, numbers in zip(edits[1:], indices[1:]):
        use = row.get('use', 'reference')
        role = {'style': '配色、装饰语言与空间氛围', 'material': '材质颜色和纹理',
                'object': '指定家具或陈设的形态', 'shape': '指定造型细节',
                'pattern': '指定纹样', 'reference': '设计说明指定的内容'}[use]
        take = (row.get('take') or role).rstrip('。；;')
        images = '、'.join('图' + str(number) for number in numbers)
        placement = '，应用于' + row['target'] if row.get('target') else ''
        scope = f'{images}的取用范围：{take}' if row.get('take') else f'{images}仅用于{take}'
        lines.append(f'{scope}{placement}；只取指定内容，图1仍决定布局、相机和物件位置，不复制参考图的背景或其他房间布局。')
    return '\n'.join(lines)


def interior_text(snapshot, change, edits):
    lines = [
        '生成一张室内建筑摄影效果图。图1是二维平面图，只用于理解空间轮廓、墙体、入口、门窗和已画出的家具位置。',
        '将平面关系转换为人在室内看到的完整空间；使用室内人眼高度的广角机位，保持合理透视和垂直线。',
        '画面呈现完成装修后的真实空间：自然的材质纹理、木石细节、接触阴影、反射和可信照明。',
        '不要输出平面线稿、俯视图、轴测鸟瞰、剖切房屋、缩微模型、灰模、塑料质感或多图拼贴；不要在成图上添加尺寸、标签或说明文字。',
        '以平面图的可辨认结构为布局依据，不照搬补充参考图中的房间布局。图纸未提供的细节可以合理设计，但不凭空声明尺寸或测量精度。',
        '先识别实墙、门窗和开放边界，再确定机位。实墙保持实墙；开放边界保持开放，不补成墙面或白色背景。只有图纸明确画出窗户时才设置窗户，不把沿墙的灯具、装饰条或家具误作落地窗。',
        '按所选机位转换图上的前后左右关系，不镜像或调换布局。若没有指定机位，选择能看清主要区域的入口或开放侧，朝向室内拍摄。',
        '按图中各组家具的数量、相邻关系和位置布置，不增加同款桌椅、不合并不同区域；物件可以合理遮挡，但不为了展示全部家具而改动布局。',
        '如果平面图包含多个房间，按设计说明选择主要室内空间，不拆除墙体来让所有房间同时入镜。',
        '所选风格：' + snapshot['style_label'] + '。',
        *(['风格说明：' + snapshot['style_description']] if snapshot.get('style_description') else []),
        '本次设计说明：' + change,
    ]
    lines.append(interior_extra_text(edits))
    return '\n'.join(lines)
