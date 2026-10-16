"""Immutable, portable design evidence. Never include model prompts or private paths."""
import copy
import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from collections import deque
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from .studio_store import FIELDS, archive_folder, ident

MAX_BUNDLE_BYTES = 256 * 1024 * 1024
EDIT_FIELDS = ('source', 'id', 'name', 'use', 'take', 'target', 'fidelity', 'region',
               'source_selection', 'target_selection', 'source_sha256', 'source_file_sha256')
REFERENCE_FIELDS = ('id', 'source_id', 'title', 'role', 'rights', 'credit', 'source_url',
                    'license_url', 'verified_sha256', 'content_sha256')


def immutable_file(target, binary):
    target = Path(target)
    if target.is_symlink() or (target.exists() and (not target.is_file() or target.read_bytes() != binary)):
        raise ValueError('已有存档文件校验不一致，未覆盖')
    if target.exists():
        return
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            temporary.chmod(0o600)
            stream.write(binary); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or target.read_bytes() != binary:
                raise ValueError('存档文件被其他操作修改，未覆盖')
    finally:
        temporary.unlink(missing_ok=True)


def own_file(task, output, row):
    root = Path(output).resolve(strict=True) / '产品设计' / ident(task['id'])
    if root.is_symlink() or root.parent.is_symlink() or not root.resolve(strict=True).is_relative_to(Path(output).resolve(strict=True)):
        raise ValueError('存档依据图片目录无效')
    relative = Path(row['file'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('存档依据图片路径无效')
    lexical = Path(output) / relative
    if lexical.is_symlink():
        raise ValueError('存档依据图片不能是符号链接')
    path = lexical.resolve(strict=True)
    if not path.is_relative_to(root.resolve(strict=True)) or not path.is_file():
        raise ValueError('存档依据图片不属于当前设计')
    if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
        raise ValueError('存档依据图片格式无效')
    return path


def basis_snapshot(private_root, image_id):
    if private_root is None:
        return {'state': 'not_recorded'}
    root = Path(private_root)
    file = root / (ident(image_id) + '.basis.json')
    if not file.exists():
        return {'state': 'not_recorded'}
    stat = file.stat()
    if (root.is_symlink() or file.is_symlink() or not file.is_file()
            or stat.st_size > 1_000_000 or stat.st_mode & 0o077):
        raise ValueError('存档生成依据文件无效')
    binary = file.read_bytes()
    value = json.loads(binary)
    if not isinstance(value, dict) or not isinstance(value.get('brief', {}), dict):
        raise ValueError('存档生成依据格式无效')
    snapshot = {k: copy.deepcopy(value[k]) for k in ('source', 'brief_revision', 'parent_image_id',
                'historical_edits_unknown') if k in value}
    snapshot['brief'] = {k: value['brief'][k] for k in FIELDS if k in value.get('brief', {})}
    snapshot['concept'] = {k: value.get('concept', {})[k] for k in ('form', 'materials') if k in value.get('concept', {})}
    # The parent ID keeps the version chain. Do not copy free-form generation instructions.
    return {'state': 'recorded', 'source_file_sha256': hashlib.sha256(binary).hexdigest(), 'snapshot': snapshot}


def evidence_bundle(task, images, catalog, output, private_root, dimension_artifacts, primary_artifacts):
    files, total = {}, 0

    def add(name, binary):
        nonlocal total
        if name in files and files[name] != binary:
            raise ValueError('存档包内文件冲突')
        if name not in files:
            total += len(binary)
            if total > MAX_BUNDLE_BYTES:
                raise ValueError('存档依据超过256MiB，请减少本次方向的图片版本')
            files[name] = binary
        return {'file': name, 'sha256': hashlib.sha256(binary).hexdigest(), 'bytes': len(binary)}

    def picture(row, *, required=False, expected=''):
        try:
            path = own_file(task, output, row)
        except FileNotFoundError:
            if required:
                raise ValueError('成果图片已缺失，未存档') from None
            return {'state': 'missing'}
        if path.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError('单个存档依据文件超过256MiB，未打包')
        binary = path.read_bytes()
        digest = hashlib.sha256(binary).hexdigest()
        if expected and digest != expected:
            raise ValueError('参考图片与该版本记录不一致，未存档')
        return {'state': 'ok', **add('inputs/' + digest + path.suffix.lower(), binary)}

    def catalog_picture(ref):
        entry = catalog.resolve_snapshot(ref)
        snapshot = {k: copy.deepcopy(ref[k]) for k in REFERENCE_FIELDS if k in ref}
        if not entry or entry.get('kind') != 'image':
            return {**snapshot, 'state': 'unavailable'}
        source_path = Path(entry['source'])
        try:
            if source_path.stat().st_size > MAX_BUNDLE_BYTES:
                raise ValueError('单个参考素材超过256MiB，未打包')
            binary = source_path.read_bytes()
        except OSError:
            return {**snapshot, 'state': 'unavailable'}
        digest = hashlib.sha256(binary).hexdigest()
        if ref.get('content_sha256') and ref['content_sha256'] != digest:
            return {**snapshot, 'state': 'unavailable'}
        try:
            with Image.open(io.BytesIO(binary)) as original:
                oriented = ImageOps.exif_transpose(original)
                has_alpha = 'A' in oriented.getbands() or 'transparency' in oriented.info
                if original.info.get('icc_profile'):
                    alpha = oriented.convert('RGBA').getchannel('A') if has_alpha else None
                    profile = ImageCms.ImageCmsProfile(io.BytesIO(original.info['icc_profile']))
                    mode = {'RGB': 'RGB', 'GRAY': 'L', 'CMYK': 'CMYK'}.get(str(profile.profile.xcolor_space).strip())
                    if mode is None:
                        raise ValueError('参考图片色彩配置不支持')
                    oriented = ImageCms.profileToProfile(oriented.convert(mode), profile,
                        ImageCms.createProfile('sRGB'), outputMode='RGB')
                    if alpha is not None:
                        oriented.putalpha(alpha)
                else:
                    oriented = oriented.convert('RGBA' if has_alpha else 'RGB')
                normalized = Image.frombytes(oriented.mode, oriented.size, oriented.tobytes())
                stream = io.BytesIO(); normalized.save(stream, format='PNG')
        except (UnidentifiedImageError, OSError, ValueError, ImageCms.PyCMSError, Image.DecompressionBombError):
            return {**snapshot, 'state': 'invalid_image', 'source_file_sha256': digest}
        clean_binary = stream.getvalue()
        return {**snapshot, 'state': 'ok', 'source_file_sha256': digest, 'metadata_removed': True,
                **add('inputs/' + hashlib.sha256(clean_binary).hexdigest() + '.png', clean_binary)}

    by_id = {row['id']: row for row in task['images']}
    assets = {row['id']: row for row in task.get('edit_assets', [])}
    selected_ids = {row['id'] for row in images}
    pending, seen, versions = deque(images), set(), []
    while pending:
        image = pending.popleft()
        image_id = ident(image['id'])
        if image_id in seen:
            continue
        seen.add(image_id)
        if image_id in primary_artifacts:
            suffix, binary = primary_artifacts[image_id]
            artifact = add('images/' + image_id + suffix, binary)
        else:
            try:
                path = own_file(task, output, image)
                if path.stat().st_size > MAX_BUNDLE_BYTES:
                    raise ValueError('单个历史版本超过256MiB，未打包')
                artifact = add('images/' + image_id + path.suffix.lower(), path.read_bytes())
            except FileNotFoundError:
                artifact = {'state': 'missing'}
        expected = image.get('content_sha256')
        if expected and artifact.get('state') != 'missing' and expected != artifact['sha256']:
            raise ValueError('图片版本与登记时内容不一致，未存档')
        row = {k: copy.deepcopy(image[k]) for k in ('id', 'version', 'parent_id', 'width', 'height',
               'kind', 'model', 'processing', 'reference_count', 'floorplan_snapshot', 'floorplan_input') if k in image}
        row.update(artifact=artifact, role='selected_direction' if image_id in selected_ids else 'supporting_version',
                   registered_content_verified=bool(expected) and artifact.get('state') != 'missing',
                   generation_basis=basis_snapshot(private_root, image_id), references=[])
        for edit in image.get('edits', []):
            record = {k: copy.deepcopy(edit[k]) for k in EDIT_FIELDS if k in edit}
            record['artifacts'] = []
            source = edit.get('source')
            if source in ('upload', 'image'):
                target = (assets if source == 'upload' else by_id).get(edit.get('id'))
                expected = edit.get('source_file_sha256') or (edit.get('source_sha256') if edit.get('source_selection') else '')
                record['artifacts'].append(picture(target, expected=expected) if target else {'state': 'missing'})
                record['generation_content_verified'] = bool(expected) and record['artifacts'][0]['state'] == 'ok'
                if source == 'image' and target:
                    pending.append(target)
            for ref in edit.get('references', []):
                record['artifacts'].append(catalog_picture(ref))
            row['references'].append(record)
        concept = next((c for c in task['concepts'] if c['id'] == image['concept_id']), {})
        row['concept_reference_snapshot'] = [catalog_picture(ref) for ref in concept.get('reference_snapshot', [])]
        parent = by_id.get(image.get('parent_id'))
        if parent:
            pending.append(parent)
        floorplan = image.get('floorplan_snapshot')
        if floorplan:
            row['floorplan_artifacts'] = []
            for asset_id in [floorplan.get('source_id'), *floorplan.get('guide_ids', [])]:
                asset = assets.get(asset_id)
                expected = floorplan.get('source_sha256', '') if asset_id == floorplan.get('source_id') else (asset or {}).get('source_sha256', '')
                row['floorplan_artifacts'].append({'id': asset_id,
                    **(picture(asset, expected=expected) if asset else {'state': 'missing'})})
        interior = image.get('floorplan_input')
        if interior and interior.get('mode') == 'floorplan_photographic':
            asset = assets.get(interior['source_id'])
            row['interior_plan_artifact'] = {'id': interior['source_id'],
                **(picture(asset, expected=interior['source_sha256']) if asset else {'state': 'missing'})}
            if interior.get('reference_kind') == 'derived_structure':
                guide = assets.get(interior.get('structure_id'))
                row['interior_view_artifact'] = {'id': interior['structure_id'],
                    **(picture(guide, expected=interior['structure_sha256']) if guide else {'state': 'missing'})}
        versions.append(row)
    dimension_records = []
    for image, binary, receipt, document_sha in dimension_artifacts:
        prefix = 'dimensions/' + ident(image['id']) + '-' + document_sha[:12]
        dimension_records.append({'image_id': image['id'], 'document_sha256': document_sha,
            'png': add(prefix + '.png', binary),
            'json': add(prefix + '.json', json.dumps(receipt, ensure_ascii=False, indent=2).encode())})
    concept = next(c for c in task['concepts'] if c['id'] == task['selected'])
    manifest = {'schema': 1, 'task_id': task['id'], 'selected_concept_id': task['selected'],
        'review': copy.deepcopy(task['review']), 'current_brief_revision': task.get('brief_revision', 1),
        'current_brief': {k: task['brief'].get(k, '') for k in FIELDS},
        'selected_concept': {k: copy.deepcopy(concept[k]) for k in ('id', 'name', 'concept', 'form', 'materials',
                            'rationale', 'risks', 'brief_revision', 'reference_basis_unknown') if k in concept},
        'versions': versions, 'dimension_records': dimension_records,
        'limitation': '缺失的历史依据未用当前要求补写。参考图片未记录生成时内容校验值时，仅保存归档时快照。尺寸标注不是透视测量、施工图或加工文件。'}
    add('archive-manifest.json', json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode())
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, files[name])
    binary = stream.getvalue()
    inputs = [ref for version in versions for record in version['references'] for ref in record['artifacts']]
    inputs += [ref for version in versions for ref in version.get('floorplan_artifacts', [])]
    inputs += [version['interior_plan_artifact'] for version in versions if version.get('interior_plan_artifact')]
    inputs += [version['interior_view_artifact'] for version in versions if version.get('interior_view_artifact')]
    inputs += [ref for version in versions for ref in version.get('concept_reference_snapshot', [])]
    inputs += [v['artifact'] for v in versions if v['artifact'].get('state') == 'missing']
    return binary, {'schema': 1, 'sha256': hashlib.sha256(binary).hexdigest(), 'bytes': len(binary),
                    'file_count': len(files), 'version_count': len(versions),
                    'missing_input_count': sum(ref.get('state') != 'ok' for ref in inputs),
                    'unknown_basis_count': sum(v['generation_basis']['state'] != 'recorded' for v in versions),
                    'unverified_version_count': sum(not v['registered_content_verified'] for v in versions),
                    'unverified_input_count': sum(ref.get('source') in ('upload', 'image') and not ref.get('generation_content_verified')
                        for version in versions for ref in version['references'])}


def bundle_file(task, archive, archive_root):
    record = archive.get('evidence_bundle', {})
    digest = record.get('sha256')
    if record.get('schema') != 1 or not re.fullmatch(r'[a-f0-9]{64}', str(digest)):
        raise ValueError('这份历史存档没有打包版本依据')
    folder = archive_folder(task, archive_root)
    path = folder / ('evidence-' + digest + '.zip')
    if path.is_symlink() or not path.is_file() or path.resolve().parent != folder.resolve():
        raise ValueError('存档依据包缺失或路径无效')
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            checksum.update(block)
    if checksum.hexdigest() != digest:
        raise ValueError('存档依据包校验不一致，未使用当前文件替代')
    return path
