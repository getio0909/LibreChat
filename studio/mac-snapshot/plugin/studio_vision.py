import base64
import copy
import hashlib
import io
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps

from .studio_images import NoRedirect
from .studio_store import FIELDS, clean, ident
from .studio_floorplan import validate_basis
from .studio_interior import interior_plan


BASE_URL = 'https://api.deepseek.com'
MODEL = 'deepseek-flash'
LABELS = {'subject': '主体与数量', 'appearance': '材质与外观', 'constraints': '保留与避免项',
          'structure': '结构与细节', 'edit': '改动与保持区域'}
VERDICTS = ('符合', '疑似不符', '无法判断')
LIMITATION = '仅核对图中可见内容，可能漏检或误判；实际尺寸、材料真伪、承重、加工可行性及文化正确性仍需人工核验。'


class CheckUncertain(RuntimeError):
    pass


def read_config(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('视觉核对配置权限必须为600')
    config = json.loads(path.read_text())
    if config.get('enabled', True) is not True:
        raise ValueError('视觉辅助核对暂未启用，等待模型质量验收')
    if (config.get('provider') != 'deepseek' or config.get('base_url') != BASE_URL
            or config.get('model') != MODEL):
        raise ValueError('视觉核对服务配置无效')
    key = Path(config.get('key_file', ''))
    if not key.is_absolute() or not key.is_file() or key.is_symlink() or key.stat().st_mode & 0o077:
        raise ValueError('请检查视觉核对密钥文件及600权限')
    if (not isinstance(config.get('rules'), str) or not config['rules'].strip()
            or config.get('reasoning_effort') not in ('low', 'high', 'max')
            or not isinstance(config.get('timeout_seconds'), int) or not 30 <= config['timeout_seconds'] <= 600
            or not isinstance(config.get('max_tokens'), int) or not 1024 <= config['max_tokens'] <= 32000):
        raise ValueError('视觉核对参数无效')
    return config


def vision_status(path):
    try:
        config = read_config(path)
        return {'vision_ready': True, 'vision_model': config['model'], 'vision_label': 'DeepSeek 视觉辅助核对'}
    except (OSError, ValueError, TypeError):
        return {'vision_ready': False, 'vision_model': '', 'vision_label': '视觉核对未就绪'}


def image_row(task, image_id):
    row = next((v for v in task['images'] if v['id'] == ident(image_id)), None)
    if row is None or not any(concept['id'] == row['concept_id'] for concept in task['concepts']):
        raise ValueError('请选择本任务中的图像版本')
    return row


def image_path(task, row, output):
    expected = (Path(output) / '产品设计' / ident(task['id'])).resolve()
    path = (Path(output) / row['file']).resolve(strict=True)
    if not path.is_relative_to(expected):
        raise ValueError('核对图像不在本任务目录')
    return path


def basis_file(private_root, image_id):
    return Path(private_root) / (ident(image_id) + '.basis.json')


def criteria(task, row, private_root):
    file = basis_file(private_root, row['id'])
    if file.exists():
        if file.is_symlink() or file.stat().st_mode & 0o077:
            raise ValueError('图像核对依据权限无效')
        value = json.loads(file.read_text())
    else:
        concept = next(c for c in task['concepts'] if c['id'] == row['concept_id'])
        value = {'source': '当前任务要求；历史改图目标未记录，不代表生成时要求',
            'brief_revision': task.get('brief_revision', 1),
            'brief': {k: task['brief'].get(k, '') for k in ('goal', 'materials', 'setting', 'must_have', 'must_avoid')},
            'concept': {k: concept.get(k, '') for k in ('form', 'materials')}, 'changes': [],
            'historical_edits_unknown': True}
    if row.get('floorplan_snapshot'):
        value['floorplan_snapshot'] = copy.deepcopy(row['floorplan_snapshot'])
    if row.get('floorplan_input', {}).get('mode') == 'floorplan_photographic':
        value['floorplan_input'] = copy.deepcopy(row['floorplan_input'])
    return value


def generation_requirements(task, concept_id, action):
    concept = next(c for c in task['concepts'] if c['id'] == concept_id)
    current = task.get('brief_revision', 1)
    revision = concept.get('brief_revision')
    if action in ('refine', 'compose') or concept.get('source_image_id'):
        revision, brief = current, task['brief']
    elif concept.get('reference_design') and concept.get('design_brief'):
        brief = concept['design_brief']
    else:
        brief = task['brief'] if revision == current else None
        if type(revision) is int and 1 <= revision < current:
            history = task.get('brief_history', [])
            brief = history[revision - 1]['brief'] if len(history) >= revision else None
    return {'brief_revision': revision,
            'brief': {key: copy.deepcopy(brief.get(key, '')) for key in FIELDS if key != 'title'} if brief is not None else None}


def generation_basis(task, concept_id, parent_id, action, change, private_root, *, floorplan=None, requirements=None):
    concept = next(c for c in task['concepts'] if c['id'] == concept_id)
    parent = next((v for v in task['images'] if v['id'] == parent_id), None)
    if requirements is not None and action != 'export':
        brief = requirements['brief']
        value = {'source': '本次生成实际提交的设计要求' if brief is not None else '历史方向描述；原任务约束无法确认',
                 'brief_revision': requirements['brief_revision'], 'brief': copy.deepcopy(brief) if brief is not None else {},
                 'concept': {key: concept.get(key, '') for key in ('form', 'materials')}
                    if action == 'generate' and not parent else {},
                 'changes': [], 'historical_edits_unknown': brief is None}
        if parent:
            value['parent_image_id'] = parent['id']
    elif parent:
        value = copy.deepcopy(criteria(task, parent, private_root))
    else:
        revision = concept.get('brief_revision')
        current = task.get('brief_revision', 1)
        brief = task['brief'] if revision == current else None
        if isinstance(revision, int) and 1 <= revision < current:
            history = task.get('brief_history', [])
            brief = history[revision - 1]['brief'] if len(history) >= revision else None
        if concept.get('reference_design') and concept.get('design_brief'):
            brief = concept['design_brief']
        value = {'source': '图像生成时的设计约束' if brief else '历史方向描述；原任务约束无法确认',
                 'brief_revision': revision, 'brief': {k: brief.get(k, '') for k in
                    ('goal', 'materials', 'setting', 'must_have', 'must_avoid')} if brief else {},
                 'concept': {k: concept.get(k, '') for k in ('form', 'materials')},
                 'changes': [], 'historical_edits_unknown': brief is None}
    if action in ('refine', 'compose', 'design'):
        value['changes'].append(clean(change))
    if floorplan:
        value['floorplan_snapshot'] = copy.deepcopy(floorplan)
    return value


def image_content(path):
    binary = Path(path).read_bytes()
    with Image.open(io.BytesIO(binary)) as source:
        if source.format not in ('JPEG', 'PNG', 'WEBP'):
            raise ValueError('核对图像格式不支持')
        im = ImageOps.exif_transpose(source).convert('RGB')
        if max(im.size) > 8192:
            im.thumbnail((8192, 8192), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        im.save(buffer, format='JPEG', quality=95)
    return ('data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii'),
            hashlib.sha256(binary).hexdigest())


def validate_result(raw, private_values):
    if not isinstance(raw, dict) or set(raw) != {'checks'} or not isinstance(raw['checks'], list):
        raise ValueError('视觉核对结果结构不完整')
    rows = raw['checks']
    if len(rows) != len(LABELS) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('视觉核对项目不完整')
    if {r.get('id') for r in rows if isinstance(r.get('id'), str)} != set(LABELS):
        raise ValueError('视觉核对项目编号无效')
    result = []
    for row in rows:
        if set(row) != {'id', 'verdict', 'observation'} or row['verdict'] not in VERDICTS:
            raise ValueError('视觉核对判断格式无效')
        observation = clean(row['observation'], 1600)
        if not observation or any(v and v in observation for v in private_values):
            raise ValueError('视觉核对结果包含无效内容')
        if re.search(r'(?:/Users/|/home/|Bearer\s|sk-[A-Za-z0-9]{12})', observation):
            raise ValueError('视觉核对结果包含非设计信息')
        result.append({'id': row['id'], 'label': LABELS[row['id']],
                       'verdict': row['verdict'], 'observation': observation})
    return sorted(result, key=lambda row: list(LABELS).index(row['id']))


def inspect(config_path, task, image_id, output, private_root, override=''):
    config = read_config(config_path)
    row = image_row(task, image_id)
    basis = criteria(task, row, private_root)
    override = clean(override)
    if override:
        basis = {'source': '本次指定的核对标准（替代原任务约束）', 'standard': override}
    target, target_hash = image_content(image_path(task, row, output))
    content = [{'type': 'text', 'text': json.dumps({'criteria': basis, 'image_role': 'target'}, ensure_ascii=False)},
               {'type': 'image_url', 'image_url': {'url': target, 'detail': 'original'}}]
    parent_hash = None
    if row.get('parent_id'):
        parent = image_row(task, row['parent_id'])
        base, parent_hash = image_content(image_path(task, parent, output))
        content += [{'type': 'text', 'text': json.dumps({'image_role': 'previous_version'})},
                    {'type': 'image_url', 'image_url': {'url': base, 'detail': 'original'}}]
    floorplan = row.get('floorplan_snapshot')
    interior = row.get('floorplan_input')
    basis_images = []
    if floorplan:
        validate_basis(floorplan, task, output)
        assets = {asset['id']: asset for asset in task.get('edit_assets', [])}
        for asset_id in [floorplan['source_id'], *floorplan['guide_ids']]:
            asset = assets[asset_id]
            reference, digest = image_content(Path(output) / asset['file'])
            expected = floorplan['source_sha256'] if asset_id == floorplan['source_id'] else asset['source_sha256']
            if digest != expected:
                raise ValueError('核对底稿已变化，请重新确认')
            kind = 'original_plan' if asset_id == floorplan['source_id'] else asset['guide_kind']
            basis_images.append({'id': asset_id, 'kind': kind, 'sha256': digest})
            content += [{'type': 'text', 'text': json.dumps({'image_role': 'floorplan_' + kind,
                           'purpose': '本版结构和相机的核对依据，不是最终材质；不可据像素判断真实尺寸'}, ensure_ascii=False)},
                        {'type': 'image_url', 'image_url': {'url': reference, 'detail': 'original'}}]
    if interior and interior.get('mode') == 'floorplan_photographic':
        path = interior_plan(task, interior, output)
        reference, digest = image_content(path)
        if digest != interior['source_sha256']:
            raise ValueError('室内平面图已变化，未提交核对')
        basis_images.append({'id': interior['source_id'], 'kind': 'original_plan', 'sha256': digest})
        content += [{'type': 'text', 'text': json.dumps({'image_role': 'interior_original_plan',
                       'purpose': '对照此原始平面图核对墙体、门窗、家具数量与位置；无法判断就标记不确定，不从效果图宣称实际尺寸'}, ensure_ascii=False)},
                    {'type': 'image_url', 'image_url': {'url': reference, 'detail': 'original'}}]
    key = Path(config['key_file']).read_text().strip()
    if not key or any(c.isspace() for c in key):
        raise ValueError('视觉核对密钥格式无效')
    body = {'model': MODEL, 'messages': [{'role': 'system', 'content': config['rules']},
                                       {'role': 'user', 'content': content}],
            'thinking': {'type': 'enabled'}, 'reasoning_effort': config['reasoning_effort'],
            'max_tokens': config['max_tokens'], 'response_format': {'type': 'json_object'}, 'stream': False}
    encoded = json.dumps(body, ensure_ascii=False).encode()
    if len(encoded) > 48 * 1024 * 1024:
        raise ValueError('核对图像超过服务请求大小限制')
    request = urllib.request.Request(BASE_URL + '/chat/completions', data=encoded,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    started = time.monotonic()
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=config['timeout_seconds']) as response:
            binary = response.read(2 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code >= 500:
            raise CheckUncertain('视觉核对服务异常，结果尚未收到；未自动重试，图片不受影响') from None
        messages = {400: '视觉核对参数未被接受', 401: 'DeepSeek 密钥无效', 402: 'DeepSeek 余额不足',
                    403: '视觉模型访问被拒绝', 429: 'DeepSeek 请求限流'}
        raise RuntimeError(messages.get(code, '视觉核对请求被拒绝') + '；未自动重试，图片不受影响') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CheckUncertain('视觉核对连接中断，结果尚未收到；未自动重试，图片不受影响') from None
    try:
        if len(binary) > 2 * 1024 * 1024:
            raise ValueError()
        response = json.loads(binary)
        choice = response['choices'][0]
        if choice['finish_reason'] != 'stop':
            raise ValueError()
        private_values = [key, config['rules'], *[v for v in config['rules'].split('\n') if len(v) > 30]]
        checks = validate_result(json.loads(choice['message']['content']), private_values)
        reported_model = response['model']
        if not isinstance(reported_model, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', reported_model):
            raise ValueError()
        if not isinstance(response.get('usage', {}), dict):
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise CheckUncertain('未收到完整、可验证的核对结果；未自动重试或标记通过') from None
    if hashlib.sha256(image_path(task, row, output).read_bytes()).hexdigest() != target_hash:
        raise CheckUncertain('核对期间图片发生变化，本次结果未登记；请重新打开当前图片')
    if floorplan:
        try:
            validate_basis(floorplan, task, output)
        except (OSError, ValueError) as error:
            raise CheckUncertain('核对期间结构依据发生变化，本次结果未登记；请重新打开当前设计') from error
    if interior and interior.get('mode') == 'floorplan_photographic':
        try:
            interior_plan(task, interior, output)
        except (ValueError, OSError) as error:
            raise CheckUncertain('核对期间原始平面图发生变化，本次结果未登记；请重新打开当前设计') from error
    return {'image_id': image_id, 'image_sha256': target_hash, 'parent_id': row.get('parent_id'),
            'parent_sha256': parent_hash, 'provider': 'deepseek', 'model': MODEL, 'reported_model': reported_model,
            'basis': basis['source'], 'criteria_sha256': hashlib.sha256(json.dumps(basis, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'checks': checks, 'limitation': LIMITATION, 'basis_images': basis_images, 'seconds': round(time.monotonic() - started, 1),
            'usage': {k: v for k, v in response.get('usage', {}).items()
                      if k in ('prompt_tokens', 'completion_tokens', 'total_tokens') and isinstance(v, int)}}
