import hashlib
import json
import os
import re
from pathlib import Path

from .studio_store import clean, ident, stamp
from .studio_images import MAX_REFERENCES
from .studio_regions import selection_value

USES = {'reference': '参考', 'object': '物件', 'shape': '造型', 'material': '材质', 'pattern': '纹样', 'style': '风格'}
MAX_EDIT_ROWS = 32


def source_image(task, image_id):
    row = next((v for v in task['images'] if v['id'] == ident(image_id)), None)
    if row is None or not any(c['id'] == row['concept_id'] for c in task['concepts']):
        raise ValueError('请选择这份设计中已有的图片版本')
    return row


def region_value(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get('type') not in ('point', 'bbox'):
        raise ValueError('修改位置无效，请重新标记')
    keys = ('x', 'y') if value['type'] == 'point' else ('x1', 'y1', 'x2', 'y2')
    if any(not isinstance(value.get(k), int) or isinstance(value[k], bool) or not 0 <= value[k] <= 999 for k in keys):
        raise ValueError('修改位置超出图片范围')
    if value['type'] == 'bbox' and (value['x2'] - value['x1'] < 2 or value['y2'] - value['y1'] < 2):
        raise ValueError('选区太小，请重新框选')
    return {'type': value['type'], **{k: value[k] for k in keys}}


def comparison_target(value, catalog):
    value = clean(value, 40)
    names = {name for product in catalog.products() for name in [product['name'], *product['aliases'], *product.get('search_terms', [])]}
    if not value or any(name in value for name in names if name) or re.search(r'不|别|除了|去掉|移除|取消|换成|改成', value):
        raise ValueError('比较部位只写位置，例如：佛像后面的墙板。不要填写材质名或其他修改要求')
    return value


def reference_budget(edits, *, base_count=0, anchor_path=None, layout_path=None):
    sources = {(path, json.dumps(row.get('source_selection'), sort_keys=True))
               for row in edits for path in row['paths']}
    anchor = (anchor_path, 'null') if anchor_path else None
    layout = (layout_path, 'null') if layout_path else None
    reference_count = len(sources - {anchor, layout})
    structure_count = int(anchor is not None)
    plan_count = int(layout is not None)
    total = reference_count + base_count + structure_count + plan_count
    return {'maximum': MAX_REFERENCES, 'reference_count': reference_count,
            'base_count': base_count, 'structure_count': structure_count,
            'total': total, 'within_limit': total <= MAX_REFERENCES,
            **({'plan_count': plan_count} if layout is not None else {})}


def resolve_edits(task, values, catalog, output, *, compose=False, enforce_limit=True):
    if not isinstance(values, list) or len(values) > MAX_EDIT_ROWS:
        raise ValueError(f'最多保留 {MAX_EDIT_ROWS} 张参考卡片，请移除不用的卡片')
    products = None
    result = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError('图片参考格式无效')
        source, ref_id = value.get('source'), value.get('id')
        target = clean(value.get('target', ''), 160)
        region = region_value(value.get('region'))
        use = value.get('use', 'material' if source == 'material' else 'reference')
        fidelity = value.get('fidelity', 'reference')
        take = clean(value.get('take', ''), 160)
        source_selection = selection_value(value.get('source_selection'))
        target_selection = selection_value(value.get('target_selection'))
        if use not in USES or fidelity not in ('reference', 'original'):
            raise ValueError('图片用途无效')
        if fidelity == 'original' and (compose or use != 'object' or not source_selection or not target_selection):
            raise ValueError('原样放入需要选出参考物件和底图中的放置范围')
        if source == 'material':
            if products is None:
                products = {v['id']: v for v in catalog.materials()}
            product = products.get(ref_id)
            if not product or not product['available']:
                raise ValueError('石材样图暂不可用，请重新选择')
            entries = [catalog.get(v['id']) for v in product['references']]
            name, aliases = product['name'], product['aliases'] + [product['name']]
        elif source == 'reference':
            entry = catalog.get(ref_id)
            if entry['kind'] != 'image':
                raise ValueError('改图参考必须是图片')
            entries, name, aliases = [entry], entry['title'], []
        elif source == 'upload':
            asset = next((v for v in task.get('edit_assets', []) if v['id'] == ident(ref_id)), None)
            if asset is None:
                raise ValueError('这张参考照片不属于当前设计')
            path = (Path(output) / asset['file']).resolve(strict=True)
            if not path.is_relative_to((Path(output) / '产品设计' / ident(task['id'])).resolve()):
                raise ValueError('参考图片路径无效')
            entries, name, aliases = [{'source': str(path)}], asset.get('name', '上传照片'), []
        elif source == 'image':
            image = source_image(task, ref_id)
            path = (Path(output) / image['file']).resolve(strict=True)
            if not path.is_relative_to((Path(output) / '产品设计' / ident(task['id'])).resolve()):
                raise ValueError('参考图片路径无效')
            entries, name, aliases = [{'source': str(path)}], concept_label(task, image['concept_id']) + '·版本' + str(image['version']), []
        else:
            raise ValueError('图片来源无效')
        if source_selection and len(entries) != 1:
            raise ValueError('请从资料库选择一张具体照片后再选取局部')
        paths = [row['source'] for row in entries]
        selection_sha = value.get('source_sha256', '') if source_selection else ''
        if selection_sha and hashlib.sha256(Path(paths[0]).read_bytes()).hexdigest() != selection_sha:
            raise ValueError('局部参考图片已变化，请重新选取局部')
        role = use if use in ('material', 'style') else 'shape'
        references = [catalog.snapshot({'id': v['id'], 'role': USES[role] if role != 'shape' else '形态'}) for v in entries if v.get('id')]
        result.append(dict(source=source, id=ref_id, name=name, target=target, region=region, paths=paths,
                           source_sha256=selection_sha or (asset.get('source_sha256', '') if source == 'upload' else ''),
                           source_file_sha256=hashlib.sha256(Path(paths[0]).read_bytes()).hexdigest() if len(paths) == 1 else '',
                           aliases=aliases, references=references, use=use, take=take, fidelity=fidelity,
                           source_selection=source_selection, target_selection=target_selection))
    if enforce_limit and not reference_budget(result, base_count=0 if compose else 1)['within_limit']:
        raise ValueError(f'本次图片合计最多 {MAX_REFERENCES} 张，请减少参考图')
    return result


def edit_text(change, edits, *, compose=False):
    paths, lines, replacements = [], [], {}
    for row in edits:
        indices = []
        for path in row['paths']:
            item = {'path': path, 'selection': row['source_selection']} if row.get('source_selection') else path
            if item not in paths:
                paths.append(item)
            indices.append(str(paths.index(item) + (1 if compose else 2)))
        sample = '、'.join('图' + index for index in indices)
        for alias in row['aliases']:
            if alias:
                replacements[alias] = sample + '所示石材'
        region = row['region']
        tag = ''
        if region:
            keys = ('x', 'y') if region['type'] == 'point' else ('x1', 'y1', 'x2', 'y2')
            tag = '<' + region['type'] + '>' + ' '.join(str(region[k]) for k in keys) + '</' + region['type'] + '>'
        use = row.get('use', 'material')
        part = (row.get('take') or ('选出的部分' if row.get('source_selection') else USES[use])).rstrip('。；，.!?;:,！？')
        if compose:
            lines.append(f"从{sample}取用{part}，用于新方案的{row['target'] or '相应部分'}。")
        elif row['target'] or tag:
            lines.append(f"图1{tag}的{row['target'] or '指定部位'}使用{sample}的{part}。")
        else:
            lines.append(f'{sample}提供本次修改的{part}，应用部位以修改说明为准。')
        if use == 'material':
            lines.append(f'{sample}只提供颜色与纹理，' + ('用于新设计的指定部位，' if compose else '保留修改部位原有造型和细节，') + '不取用样图背景、地面、支架、标签与其他物件。')
        if row.get('source_selection'):
            lines.append(f'{sample}的纯色背景仅用于展示所选内容，不作为设计元素。')
    def neutral(text):
        for alias in sorted(replacements, key=len, reverse=True):
            text = re.sub(re.escape(alias) + ('(?!画)' if alias == '敦煌壁' else ''), lambda _: replacements[alias], text)
        return text
    instructions = neutral(change) + '\n' + '\n'.join(neutral(line) for line in lines)
    if compose:
        instructions += '\n按说明组合成新的完整设计，各参考图地位相同，不默认第一张是底图。只取指定内容，统一比例、透视、光线与构图，不拼成对照图或分栏图。'
    else:
        instructions += '\n图1是唯一的修改底图。保留底图的构图、视角和前景遮挡。未要求修改的物体、面部表情、五官、姿态、手势、小像和装饰细节须与图1一致。修改范围以说明为准；说明未写整面或全部时，只改点名的部位，保留相邻的边框、线脚和板块。'
    return instructions, paths


def public_edits(edits):
    return [{k: row[k] for k in ('source', 'id', 'name', 'source_sha256', 'source_file_sha256', 'region', 'references', 'use', 'take', 'target', 'fidelity', 'source_selection', 'target_selection')
             if k in row and (not k.endswith('_selection') or row[k] is not None)} for row in edits]


def concept_label(task, concept_id):
    interior = next((row for row in task['concepts'] if row['id'] == concept_id and row.get('interior_photographic')), None)
    if interior:
        return interior['name']
    designs = [v['id'] for v in task['concepts'] if v.get('reference_design')]
    if concept_id in designs:
        return '参考方案' + str(designs.index(concept_id) + 1)
    compositions = [v['id'] for v in task['concepts'] if v.get('composition') and not v.get('reference_design')]
    if concept_id in compositions:
        return '组合' + str(compositions.index(concept_id) + 1)
    planned = [v['id'] for v in task['concepts'] if not v.get('source_image_id') and not v.get('composition')]
    if concept_id in planned:
        return '方案' + str(planned.index(concept_id) + 1)
    uploaded = [v['id'] for v in task['concepts'] if v.get('source_image_id')]
    return '上传图' + str(uploaded.index(concept_id) + 1) if concept_id in uploaded else '图片'


def filename(task, image):
    def safe(text, length):
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(text)).strip(' .')[:length]
    title = safe(task['brief']['title'], 40) or '设计图'
    guide_id = (image.get('floorplan_input') or {}).get('structure_id')
    material = safe('、'.join(dict.fromkeys(v['name'] for v in image.get('edits', []) if not guide_id or v.get('id') != guide_id)), 30)
    label = safe(concept_label(task, image['concept_id']), 80) or '图片'
    return f"{title}-{label}-版本{image['version']}" + (f'-{material}' if material else '') + f"-{image['id'][:8]}{Path(image['file']).suffix.lower()}"


def export_subfolder(task):
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(task['brief']['title'])).strip(' .')[:40] or '设计图'
    name = task.get('export_folder') or (title + '-' + ident(task['id'])[:8])
    if (not isinstance(name, str) or not 1 <= len(name) <= 120 or name in ('.', '..')
            or re.search(r'[/\\\x00-\x1f]', name) or not name.endswith(ident(task['id'])[:8])):
        raise ValueError('设计导出目录无效')
    return name


def save_export(task, image, output, root, archived=None):
    root = Path(root)
    if not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('导出文件夹无效')
    source = (Path(archived) if archived else Path(output) / image['file']).resolve(strict=True)
    expected = Path(output) / ('设计成果' if archived else '产品设计') / ident(task['id'])
    if not source.is_relative_to(expected.resolve()) or source.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        raise ValueError('图片路径无效')
    folder = root / export_subfolder(task)
    if folder.is_symlink():
        raise ValueError('导出文件夹不能是链接')
    binary = source.read_bytes()
    digest = hashlib.sha256(binary).hexdigest()
    if image.get('content_sha256') and image['content_sha256'] != digest:
        raise ValueError('这张图片与登记版本不一致，未导出。请先核对原文件')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    for record in image.get('exports', []):
        previous = root / record.get('file', '')
        if (previous.is_file() and not previous.is_symlink() and previous.resolve().is_relative_to(folder.resolve())
                and record.get('sha256') == digest and hashlib.sha256(previous.read_bytes()).hexdigest() == digest):
            return record, previous
    target = folder / filename(task, image)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError('同名导出文件内容不同，未覆盖。请先在访达中核对')
    else:
        created = False
        try:
            with target.open('xb') as stream:
                created = True
                target.chmod(0o600)
                stream.write(binary)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise
    return dict(file=target.relative_to(root).as_posix(), sha256=digest, created=stamp()), target


def reveal_export(task, image, root):
    root = Path(root)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError('导出文件夹无效')
    root = root.resolve(strict=True)
    if not image.get('exports'):
        raise ValueError('请先保存这张图片')
    path = root / image['exports'][-1]['file']
    if path.is_symlink() or path.parent.is_symlink() or not path.resolve(strict=True).is_relative_to(root / export_subfolder(task)):
        raise ValueError('导出图片位置无效')
    if hashlib.sha256(path.read_bytes()).hexdigest() != image['exports'][-1]['sha256']:
        raise ValueError('导出文件已被更改，请先核对')
    return path
