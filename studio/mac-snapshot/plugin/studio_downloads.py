import hashlib
import io
import re
import zipfile
from pathlib import Path


def _name_part(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', str(value)).strip(' .')
    return value[:64].rstrip(' .') or '室内效果图'


def interior_set_zip(task, batch, output):
    task_id = task['id']
    if not re.fullmatch(r'[a-f0-9]{32}', task_id):
        raise ValueError('设计编号无效')
    if not isinstance(batch, str) or (batch != 'legacy' and not re.fullmatch(r'[a-f0-9]{32}', batch)):
        raise ValueError('室内生成轮次无效')
    concepts = [row for row in task['concepts'] if row.get('interior_view')
                and (row.get('batch') or 'legacy') == batch]
    if not concepts:
        raise ValueError('找不到这套室内图，请刷新后重新打开')
    completed = [(concept, max((row for row in task['images'] if row['concept_id'] == concept['id']),
                              key=lambda row: row['version'], default=None)) for concept in concepts]
    completed = [(concept, image) for concept, image in completed if image is not None]
    if not completed:
        raise ValueError('这套图还没有可下载的照片')
    root = Path(output).resolve(strict=True)
    folder = (root / '产品设计' / task_id).resolve(strict=True)
    if not folder.is_relative_to(root):
        raise ValueError('设计图片目录无效')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_STORED) as archive:
        for index, (concept, image) in enumerate(completed, 1):
            source = (root / image['file']).resolve(strict=True)
            if not source.is_relative_to(folder) or source.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
                raise ValueError('图片不属于当前设计')
            binary = source.read_bytes()
            if image.get('content_sha256') and hashlib.sha256(binary).hexdigest() != image['content_sha256']:
                raise ValueError('图片与登记版本不一致，请先核对原文件')
            label = _name_part(concept.get('name') or concept['interior_view'].get('space_label') or '室内视角')
            name = f'{index:02d}_{label}_v{image["version"]}{source.suffix.lower()}'
            archive.writestr(name, binary)
    title = _name_part(task.get('brief', {}).get('title') or '室内效果图')
    return {'body': buffer.getvalue(), 'filename': f'{title}-{task_id[:8]}-{batch[:8]}.zip',
            'count': len(completed), 'total': len(concepts)}
