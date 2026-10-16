import asyncio
import hashlib
import io
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

from aiohttp import ClientSession, ClientTimeout, web
import numpy as np
from PIL import Image, ImageChops, ImageOps
import torch
import comfy.model_management as comfy_model_management

from .studio_store import (CATEGORIES, CHECKS, DECISIONS, FLOORPLAN_CHECKS, ROLES, MAX_UPLOAD_BYTES, MAX_UPLOAD_PIXELS, Catalog, Store, archive_file,
                           archive_images, archive_note, archive_location, archived_dimensions, archived_dimension_file,
                           clean, ident, preserve_references, stamp,
                           reset_review, review_for_image, save_review, validate_plan, write_private)
from .studio_images import (SIZES, MAX_REFERENCES, GenerationUncertain, check_reference_images, generate as cloud_generate, image_status,
                            read_config, reference_data, reference_entries)
from .studio_vision import (CheckUncertain, basis_file, generation_basis, generation_requirements, image_row,
                            inspect as inspect_image, read_config as vision_config, vision_status)
from .studio_edits import comparison_target, edit_text, public_edits, reference_budget, resolve_edits, reveal_export, save_export, source_image
from .studio_dimensions import annotation_values, base_digest, save_dimensions, export_dimensions, known_values
from .studio_archive import bundle_file
from .studio_downloads import interior_set_zip
from .studio_entry import require_local, studio_entry
from .studio_styles import library as style_library
from .studio_floorplan import EXAMPLE, prepare_guides, validate_basis, structure_anchor, constraints as floorplan_constraints
from .studio_interior import (build_interior_layout, interior_input, interior_plan, interior_layout_reference,
                              interior_reference_order, interior_reference_layout, interior_text,
                              interior_extra_text, interior_view_binding, layout_digest, room_first_views,
                              interior_resume_concepts, interior_unresolved_states,
                              validate_frozen_design_references, resolve_interior_object_scheme,
                              validate_interior_object_scheme, interior_object_digest, interior_object_text, LAYOUT_VERSION)
from .studio_layout import (photographic_text, validate_layout, resolve_finish_scheme, validate_finish_scheme,
                            view_object_anchors)
from .studio_regions import (Segmenter, composite_local, local_context, mask_for, object_placement, open_image, region_in_crop,
                             selected_reference, selection_proof, selection_value)

OPS = Path('/Users/ipd/workspace/comfyui-ops')
ROOT = OPS / 'studio-data'
VAULT = Path('/Users/ipd/obsidian/WIKI')
ARCHIVE = Path('/Users/ipd/Archive')
OUTPUT = Path('/Users/ipd/workspace/ComfyUI/output')
COMFY = 'http://127.0.0.1:8188'
store = None
catalog = Catalog(VAULT, ARCHIVE, OPS / 'studio-materials.json')
EXPORTS = Path('/Users/ipd/Pictures/庄严宝刹效果图')
workers = {}
planner_process = None
planner_lock = threading.Lock()
segmenter = Segmenter(OPS / 'studio-model' / 'sam2.1-hiera-tiny')


def planner_status():
    try:
        config = json.loads((OPS / 'studio-llm.json').read_text())
        provider = config['provider']
        if provider == 'deepseek':
            ready = config['base_url'] == 'https://api.deepseek.com' and Path(config['key_file']).is_file()
            return {'planner_ready': ready, 'provider': provider, 'model': config['model'], 'local_only': False}
        ready = provider == 'local' and all((OPS / 'studio-model/qwen35-9b' / name).is_file()
                for name in ('model-00001-of-00002.safetensors', 'model-00002-of-00002.safetensors', 'tokenizer.json'))
        return {'planner_ready': ready, 'provider': provider, 'model': 'Qwen3.5-9B-4bit', 'local_only': True}
    except (OSError, KeyError, ValueError):
        return {'planner_ready': False, 'provider': 'unconfigured', 'model': '', 'local_only': True}


def unconfirmed_cloud_jobs(task):
    history = task.get('cloud_job_history') or []
    damaged = not isinstance(history, list) or any(not isinstance(row, dict) for row in history)
    rows = [row for row in history if isinstance(row, dict) and not row.get('continued_at')] if isinstance(history, list) else []
    if damaged:
        rows.append({'unreadable': True})
    current = task.get('job') or {}
    if current.get('status') == 'uncertain' and not any(current.get('operation_id') and
            row.get('operation_id') == current['operation_id'] for row in rows):
        rows.append(current)
    return rows


def retain_cloud_job(task, action, operation_id, continue_confirmed=False):
    current = task.get('job') or {}
    history = task.get('cloud_job_history') or []
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        raise ValueError('生成记录未能读取，已有图片仍保留')
    if current.get('status') == 'uncertain':
        task['cloud_job_history'] = history
        previous = next((row for row in history if current.get('operation_id') and
                         row.get('operation_id') == current['operation_id']), None)
        if previous is None:
            fields = ('action', 'operation_id', 'started', 'finished', 'concept_ids', 'cloud_operation',
                      'cloud_pending', 'prompt_id', 'items', 'total', 'completed', 'phase')
            record = json.loads(json.dumps({key: current[key] for key in fields if key in current}))
            history.append({**record, 'status': 'uncertain', 'record_id': uuid.uuid4().hex,
                            'retained_at': stamp(), 'source_revision': task['revision']})
    if action != 'export' and continue_confirmed:
        for row in history:
            if not row.get('continued_at'):
                row.update(continued_at=stamp(), continued_action=action,
                           continued_operation_id=operation_id)


def export_folder():
    config = OPS / 'studio-exports.json'
    if not config.exists():
        return EXPORTS
    if config.is_symlink() or config.stat().st_size > 4096:
        raise ValueError('导出目录配置无效')
    value = json.loads(config.read_text(encoding='utf-8')).get('directory')
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('导出目录配置无效')
    return Path(value)


def remember_export(task_id, image_id, record, folder):
    for attempt in range(3):
        task = store.get(task_id)
        row = source_image(task, image_id)
        if any(v['file'] == record['file'] and v['sha256'] == record['sha256'] for v in row.get('exports', [])):
            return
        task['export_folder'] = folder
        row.setdefault('exports', []).append(record)
        try:
            store.put(task, task['revision'])
            return
        except ValueError as error:
            if '另一窗口更新' not in str(error):
                raise
    raise ValueError('图片已写入效果图文件夹，但保存标记尚未更新。请稍后再次保存以核对记录')


def stop_planner(job_id):
    with planner_lock:
        if planner_process and planner_process[0] == job_id and planner_process[1].poll() is None:
            try:
                os.killpg(planner_process[1].pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def thumbnail(ref_id):
    ref = catalog.get(ref_id)
    if ref['kind'] != 'image':
        raise ValueError('不是图像资料')
    with Image.open(ref['source']) as source:
        source.draft('RGB', (960, 960))
        im = ImageOps.exif_transpose(source).convert('RGB')
        im.thumbnail((480, 480))
        output = io.BytesIO()
        im.save(output, format='JPEG', quality=82)
    return output.getvalue()


def private_file(job_id, suffix='json'):
    return ROOT / 'private' / (ident(job_id) + '.' + suffix)


def payload(job_id):
    return json.loads(private_file(job_id).read_text())


class ZybStudioText:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'job_id': ('STRING', {})}}
    RETURN_TYPES = ('STRING',)
    FUNCTION = 'load'
    CATEGORY = '庄严宝刹/工作台内部'

    def load(self, job_id):
        return (payload(job_id)['text'],)


class ZybStudioImage:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'job_id': ('STRING', {})}}
    RETURN_TYPES = ('IMAGE',)
    FUNCTION = 'load'
    CATEGORY = '庄严宝刹/工作台内部'

    def load(self, job_id):
        path = Path(payload(job_id)['source']).resolve(strict=True)
        if not any(path.is_relative_to(root.resolve()) for root in (ARCHIVE, OUTPUT)):
            raise ValueError('图像来源不在允许范围')
        with Image.open(path) as im:
            if im.width * im.height > MAX_UPLOAD_PIXELS:
                raise ValueError('参考图过大，请先使用压缩副本')
            rgb = ImageOps.exif_transpose(im).convert('RGB')
            if rgb.width * rgb.height > 24_000_000:
                rgb.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            pixels = np.asarray(rgb, dtype=np.float32) / 255.0
        return (torch.from_numpy(pixels.copy()).unsqueeze(0),)


class ZybStudioPlanner:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'job_id': ('STRING', {})}}
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = 'run'
    CATEGORY = '庄严宝刹/工作台内部'

    def run(self, job_id):
        global planner_process
        request = payload(job_id)
        if planner_status()['local_only']:
            comfy_model_management.unload_all_models()
            comfy_model_management.soft_empty_cache()
        proc = subprocess.Popen(['/Users/ipd/.local/bin/py313', 'python', str(OPS / 'studio-planner.py')],
                                cwd=OPS, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        with planner_lock:
            planner_process = (job_id, proc)
        try:
            output, error = proc.communicate(json.dumps(request, ensure_ascii=False), timeout=570)
            if proc.returncode:
                errors = {'provider_http_401': 'DeepSeek 密钥无效', 'provider_http_402': 'DeepSeek 余额不足',
                          'provider_http_429': 'DeepSeek 请求限流', 'incomplete_response': '方案输出不完整',
                          'provider_unavailable': 'DeepSeek 暂时无法连接'}
                raise RuntimeError(errors.get(error.strip(), '方案模型执行失败') + '；未保存候选方向')
            parsed = json.loads(output)
            write_private(private_file(job_id, 'result.json'), json.dumps(parsed, ensure_ascii=False))
        except subprocess.TimeoutExpired:
            stop_planner(job_id)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
            raise RuntimeError('方案推理超时，可缩短任务后重试') from None
        finally:
            with planner_lock:
                planner_process = None
        return {'ui': {'text': ['方案计算完成']}}


def graph(job_id, task_id, mode='generate', quality='精细', size='1K', aspect='1:1'):
    g = {}
    def node(kind, **inputs):
        key = str(len(g) + 1)
        g[key] = {'class_type': kind, 'inputs': inputs}
        return [key, 0]
    width, height = {'1:1': (1024, 1024), '4:3': (1024, 768), '3:4': (768, 1024)}[aspect]
    image = node('ZybStudioImage', job_id=job_id) if mode in ('refine', 'export') else None
    if mode != 'export':
        base = quality == '精细'
        model = node('UNETLoader', unet_name='flux-2-klein-base-4b.safetensors' if base else 'flux-2-klein-4b.safetensors', weight_dtype='default')
        clip = node('CLIPLoader', clip_name='qwen_3_4b.safetensors', type='flux2', device='default')
        vae = node('VAELoader', vae_name='flux2-vae.safetensors')
        text = node('ZybStudioText', job_id=job_id)
        positive = node('CLIPTextEncode', clip=clip, text=text)
        negative = node('CLIPTextEncode', clip=clip, text='') if base else node('ConditioningZeroOut', conditioning=positive)
        if image:
            scaled = node('ImageScaleToTotalPixels', image=image, upscale_method='lanczos', megapixels=1.0, resolution_steps=16)
            shape = node('GetImageSize', image=scaled)
            width, height = shape, [shape[0], 1]
            latent = node('VAEEncode', pixels=scaled, vae=vae)
            positive = node('ReferenceLatent', conditioning=positive, latent=latent)
            negative = node('ReferenceLatent', conditioning=negative, latent=latent)
        guider = node('CFGGuider', model=model, positive=positive, negative=negative, cfg=4.0 if base else 1.0)
        noise = node('RandomNoise', noise_seed=int(job_id[:12], 16))
        sampler = node('KSamplerSelect', sampler_name='euler')
        sigmas = node('Flux2Scheduler', steps=28 if base else 4, width=width, height=height)
        empty = node('EmptyFlux2LatentImage', width=width, height=height, batch_size=1)
        samples = node('SamplerCustomAdvanced', noise=noise, guider=guider, sampler=sampler, sigmas=sigmas, latent_image=empty)
        image = node('VAEDecode', samples=samples, vae=vae)
    upscale = node('UpscaleModelLoader', model_name='RealESRGAN_x4plus.pth')
    larger = node('ImageUpscaleWithModel', upscale_model=upscale, image=image)
    chosen = node('ZybOutputResolution', image=image, upscaled=larger, **{'输出尺寸': size})
    sized = node('ImageScaleToMaxDimension', image=chosen, upscale_method='lanczos', largest_size=[chosen[0], 1])
    node('SaveImage', images=sized, filename_prefix=f'产品设计/{ident(task_id)}/{job_id}')
    return g


async def comfy(path, data=None):
    async with ClientSession(timeout=ClientTimeout(total=25)) as session:
        response = await session.request('POST' if data is not None else 'GET', COMFY + path, json=data)
        if response.status != 200:
            raise RuntimeError('图像引擎请求失败，请检查服务状态')
        return await response.json()


def update_job(task_id, **changes):
    task = store.get(task_id)
    task['job'].update(changes)
    store.put(task, task['revision'])


async def execute(task_id, workflow):
    job = store.get(task_id)['job']
    if job.get('cancel'):
        if (job.get('cloud_pending') and job.get('cloud_operation') == 'plan' and not job.get('prompt_id')
                and any(node.get('class_type') == 'ZybStudioPlanner' for node in workflow.values())):
            update_job(task_id, cloud_pending=False)
        raise ExecutionCancelled('任务已取消，已完成版本保留')
    accepted = await comfy('/prompt', {'prompt': workflow, 'client_id': 'zyb-studio'})
    prompt_id = accepted['prompt_id']
    update_job(task_id, prompt_id=prompt_id)
    deadline = time.monotonic() + 1800
    cancel_noted = False
    while True:
        task = store.get(task_id)
        if task['job'].get('cancel'):
            await comfy('/queue', {'delete': [prompt_id]})
            q = await comfy('/queue')
            running = any(row[1] == prompt_id for row in q['queue_running'])
            if running:
                for node in workflow.values():
                    if node['class_type'] == 'ZybStudioPlanner':
                        stop_planner(node['inputs']['job_id'])
                if not cancel_noted:
                    update_job(task_id, message='已停止后续步骤；等待当前图像结束并保留结果')
                    cancel_noted = True
            elif prompt_id not in await comfy('/history/' + prompt_id):
                raise ExecutionCancelled('任务已取消，已完成版本保留')
        history = await comfy('/history/' + prompt_id)
        item = history.get(prompt_id)
        if item:
            if item['status']['status_str'] != 'success':
                raise RuntimeError('本步骤未成功，已完成版本保留；可手动重试')
            return item
        if time.monotonic() > deadline:
            update_job(task_id, message='引擎处理超过30分钟，仍在等待本步骤；请勿重复提交')
            deadline = time.monotonic() + 1800
        await asyncio.sleep(3)


def save_image(task_id, concept_id, job_id, parent_id, action, options, path, details):
    path = Path(path).resolve(strict=True)
    expected = (OUTPUT / '产品设计' / ident(task_id)).resolve()
    if not path.is_relative_to(expected) or not (path.name.startswith(job_id) or path.parent.name == job_id):
        raise RuntimeError('输出路径异常')
    with Image.open(path) as image:
        if any(k.casefold() in ('prompt', 'workflow', 'parameters', 'description') for k in image.info):
            raise RuntimeError('图片包含内部生成描述，未登记结果')
        width, height = image.size
    task = store.get(task_id)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    existing = next((row for row in task['images'] if row['id'] == job_id), None)
    if existing:
        if existing['file'] != path.relative_to(OUTPUT.resolve()).as_posix():
            raise ValueError('已登记的图片路径不同，未重复登记')
        if existing.get('content_sha256') and existing['content_sha256'] != digest:
            raise ValueError('已登记的图片内容不同，未重复登记')
        return existing
    row = {**details, 'id': job_id, 'concept_id': concept_id, 'parent_id': parent_id,
           'version': 1 + sum(v['concept_id'] == concept_id for v in task['images']), 'kind': action,
           'file': path.relative_to(OUTPUT.resolve()).as_posix(), 'width': width, 'height': height,
           'quality': options['quality'], 'size': options['size'], 'created': stamp(),
           'aspect': options['aspect'], 'content_sha256': digest}
    task['images'].append(row)
    if options.get('edits_snapshot'):
        row['edits'] = options['edits_snapshot']
    floorplan = options.get('floorplan_snapshot') or next(
        (image.get('floorplan_snapshot') for image in task['images'] if image['id'] == parent_id), None)
    if floorplan:
        row['floorplan_snapshot'] = json.loads(json.dumps(floorplan, ensure_ascii=False))
    if action == 'export':
        parent_plan = next((image.get('floorplan_input') for image in task['images'] if image['id'] == parent_id), None)
        if parent_plan and parent_plan.get('mode') == 'floorplan_photographic':
            row['floorplan_input'] = json.loads(json.dumps(parent_plan, ensure_ascii=False))
    if options.get('variant_group'):
        row['variant_group'] = options['variant_group']
    basis = options.get('basis_snapshot') or generation_basis(task, concept_id, parent_id, action, options.get('change', ''), ROOT / 'private',
                                                            floorplan=floorplan, requirements=options.get('generation_requirements'))
    write_private(basis_file(ROOT / 'private', job_id), json.dumps(basis, ensure_ascii=False))
    task['status'] = '待选定方案' if not task['selected'] else '待评审'
    if not task['review'].get('image_id'):
        task['review'].update(design='待核验', culture='待核验', process='待核验')
    store.put(task, task['revision'])
    return row


async def local_image(task_id, concept_id, parent, action, options, value):
    job_id = uuid.uuid4().hex
    task = store.get(task_id)
    parent_id = parent['id'] if parent else None
    binding = dict(item_id=concept_id, concept_id=concept_id, parent_id=parent_id, action=action, origin='local')
    registration = dict(schema=1, task_id=task_id, job_id=job_id, state='awaiting_result',
                        options=saved_image_options(task, concept_id, parent_id, action, options), **binding)
    persist_recovery(registration_file(job_id), registration, '本机无法保存登记恢复资料，本次尚未提交图像引擎')
    update_job(task_id, registration_jobs={**task['job'].get('registration_jobs', {}), job_id: binding})
    write_private(private_file(job_id), json.dumps(value, ensure_ascii=False))
    try:
        history = await execute(task_id, graph(job_id, task_id, action, options['quality'], options['size'], options['aspect']))
        files = [image for row in history.get('outputs', {}).values() for image in row.get('images', []) if image.get('type') == 'output']
        if len(files) != 1:
            raise RuntimeError('输出数量异常，未登记图像')
        result = files[0]
        path = OUTPUT / result.get('subfolder', '') / result['filename']
        if action == 'export':
            details = {k: parent.get(k) for k in ('provider', 'model', 'native_width', 'native_height', 'reference_count')}
            details.update(seed=None, steps=None, processing='AI放大' if SIZES[options['size']] > max(parent['width'], parent['height']) else '尺寸导出')
        else:
            details = {'provider': 'local', 'model': 'flux-2-klein-base-4b' if options['quality'] == '精细' else 'flux-2-klein-4b',
                       'seed': int(job_id[:12], 16), 'steps': 28 if options['quality'] == '精细' else 4,
                       'processing': '本地生成与尺寸适配'}
        if path.is_symlink():
            raise ValueError('输出路径异常')
        raw = path.resolve(strict=True)
        folder = (OUTPUT / '产品设计' / ident(task_id)).resolve(strict=True)
        if raw.parent != folder or not raw.name.startswith(job_id) or raw.stat().st_nlink != 1:
            raise ValueError('输出路径异常')
        raw.chmod(0o600)
        registration.update(state='returned', raw=raw.relative_to(OUTPUT.resolve()).as_posix(),
                            raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(), result=details)
        try:
            try:
                persist_recovery(registration_file(job_id), registration, '已有图片的登记记录暂时无法保存')
            except RuntimeError:
                task = store.get(task_id)
                task['job']['registration_jobs'][job_id]['returned'] = {
                    key: registration[key] for key in ('raw', 'raw_sha256', 'result')}
                store.put(task, task['revision'])
            task = store.get(task_id)
            update_job(task_id, result_recovery=list(dict.fromkeys([*task['job'].get('result_recovery', []), job_id])))
            finish_registration(task_id, job_id)
        except Exception as error:
            if job_id in registration_recoveries(store.get(task_id)):
                raise RegistrationPending('图片已保存，尚未登记；可恢复已有结果，不会重新生图') from error
            raise RuntimeError('图片已保存，但恢复记录尚未确认；请联系管理员核查，不要重复处理') from error
        return source_image(store.get(task_id), job_id)
    finally:
        private_file(job_id).unlink(missing_ok=True)


def parallel_limit(image_config):
    if 'max_parallel' not in image_config:
        return 3
    value = image_config['max_parallel']
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 3 else 1


def mark(task_id, concept_id, state, cloud=False, finished=False):
    task = store.get(task_id)
    job = task['job']
    job.setdefault('items', {})[concept_id] = state
    if finished:
        job['completed'] = job.get('completed', 0) + 1
    if cloud:
        job['cloud_pending'] = any(value in ('running', 'uncertain') for value in job['items'].values())
    store.put(task, task['revision'])


def design_recipe(task, concept_id):
    concept_id = ident(concept_id)
    concept = next((row for row in task['concepts'] if row['id'] == concept_id and row.get('reference_design')), None)
    path = ROOT / 'private' / ('design-' + concept_id + '.json')
    if concept is None or not path.is_file() or path.is_symlink() or path.stat().st_size > 100_000:
        raise ValueError('找不到原来的参考设置，请重新选择参考图片')
    recipe = json.loads(path.read_text())
    if not isinstance(recipe, dict) or recipe.get('task_id') != task['id']:
        raise ValueError('参考设置不属于这份设计')
    if any(key not in recipe for key in ('change', 'edits', 'size', 'aspect', 'quality')):
        raise ValueError('原参考设置未能读取，已有结果仍保留')
    binding = concept.get('interior_view')
    if binding and recipe.get('interior_view') != binding:
        raise ValueError('这一版的空间布局记录已变化，已有结果仍保留')
    if concept.get('interior_photographic'):
        snapshot = recipe.get('interior_snapshot')
        if not isinstance(snapshot, dict) or snapshot.get('style_label') != concept.get('interior_style_label'):
            raise ValueError('这一版的室内风格或平面图记录未能读取，已有结果仍保留')
        if 'interior_finish_scheme' in recipe:
            validate_finish_scheme(recipe['interior_finish_scheme'], snapshot['style_label'])
        if 'interior_object_scheme' in recipe or 'interior_object_scheme_sha256' in concept:
            if not binding:
                raise ValueError('这一版的家具对应记录未能读取，已有结果仍保留')
            saved = validate_interior_object_scheme(recipe.get('interior_object_scheme'), binding['layout_snapshot'],
                snapshot['style_label'], interior_reference_order(snapshot, recipe['edits']))
            if interior_object_digest(saved) != concept.get('interior_object_scheme_sha256'):
                raise ValueError('这一版的家具对应记录已变化，已有结果仍保留')
    return concept, recipe


async def prepare(task, action, concept_id, options, config, image_config, cloud):
    ident(concept_id)
    concept = next((c for c in task['concepts'] if c['id'] == concept_id), None)
    if concept is None:
        raise ValueError('候选方向无效')
    requirements = generation_requirements(task, concept_id, action)
    text = '' if action in ('refine', 'export', 'compose', 'design') or concept.get('source_image_id') else payload(concept_id)['text']
    value = {'text': config['render_prefix'] + text}
    source = source_path = None
    source_id = options.get('image_id') if action in ('refine', 'export') else concept.get('source_image_id')
    if action in ('refine', 'export') and not source_id:
        raise ValueError('请选择要修改的图片版本')
    if source_id:
        source = next((v for v in task['images'] if v['id'] == source_id and v['concept_id'] == concept_id), None)
        if source is None:
            raise ValueError('请选择已选定方向的已有图像版本')
        source_path = (OUTPUT / source['file']).resolve(strict=True)
        if not source_path.is_relative_to((OUTPUT / '产品设计' / ident(task['id'])).resolve()):
            raise ValueError('图像路径无效')
        value.update(source=str(source_path), text=config['edit_prefix'] + (options.get('change') or task['brief']['goal']))
    refs, edits, local = [], [], None
    floorplan_input = None
    floorplan = options.get('floorplan_snapshot') or (source.get('floorplan_snapshot') if source and action == 'refine' else None)
    anchor = await asyncio.to_thread(structure_anchor, floorplan, task, OUTPUT) if floorplan else None
    anchor_path = str((OUTPUT / anchor['file']).resolve(strict=True)) if anchor else None
    if cloud:
        if action == 'design' and options.get('interior_snapshot'):
            edits = await asyncio.to_thread(resolve_edits, task, options['edits'], catalog, OUTPUT, compose=True)
            if options.get('retry_concept_id') or options.get('resume_concept_ids'):
                _, recipe = design_recipe(task, concept_id)
                await asyncio.to_thread(validate_frozen_design_references, task, concept, edits, OUTPUT, recipe['edits'])
            snapshot = await asyncio.to_thread(interior_input, task, options['interior_snapshot'], edits, OUTPUT)
            edits = interior_reference_order(snapshot, edits)
            binding = concept.get('interior_view')
            object_scheme = None
            if binding:
                layout = validate_layout(binding['layout_snapshot'])
                if layout_digest(layout) != binding['layout_sha256']:
                    raise ValueError('这一版的空间布局记录已变化，未重新生成')
                if 'interior_object_scheme' in options or 'interior_object_scheme_sha256' in concept:
                    saved = options.get('interior_object_scheme')
                    if 'interior_object_scheme' not in options:
                        saved = design_recipe(task, concept_id)[1].get('interior_object_scheme')
                    object_scheme = validate_interior_object_scheme(saved, layout, snapshot['style_label'], edits)
                    if interior_object_digest(object_scheme) != concept.get('interior_object_scheme_sha256'):
                        raise ValueError('这一版的家具对应记录已变化，未重新生成')
                await asyncio.to_thread(interior_layout_reference, task, {**snapshot, **binding}, OUTPUT)
                guide = await asyncio.to_thread(resolve_edits, task,
                    [{'source': 'upload', 'id': binding['structure_id'], 'use': 'shape'}], catalog, OUTPUT, compose=True)
                edits = [guide[0], *edits[1:]]
            paths, _ = interior_reference_layout(edits)
            for index, path in enumerate(paths):
                refs.append(await asyncio.to_thread(selected_reference, path['path'], path['selection']) if isinstance(path, dict)
                            else await asyncio.to_thread(reference_data, path, (OUTPUT, ARCHIVE), linework=index == 0))
            if len(refs) > MAX_REFERENCES:
                raise ValueError('平面图、风格和补充参考合计超过图片上限，请减少参考')
            if binding:
                view = dict(space_id=binding['space_id'], label=binding['view_label'], camera=binding['camera'],
                            layout_version=binding.get('layout_version', 8))
                anchors = (await asyncio.to_thread(view_object_anchors, layout, view, include_neighbors=True)
                           if object_scheme is not None else None)
                value['text'] = photographic_text(layout, view, snapshot['style_label'],
                    snapshot.get('style_description', ''), options['change'],
                    finish_scheme=options.get('interior_finish_scheme'), anchors=anchors) + '\n' + interior_extra_text(edits)
                if object_scheme is not None:
                    value['text'] += '\n' + interior_object_text(object_scheme, layout, view, edits,
                                                               snapshot['style_label'], anchors=anchors)
            else:
                value['text'] = interior_text(snapshot, options['change'], edits)
            if not binding and concept.get('design_total', 1) > 1 and not options.get('retry_concept_id'):
                value['text'] += '\n多个方案只调整未限定的材质、装饰与照明，不改变平面图布局、门窗位置或家具数量。每张仅呈现一个完整室内空间。'
            floorplan_input = {**snapshot, **(binding or {}), 'reference_index': 1, 'reference_count': len(refs)}
            if options.get('interior_finish_scheme') is not None:
                floorplan_input['interior_finish_scheme'] = validate_finish_scheme(
                    options['interior_finish_scheme'], snapshot['style_label'])
            if object_scheme is not None:
                floorplan_input['interior_object_scheme'] = object_scheme
                floorplan_input['interior_object_scheme_sha256'] = interior_object_digest(object_scheme)
        elif action in ('refine', 'compose', 'design') or concept.get('source_image_id'):
            anchored_design = action == 'design' and anchor is not None
            compose = action in ('compose', 'design') and not anchored_design
            edit_values = options.get('edits', [])
            if anchored_design:
                source_path = Path(anchor_path)
                value['source'] = anchor_path
                edit_values = [row for row in edit_values if not (row.get('source') == 'upload' and row.get('id') == anchor['id'])]
            edits = await asyncio.to_thread(resolve_edits, task, edit_values, catalog, OUTPUT, compose=compose)
            if not compose:
                if options.get('edit_mode') == 'local':
                    data, crop, dimensions = await asyncio.to_thread(local_context, source_path, options['selection'])
                    local = {'selection': options['selection'], 'crop': crop, 'source_size': dimensions,
                             'proof': options['selection_proof']}
                    refs.append(data)
                else:
                    refs.append(await asyncio.to_thread(reference_data, source_path, (OUTPUT,)))
            instruction, reference_paths = edit_text(options['change'] if action in ('refine', 'compose', 'design') else task['brief']['goal'], edits, compose=compose)
            interior = source.get('floorplan_input', {}) if source and action == 'refine' else {}
            plan_path = str(await asyncio.to_thread(interior_layout_reference, task, interior, OUTPUT)) if interior.get('mode') == 'floorplan_photographic' else None
            for path in reference_paths:
                if isinstance(path, dict):
                    refs.append(await asyncio.to_thread(selected_reference, path['path'], path['selection']))
                else:
                    refs.append(await asyncio.to_thread(reference_data, path, (OUTPUT, ARCHIVE), linework=path == plan_path))
            if anchor and action == 'refine' and anchor_path not in reference_paths:
                refs.append(await asyncio.to_thread(reference_data, anchor_path, (OUTPUT,)))
            if plan_path:
                if plan_path not in reference_paths:
                    refs.append(await asyncio.to_thread(reference_data, plan_path, (OUTPUT,), linework=True))
                    plan_index = len(refs)
                else:
                    plan_index = reference_paths.index(plan_path) + 2
                floorplan_input = {**interior, 'reference_index': plan_index, 'reference_count': len(refs), 'role': 'version_bound_layout'}
            if len(refs) > MAX_REFERENCES:
                raise ValueError('保留本版布局依据后图片超过10张，请减少补充参考' if floorplan_input and floorplan_input.get('mode') == 'floorplan_photographic'
                                 else '带上本版结构灰模后参考图超过上限，请减少其他参考图片')
            # Free composition has no base; a floorplan design uses its version-bound greybox.
            value['text'] = (image_config.get('reference_rules', '') if compose else image_config['edit_rules']) + '\n' + instruction
            if floorplan_input and floorplan_input.get('mode') == 'floorplan_photographic':
                basis = '当前视角结构底稿' if floorplan_input.get('reference_kind') == 'derived_structure' else '原始平面图'
                value['text'] += f"\n图{floorplan_input['reference_index']}是这一版绑定的{basis}，只用于核对墙体、门窗、开放边界、家具位置和相机，不是材质参考或成图底图。以图1实景图为修改底图，只做本次指定修改，不改变其余布局，不添加尺寸或图纸文字。"
            if anchor:
                anchor_index = 1 if anchored_design else reference_paths.index(anchor_path) + 2 if anchor_path in reference_paths else len(refs)
                value['text'] += f'\n图{anchor_index}是本版确认的结构灰模，只用于核对墙体、开口、物件布置与相机，不是材质样图。'
                if anchored_design:
                    value['text'] += '\n以图1结构灰模为唯一空间底图，给既有表面设计材质与光线；其他参考不提供房间布局或相机。'
                floorplan_input = {'structure_id': anchor['id'], 'reference_index': anchor_index,
                                  'mode': 'structure_base' if anchored_design else 'version_bound_reference'}
            if action == 'design':
                number, total = concept.get('design_index', 1), concept.get('design_total', 1)
                value['text'] += f'\n本次是{total}个备选设计中的第{number}个。只画一个完整方案，不把多个方案拼在同一张图里。'
                if total > 1 and not anchored_design:
                    value['text'] += '\n在相同设计要求和参考用途下探索不同的布局、形态比例和细节，不改变明确指定的材质、对象和数量。'
                    direction = ('整体布局与主次关系', '造型比例与体量层次', '细部工艺与光线组织')[(number - 1) % 3]
                    value['text'] += f'本方案侧重探索{direction}。这些变化仅限用户没有明确限定的部分。'
                elif total > 1:
                    value['text'] += '\n备选方案只探索未限定的表面材质、装饰细节和光线，不改变灰模的布局、物件数量和相机。'
            if local:
                box = region_in_crop(local['selection'], local['source_size'], local['crop'])
                value['text'] += '\n图1是原场景的局部裁图。仅修改 <bbox>' + ' '.join(map(str, box)) + '</bbox> 内的指定内容，保持构图位置、边缘结构与周围光线一致，不添加选区标记。'
                for index, row in enumerate(edits):
                    if row['target_selection']:
                        target = region_in_crop(row['target_selection'], local['source_size'], local['crop'])
                        value['text'] += f'\n第{index + 1}项参考内容应用于图1 <bbox>' + ' '.join(map(str, target)) + '</bbox>。'
                        if row['fidelity'] == 'original':
                            value['text'] += '该位置的物件将由后续像素合成放入，本步仅清理原物件并准备空位置，不要另行绘制或复制该物件。'
            elif source_path:
                dimensions = (await asyncio.to_thread(open_image, source_path)).size
                for index, row in enumerate(edits):
                    if row['target_selection']:
                        target = region_in_crop(row['target_selection'], dimensions, (0, 0, *dimensions))
                        value['text'] += f'\n第{index + 1}项参考内容应用于图1 <bbox>' + ' '.join(map(str, target)) + '</bbox>。'
            if source and source.get('kind') == 'upload':
                value['text'] += '\n' + json.dumps({'input_kind': source['upload_mode'],
                                                   'source_role': '保留的上传底图'}, ensure_ascii=False)
        else:
            entries = await asyncio.to_thread(reference_entries, catalog, concept)
            for entry in entries:
                if entry.get('source_selection'):
                    refs.append(await asyncio.to_thread(selected_reference, entry['source'], entry['source_selection']))
                else:
                    refs.append(await asyncio.to_thread(reference_data, entry['source'], (ARCHIVE,)))
            if entries:
                value['text'] += '\n' + image_config['reference_rules'] + '\n' + json.dumps({'reference_images': [
                    {'index': index + 1, 'role': entry['role'], 'title': entry['title'],
                     **{k: entry[k] for k in ('use', 'take') if k in entry},
                     'selected_part': bool(entry.get('source_selection'))}
                    for index, entry in enumerate(entries)]}, ensure_ascii=False)
    if action != 'export' and requirements['brief'] is not None:
        value['text'] += '\n' + json.dumps({'design_constraints': requirements['brief']}, ensure_ascii=False)
    if floorplan:
        value['text'] += '\n' + floorplan_constraints(floorplan)
    return {'concept_id': concept_id, 'value': value, 'refs': refs, 'source': source,
            'edits_snapshot': public_edits(edits), 'local': local, 'resolved_edits': edits, 'floorplan_input': floorplan_input,
            'generation_requirements': requirements, 'change': options.get('change', '')}


def recovery_file(job_id):
    return ROOT / 'private' / ('local-' + ident(job_id) + '.json')


class ExecutionCancelled(RuntimeError):
    pass


class RegistrationPending(RuntimeError):
    pass


def registration_file(job_id):
    return ROOT / 'private' / ('registration-' + ident(job_id) + '.json')


def saved_image_options(task, concept_id, parent_id, action, options, change=None, edits=None, requirements=None):
    saved = {key: options[key] for key in ('quality', 'size', 'aspect')}
    saved.update(change=options.get('change', '') if change is None else change,
                 edits_snapshot=options.get('edits_snapshot', []) if edits is None else edits)
    for key in ('floorplan_snapshot', 'variant_group'):
        if options.get(key):
            saved[key] = options[key]
    parent = source_image(task, parent_id) if parent_id else None
    if not saved.get('floorplan_snapshot') and parent and parent.get('floorplan_snapshot'):
        saved['floorplan_snapshot'] = parent['floorplan_snapshot']
    saved['basis_snapshot'] = generation_basis(task, concept_id, parent_id, action, saved['change'], ROOT / 'private',
        floorplan=saved.get('floorplan_snapshot'), requirements=requirements or options.get('generation_requirements'))
    return saved


def persist_recovery(file, record, message):
    binary = json.dumps(record, ensure_ascii=False)
    if len(binary.encode()) > 1_000_000 or file.parent.is_symlink():
        raise ValueError('恢复资料无法安全保存，未继续处理')
    try:
        write_private(file, binary)
    except OSError as error:
        raise RuntimeError(message) from error


def read_registration(task, job_id):
    job_id = ident(job_id)
    bindings = (task.get('job') or {}).get('registration_jobs', {})
    if not isinstance(bindings, dict):
        raise ValueError('登记恢复记录无效，请联系管理员核查')
    binding = bindings.get(job_id)
    if not isinstance(binding, dict):
        raise ValueError('待登记结果不属于当前操作')
    file = registration_file(job_id)
    if file.parent.is_symlink() or file.is_symlink() or file.stat().st_mode & 0o077 or file.stat().st_size > 1_000_000:
        raise ValueError('登记恢复记录无效')
    record = json.loads(file.read_text())
    if (not isinstance(record, dict) or record.get('schema') != 1 or record.get('task_id') != task['id']
            or record.get('job_id') != job_id or any(record.get(key) != binding.get(key)
                for key in ('item_id', 'concept_id', 'parent_id', 'action', 'origin'))
            or record.get('origin') not in (None, 'local')
            or record.get('action') not in (('generate', 'refine', 'design', 'compose', 'export')
                if record.get('origin') == 'local' else ('generate', 'refine', 'design', 'compose'))
            or not any(row['id'] == record['concept_id'] for row in task['concepts'])
            or not isinstance(record.get('options'), dict) or not isinstance(record['options'].get('basis_snapshot'), dict)):
        raise ValueError('登记恢复记录与设计版本不一致')
    if record['parent_id']:
        parent = source_image(task, record['parent_id'])
        if parent['concept_id'] != record['concept_id']:
            raise ValueError('待登记结果的原版本不一致')
    if record.get('origin') == 'local' and 'returned' in binding:
        returned = binding['returned']
        if not isinstance(returned, dict) or set(returned) != {'raw', 'raw_sha256', 'result'}:
            raise ValueError('已有图片的恢复绑定无效')
        record = {**record, **returned, 'state': 'returned'}
    return record


def returned_registration(record):
    if record.get('state') == 'returned':
        return record
    if record.get('origin') == 'local':
        raise ValueError('图像引擎结果尚未保存到恢复记录')
    folder = OUTPUT / '产品设计' / ident(record['task_id']) / ident(record['job_id'])
    if folder.is_symlink() or folder.parent.is_symlink():
        raise ValueError('待登记结果目录无效')
    receipt = folder / 'receipt.json'
    if receipt.is_symlink() or receipt.stat().st_mode & 0o077 or receipt.stat().st_size > 65_536:
        raise ValueError('生成结果记录无效')
    saved = json.loads(receipt.read_text())
    if (not isinstance(saved, dict) or saved.get('state') != 'saved'
            or saved.get('file') not in ('original.png', 'original.jpg', 'original.webp')):
        raise ValueError('本次生成结果尚未确认，未执行登记恢复')
    return {**record, 'state': 'returned', 'raw': (folder / saved['file']).relative_to(OUTPUT).as_posix(),
            'raw_sha256': saved['sha256'], 'result': {**record.get('result_context', {}),
                **{key: saved[key] for key in ('model', 'width', 'height', 'requested_size', 'reference_count') if key in saved},
                'native_width': saved.get('width'), 'native_height': saved.get('height')}}


def registration_recoveries(task):
    job = task.get('job') or {}
    values = job.get('result_recovery', [])
    bindings = job.get('registration_jobs', {})
    if not isinstance(values, list) or not isinstance(bindings, dict):
        raise ValueError('登记恢复记录无效，请联系管理员核查')
    pending = list(dict.fromkeys(ident(value) for value in values))
    for job_id in bindings:
        if job_id in pending:
            continue
        try:
            returned_registration(read_registration(task, job_id))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        pending.append(job_id)
    return pending


def registration_result(task, job_id):
    record = returned_registration(read_registration(task, job_id))
    return record, verified_result_image(record)


def verified_result_image(record):
    raw = OUTPUT / record['raw']
    folder = OUTPUT / '产品设计' / ident(record['task_id'])
    local = record.get('origin') == 'local'
    if not local:
        folder = folder / ident(record['job_id'])
    valid_name = raw.name.startswith(ident(record['job_id'])) if local else raw.name in ('original.png', 'original.jpg', 'original.webp')
    if (folder.is_symlink() or folder.parent.is_symlink() or raw.is_symlink() or not valid_name
            or raw.resolve(strict=True).parent != folder.resolve(strict=True)
            or raw.stat().st_mode & 0o077 or raw.stat().st_size > 96 * 1024 * 1024
            or not re.fullmatch(r'[a-f0-9]{64}', str(record.get('raw_sha256', '')))
            or hashlib.sha256(raw.read_bytes()).hexdigest() != record['raw_sha256']):
        raise ValueError('已保存图片已变化或不完整，未恢复登记，请联系管理员核查')
    with Image.open(raw) as image:
        if (image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 24_000_000
                or any(key.casefold() in ('prompt', 'workflow', 'parameters', 'description') for key in image.info)):
            raise ValueError('已保存图片未通过核验，未恢复登记')
        image.verify()
    return raw


def finish_registration(task_id, job_id, recovered=False):
    task = store.get(task_id)
    if job_id not in registration_recoveries(task):
        raise ValueError('没有待登记的结果')
    record, raw = registration_result(task, job_id)
    details = {'seed': None, 'steps': None, **record['result'],
               **({'registration_recovered_at': stamp()} if recovered else {})}
    save_image(task_id, record['concept_id'], job_id, record['parent_id'], record['action'], record['options'], raw, details)
    task = store.get(task_id)
    job = task['job']
    if recovered and job.setdefault('items', {}).get(record['item_id']) != 'done':
        job['items'][record['item_id']] = 'done'
        job['completed'] = job.get('completed', 0) + 1
    job['result_recovery'] = [value for value in registration_recoveries(task) if value != job_id]
    job.get('registration_jobs', {}).pop(job_id, None)
    if recovered:
        job['cloud_pending'] = any(value in ('running', 'uncertain') for value in job.get('items', {}).values())
    store.put(task, task['revision'])
    try:
        registration_file(job_id).unlink(missing_ok=True)
    except OSError:
        pass


def read_local_recovery(task, job_id):
    job_id = ident(job_id)
    job = task.get('job') or {}
    bindings = job.get('local_jobs', {})
    if not isinstance(bindings, dict):
        raise ValueError('合成恢复绑定无效，请联系管理员核查')
    binding = bindings.get(job_id)
    if job_id not in job.get('local_recovery', []) and binding is None:
        raise ValueError('待合成结果不属于当前操作')
    file = recovery_file(job_id)
    if file.parent.is_symlink() or file.is_symlink() or file.stat().st_mode & 0o077 or file.stat().st_size > 1_000_000:
        raise ValueError('合成记录无效')
    record = json.loads(file.read_text())
    if (not isinstance(record, dict) or record.get('task_id') != task['id']
            or not isinstance(record.get('local'), dict) or not isinstance(record.get('options'), dict)
            or not isinstance(record.get('preserve'), list) or not isinstance(record.get('preserve_sha256'), list)
            or len(record['preserve']) != len(record['preserve_sha256'])):
        raise ValueError('合成记录与设计版本不一致')
    if record.get('schema') is not None:
        if (record.get('schema') != 1 or record.get('job_id') != job_id or not isinstance(binding, dict)
                or any(record.get(key) != binding.get(key) for key in ('source_id', 'item_id'))
                or not isinstance(record['options'].get('basis_snapshot'), dict)):
            raise ValueError('合成恢复绑定与设计版本不一致')
    else:
        if job_id not in job.get('local_recovery', []):
            raise ValueError('旧合成结果缺少当前操作绑定')
        record = {**record, 'job_id': job_id, 'state': 'returned'}
    source_image(task, record['source_id'])
    return record


def local_recoveries(task):
    job = task.get('job') or {}
    values, bindings = job.get('local_recovery', []), job.get('local_jobs', {})
    if not isinstance(values, list) or not isinstance(bindings, dict):
        raise ValueError('合成恢复记录无效，请联系管理员核查')
    pending = list(dict.fromkeys(ident(value) for value in values))
    for job_id in bindings:
        if job_id in pending:
            continue
        try:
            returned_registration(read_local_recovery(task, job_id))
        except (OSError, ValueError, KeyError, TypeError):
            continue
        pending.append(job_id)
    return pending


def local_result(task, job_id):
    record = returned_registration(read_local_recovery(task, job_id))
    return record, verified_result_image(record)


def recovery_job_view(task):
    job = task.get('job')
    for key, pending in (('result_recovery', registration_recoveries(task)), ('local_recovery', local_recoveries(task))):
        if pending:
            job = {**job, key: pending}
    return job


async def finish_local(task_id, job_id, recovered=False):
    task = store.get(task_id)
    if job_id not in local_recoveries(task):
        raise ValueError('没有待合成的图片')
    record, raw = local_result(task, job_id)
    source = source_image(task, record['source_id'])
    base = (OUTPUT / source['file']).resolve(strict=True)
    folder = (OUTPUT / '产品设计' / ident(task_id)).resolve()
    if not base.is_relative_to(folder) or raw.parent != folder / ident(job_id):
        raise ValueError('合成图片路径无效')
    proof = await asyncio.to_thread(selection_proof, base, record['local']['selection'])
    if proof != record['local']['proof'] or hashlib.sha256(raw.read_bytes()).hexdigest() != record['raw_sha256']:
        raise ValueError('底图、选区或生成图已变化，未合成，请联系管理员核查')
    preserve = await asyncio.to_thread(resolve_edits, task, record['preserve'], catalog, OUTPUT)
    for row, digest in zip(preserve, record['preserve_sha256']):
        if hashlib.sha256(Path(row['paths'][0]).read_bytes()).hexdigest() != digest:
            raise ValueError('待放入的参考图已变化，未合成')
    destination = raw.with_name('edited.png')
    verification = await asyncio.to_thread(composite_local, base, raw, record['local']['selection'],
        record['local']['crop'], destination, preserve)
    details = {**record['result'], 'processing': '局部修改与原图合成', 'local_edit': verification,
               'edit_selection': record['local']['selection'], 'seed': None, 'steps': None}
    save_image(task_id, source['concept_id'], job_id, source['id'], 'refine', record['options'], destination, details)
    task = store.get(task_id)
    task['job']['local_recovery'] = [value for value in local_recoveries(task) if value != job_id]
    task['job'].get('local_jobs', {}).pop(job_id, None)
    if recovered and task['job'].setdefault('items', {}).get(record['item_id']) != 'done':
        task['job']['completed'] = task['job'].get('completed', 0) + 1
    task['job'].setdefault('items', {})[record['item_id']] = 'done'
    store.put(task, task['revision'])
    try:
        recovery_file(job_id).unlink(missing_ok=True)
    except OSError:
        pass


async def cloud_one(task_id, action, item, options):
    job_id = uuid.uuid4().hex
    item_id = item.get('item_id', item['concept_id'])
    mark(task_id, item_id, 'running', cloud=True)
    update_job(task_id, cloud_job_id=job_id)
    registration, record = None, None
    task = store.get(task_id)
    local = item.get('local')
    plan_context = {'floorplan_input': item['floorplan_input']} if item.get('floorplan_input') else {}
    if local:
        preserve = [row for row in item['resolved_edits'] if row['fidelity'] == 'original']
        binding = dict(source_id=item['source']['id'], item_id=item_id)
        record = dict(schema=1, task_id=task_id, job_id=job_id, state='awaiting_result', local=local,
                      preserve=public_edits(preserve),
                      preserve_sha256=[hashlib.sha256(Path(row['paths'][0]).read_bytes()).hexdigest() for row in preserve],
                      options=saved_image_options(task, item['concept_id'], item['source']['id'], action, options,
                          change=item.get('change', options.get('change', '')), edits=item.get('edits_snapshot', []),
                          requirements=item.get('generation_requirements')),
                      result_context=dict(provider='ark', processing='模型原图', watermark=False, **plan_context), **binding)
        persist_recovery(recovery_file(job_id), record, '本机无法保存合成恢复资料，本次尚未发送生图请求')
        update_job(task_id, local_jobs={**task['job'].get('local_jobs', {}), job_id: binding})
    else:
        parent_id = item['source']['id'] if item['source'] else None
        binding = dict(item_id=item_id, concept_id=item['concept_id'], parent_id=parent_id, action=action)
        saved_options = saved_image_options(task, item['concept_id'], parent_id, action, options,
            change=item.get('change', options.get('change', '')), edits=item.get('edits_snapshot', []),
            requirements=item.get('generation_requirements'))
        registration = dict(schema=1, task_id=task_id, job_id=job_id, state='awaiting_result',
                            options=saved_options, result_context=dict(provider='ark', processing='模型原图', watermark=False,
                                **plan_context), **binding)
        persist_recovery(registration_file(job_id), registration, '本机无法保存登记恢复资料，本次尚未发送生图请求')
        update_job(task_id, registration_jobs={**task['job'].get('registration_jobs', {}), job_id: binding})
    try:
        extra = {}
        if local:
            x1, y1, x2, y2 = local['crop']
            extra['source_size'] = (x2 - x1, y2 - y1)
        elif options['aspect'] == 'source':
            with Image.open(item['value']['source']) as original:
                extra['source_size'] = original.size
        result = await asyncio.to_thread(cloud_generate, OPS / 'studio-image.json', item['value']['text'], item['refs'],
                                         options['size'], 'source' if local else options['aspect'], OUTPUT / '产品设计' / task_id / job_id, **extra)
    except (ValueError, RuntimeError):
        raise
    except Exception as error:
        raise GenerationUncertain('云端可能已完成，但本地登记未确认；请先检查输出记录，不要重复生图') from error
    try:
        path = result.pop('path')
        if local:
            record.update(state='returned', raw=Path(path).relative_to(OUTPUT).as_posix(),
                          raw_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(), result={**result, **plan_context})
            persist_recovery(recovery_file(job_id), record, '已生成图片的合成记录暂时无法更新')
            pending = store.get(task_id)['job'].get('local_recovery', [])
            update_job(task_id, local_recovery=[*pending, job_id])
            try:
                await finish_local(task_id, job_id)
            except Exception as error:
                update_job(task_id, local_error=str(error) if isinstance(error, ValueError) else '本地合成中断，请继续处理或保留结果后结束本次操作。')
                raise RuntimeError('图片已生成，合成尚未完成。可继续处理已生成的图片，不会重新生图') from error
            return
        raw = Path(path).resolve(strict=True)
        folder = (OUTPUT / '产品设计' / ident(task_id) / ident(job_id)).resolve(strict=True)
        if raw.parent != folder or raw.name not in ('original.png', 'original.jpg', 'original.webp'):
            raise ValueError('生成结果路径无效')
        registration.update(state='returned', raw=raw.relative_to(OUTPUT.resolve()).as_posix(),
                            raw_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(), result={**result, **plan_context})
        write_private(registration_file(job_id), json.dumps(registration, ensure_ascii=False))
        task = store.get(task_id)
        update_job(task_id, result_recovery=list(dict.fromkeys([*task['job'].get('result_recovery', []), job_id])))
        finish_registration(task_id, job_id)
    except Exception as error:
        if job_id in local_recoveries(store.get(task_id)):
            raise RuntimeError('图片已生成，合成尚未完成。可继续处理已生成的图片，不会重新生图') from error
        if job_id in registration_recoveries(store.get(task_id)):
            raise RegistrationPending('图片已保存，尚未登记；可恢复已有结果，不会重新生图') from error
        raise GenerationUncertain('图片已返回，但本地登记尚未确认；请先核对结果，不要重复生成') from error


def failed_state(error):
    if isinstance(error, ExecutionCancelled):
        return 'skipped'
    if isinstance(error, RegistrationPending):
        return 'registration-pending'
    return 'uncertain' if isinstance(error, (GenerationUncertain, CheckUncertain)) else 'failed'


async def produce(task_id, action, concept_ids, options):
    task = store.get(task_id)
    config = json.loads((OPS / 'studio-runtime.json').read_text())
    image_config = read_config(OPS / 'studio-image.json') if action != 'export' else {}
    cloud = image_config.get('provider') == 'ark'
    if cloud and (options.get('cloud_consent') is not True or options.get('cloud_consent_provider') != image_config['provider']):
        raise ValueError('请先确认本次云端生图及参考图发送范围')
    if options.get('variants'):
        items = []
        change = options['change']
        for alias in sorted({name for product in catalog.products() for name in [product['name'], *product['aliases']]}, key=len, reverse=True):
            change = re.sub(re.escape(alias) + ('(?!画)' if alias == '敦煌壁' else ''), '本次样图所示石材', change)
        for variant in options['variants']:
            item = await prepare(task, action, concept_ids[0], {**options, 'edits': variant, 'change': change}, config, image_config, cloud)
            item['item_id'] = uuid.uuid4().hex
            items.append(item)
    else:
        items = [await prepare(task, action, concept_id, options, config, image_config, cloud)
                 for concept_id in dict.fromkeys(concept_ids)]
    limit = min(parallel_limit(image_config), len(items)) if cloud and (action in ('generate', 'design') or options.get('variants')) else 1
    key = lambda item: item.get('item_id', item['concept_id'])
    update_job(task_id, items={key(item): 'queued' for item in items}, total=len(items), completed=0, parallel=limit,
               **({'phase': 'interior-views'} if action == 'design' and task.get('interior_input') else {}))
    if limit <= 1 and not options.get('variants'):
        for index, item in enumerate(items):
            if store.get(task_id)['job'].get('cancel'):
                for rest in items[index:]:
                    mark(task_id, key(rest), 'skipped')
                return
            update_job(task_id, message=f'正在处理 {index + 1}/{len(items)}，可离开页面稍后查看')
            try:
                if cloud:
                    await cloud_one(task_id, action, item, options)
                else:
                    mark(task_id, item['concept_id'], 'running')
                    await local_image(task_id, item['concept_id'], item['source'],
                                      'refine' if item['source'] and action == 'generate' else action,
                                      {**options, 'generation_requirements': item.get('generation_requirements')}, item['value'])
            except Exception as error:
                mark(task_id, key(item), failed_state(error), cloud=cloud)
                for rest in items[index + 1:]:
                    mark(task_id, key(rest), 'skipped')
                raise
            mark(task_id, key(item), 'done', cloud=cloud, finished=True)
        return
    gate, failures = asyncio.Semaphore(limit), {}

    async def run(item):
        async with gate:
            if store.get(task_id)['job'].get('cancel'):
                mark(task_id, key(item), 'skipped')
                return
            try:
                await cloud_one(task_id, action, item, options)
            except Exception as error:
                failures[key(item)] = error
                mark(task_id, key(item), failed_state(error), cloud=True)
                return
            mark(task_id, key(item), 'done', cloud=True, finished=True)

    update_job(task_id, message=f'正在生成 {len(items)} 张，可离开页面稍后查看')
    await asyncio.gather(*(run(item) for item in items))
    uncertain = [error for error in failures.values() if failed_state(error) == 'uncertain']
    if uncertain:
        raise GenerationUncertain(str(uncertain[0]))
    registration_pending = [error for error in failures.values() if isinstance(error, RegistrationPending)]
    if registration_pending:
        raise RegistrationPending(str(registration_pending[0]))
    if failures:
        raise RuntimeError(f'{len(failures)} 张没有生成，其余已保留：{next(iter(failures.values()))}')


async def work(task_id, action, options):
    created_design_concept_ids = []
    try:
        task = store.get(task_id)
        if action == 'recover-results':
            failures = []
            for job_id in registration_recoveries(task):
                try:
                    finish_registration(task_id, job_id, recovered=True)
                except Exception as error:
                    failures.append(str(error) if isinstance(error, ValueError) else '已有结果仍未登记，请稍后重试或联系管理员核查。')
            if failures:
                update_job(task_id, registration_error='；'.join(failures[:3]))
                raise RegistrationPending(failures[0])
            if any(state in ('running', 'uncertain') for state in store.get(task_id)['job'].get('items', {}).values()):
                raise GenerationUncertain('已有结果已恢复登记，另有请求尚未确认；请联系管理员核查，不会自动重试')
            update_job(task_id, cloud_pending=False, registration_error='')
            if local_recoveries(store.get(task_id)):
                update_job(task_id, status='interrupted', message='已有结果已恢复登记，另有已保存图片尚未完成本地合成；可继续处理，不会重新生图')
                return
        elif action == 'finish-local':
            failures = []
            for job_id in local_recoveries(task):
                try:
                    await finish_local(task_id, job_id, recovered=True)
                except Exception as error:
                    failures.append(str(error) if isinstance(error, ValueError) else '本地合成中断，可以继续处理或结束本次合成。')
            if failures:
                update_job(task_id, local_error='；'.join(failures[:3]))
                raise ValueError(failures[0])
            if any(state in ('running', 'uncertain') for state in store.get(task_id)['job'].get('items', {}).values()):
                raise GenerationUncertain('已完成本地合成，另有请求的结果尚未确认，请联系管理员核查')
            update_job(task_id, cloud_pending=False)
            if registration_recoveries(store.get(task_id)):
                raise RegistrationPending('已完成本地合成，另有已保存图片尚未登记；可恢复已有结果，不会重新生图')
        elif action == 'plan':
            remote_plan = not planner_status()['local_only']
            if remote_plan and not options.get('plan_consent'):
                raise ValueError('请重新确认云端构思发送内容；本次尚未发送')
            job_id = uuid.uuid4().hex
            refs, snapshots = [], []
            for ref in task['references']:
                entry = await asyncio.to_thread(catalog.get, ref['id'])
                snapshots.append(await asyncio.to_thread(catalog.snapshot, ref))
                fields = {k: str(v)[:800 if k == 'visual_description' else 250] for k, v in entry['fields'].items()}
                refs.append({**{k: entry[k] for k in ('id', 'title', 'rights', 'origin')}, 'fields': fields, 'role': ref['role'],
                             **{k: ref[k] for k in ('use', 'take') if k in ref},
                             'selected_part': bool(ref.get('source_selection'))})
            request = {'brief': task['brief'], 'references': refs,
                       'reviewed_feedback': store.context_feedback(task['brief']['category'])}
            write_private(private_file(job_id), json.dumps(request, ensure_ascii=False))
            update_job(task_id, message=('DeepSeek' if remote_plan else '本地模型') + '正在构思三个方向',
                       cloud_pending=remote_plan, cloud_operation='plan')
            await execute(task_id, {'1': {'class_type': 'ZybStudioPlanner', 'inputs': {'job_id': job_id}}})
            raw = json.loads(private_file(job_id, 'result.json').read_text())
            concepts, private = validate_plan(raw, refs)
            task = store.get(task_id)
            for concept_id, text in private.items():
                write_private(private_file(concept_id), json.dumps({'text': text}, ensure_ascii=False))
            batch = uuid.uuid4().hex
            for concept in concepts:
                concept.update(batch=batch, created=stamp(), brief_revision=task.get('brief_revision', 1),
                               reference_snapshot=snapshots, reference_snapshot_version=2)
            task['concepts'].extend(concepts)
            task['job']['cloud_pending'] = False
            task['selected'] = None
            reset_review(task)
            task['status'] = '待比较方向'
            store.put(task, task['revision'])
            private_file(job_id).unlink(missing_ok=True)
            private_file(job_id, 'result.json').unlink(missing_ok=True)
            if options.get('render') and not store.get(task_id)['job'].get('cancel'):
                update_job(task_id, action='generate', phase='render', cloud_operation='image', message='方案已出，接着出图')
                await produce(task_id, 'generate', [concept['id'] for concept in concepts], options)
        elif action == 'check':
            update_job(task_id, message='DeepSeek 正在核对当前版本；不会自动修改图片或评审结论',
                       cloud_pending=True, cloud_operation='vision')
            result = await asyncio.to_thread(inspect_image, OPS / 'studio-vision.json', task,
                                            options['image_id'], OUTPUT, ROOT / 'private', options['standard'])
            result.update(id=uuid.uuid4().hex, created=stamp())
            task = store.get(task_id)
            task.setdefault('visual_checks', []).append(result)
            task['job']['cloud_pending'] = False
            store.put(task, task['revision'])
        else:
            concept_ids = options.get('concept_ids', [])
            if action == 'design':
                if options.get('resume_concept_ids'):
                    concept_ids = options['resume_concept_ids']
                elif options.get('retry_concept_id'):
                    concept_ids = [options['retry_concept_id']]
                else:
                    resolved = await asyncio.to_thread(resolve_edits, task, options['edits'], catalog, OUTPUT, compose=True)
                    snapshots = [ref for row in resolved for ref in row['references']]
                    views = [None] * options['count']
                    if options.get('interior_snapshot'):
                        update_job(task_id, phase='layout', message='正在识别空间并安排视角，平面图已保存，可稍后回来查看')
                        record, assets = await asyncio.to_thread(build_interior_layout, task,
                            options['interior_snapshot'], OUTPUT, OPS / 'studio-image.json',
                            refresh=options.get('refresh_layout', False))
                        task = store.get(task_id)
                        interior_plan(task, options['interior_snapshot'], OUTPUT)
                        if task.get('interior_layout') and task['interior_layout']['id'] != record['id']:
                            task.setdefault('interior_layout_history', []).append(task['interior_layout'])
                        task['interior_layout'] = record
                        task.setdefault('edit_assets', []).extend(assets)
                        task = store.put(task, task['revision'])
                        if task['job'].get('cancel'):
                            raise ExecutionCancelled('已停止后续生成，平面图和空间识别进度已保留')
                        views = room_first_views(record['views'])
                    if options.get('interior_snapshot'):
                        options['interior_finish_scheme'] = resolve_finish_scheme(options['interior_snapshot']['style_label'])
                        options['interior_object_scheme'] = resolve_interior_object_scheme(record['analysis']['layout'],
                            options['interior_snapshot']['style_label'],
                            interior_reference_order(options['interior_snapshot'], resolved), options['interior_finish_scheme'])
                    batch, concept_ids = uuid.uuid4().hex, []
                    for index, view in enumerate(views):
                        concept_id = uuid.uuid4().hex
                        concept_ids.append(concept_id)
                        created_design_concept_ids.append(concept_id)
                        name = (view['label'] if view else
                                f'室内实景 {index + 1}' if options.get('interior_snapshot') else
                                f'空间试例 {index + 1}' if options.get('floorplan_snapshot') else f'参考设计 {index + 1}')
                        task['concepts'].append(dict(id=concept_id, name=name,
                            concept='根据本次参考图片和要求生成的新设计。', form='参考图片设计',
                            materials='以参考图和指定用途为准', rationale='参考图片不作为固定底图。',
                            risks=['效果图的细节、材质和工艺仍需核对。'], references=[], composition=True,
                            reference_design=True, design_index=index + 1, design_total=len(views),
                            design_brief={**task['brief'], 'goal': options['change']},
                            reference_snapshot=snapshots, reference_snapshot_version=2,
                            brief_revision=task.get('brief_revision', 1), batch=batch, created=stamp()))
                        if options.get('floorplan_snapshot'):
                            task['concepts'][-1]['floorplan_snapshot'] = options['floorplan_snapshot']
                            task['concepts'][-1]['risks'].append('结构底稿不是约束求解器，效果图仍需逐项复核墙体、开口和布置。')
                        recipe = {k: options[k] for k in ('change', 'edits', 'size', 'aspect', 'quality')}
                        if options.get('floorplan_snapshot'):
                            recipe['floorplan_snapshot'] = options['floorplan_snapshot']
                        if options.get('interior_snapshot'):
                            recipe['edits'] = public_edits(resolved)
                            recipe['interior_snapshot'] = options['interior_snapshot']
                            recipe['interior_finish_scheme'] = options['interior_finish_scheme']
                            recipe['interior_object_scheme'] = options['interior_object_scheme']
                            task['concepts'][-1]['interior_object_scheme_sha256'] = interior_object_digest(options['interior_object_scheme'])
                            task['concepts'][-1].update(interior_photographic=True,
                                interior_style_label=options['interior_snapshot']['style_label'],
                                concept='按原始平面图与所选参考设计的室内实景方案。',
                                rationale='平面图提供布局，其他图片按各自用途取用；生成结果仍需核对。')
                            if view:
                                binding = interior_view_binding(record, view)
                                recipe['interior_view'] = binding
                                task['concepts'][-1]['interior_view'] = binding
                                task['concepts'][-1]['form'] = view['label']
                        serialized = json.dumps({'task_id': task_id, **recipe}, ensure_ascii=False)
                        if len(serialized.encode()) > 100_000:
                            raise ValueError('这套图的设计记录较大，未提交生成，已有资料仍保留')
                        write_private(ROOT / 'private' / ('design-' + concept_id + '.json'), serialized)
                    task['job']['concept_ids'] = concept_ids
                    store.put(task, task['revision'])
            if action == 'compose':
                concept_id = uuid.uuid4().hex
                resolved = await asyncio.to_thread(resolve_edits, task, options['edits'], catalog, OUTPUT, compose=True)
                snapshots = [ref for row in resolved for ref in row['references']]
                task['concepts'].append(dict(id=concept_id, name='组合方案', concept='从参考图片中取用内容组合设计。',
                    form='多图组合', materials='以参考图和设计要求为准', rationale='使用本次选取的图片内容。',
                    risks=['组合效果、细节及工艺仍需核对。'], references=[], composition=True,
                    reference_snapshot=snapshots, reference_snapshot_version=2,
                    brief_revision=task.get('brief_revision', 1), batch=uuid.uuid4().hex, created=stamp()))
                store.put(task, task['revision'])
                concept_ids = [concept_id]
            if action in ('refine', 'export'):
                concept_ids = [source_image(task, options['image_id'])['concept_id']]
            await produce(task_id, action, concept_ids, options)
        cancelled = store.get(task_id)['job'].get('cancel')
        update_job(task_id, status='cancelled' if cancelled else 'done', message='已取消后续步骤，完成版本已保留' if cancelled else '本次操作完成', prompt_id=None)
    except ExecutionCancelled:
        job = store.get(task_id)['job']
        if job.get('cloud_pending'):
            update_job(task_id, status='uncertain', message='已停止后续步骤；云端请求结果尚未确认，请联系管理员核查，确认前不要重复提交')
        else:
            message = '已停止构思；之前的方案和图片已保留' if action == 'plan' else '已停止后续步骤，已完成版本保留'
            update_job(task_id, status='cancelled', message=message, prompt_id=None)
    except RegistrationPending as error:
        update_job(task_id, status='registration-pending', message=str(error))
    except (GenerationUncertain, CheckUncertain) as error:
        update_job(task_id, status='uncertain', message=str(error))
    except Exception as error:
        if action == 'design' and created_design_concept_ids:
            latest_task = store.get(task_id)
            if not latest_task['job'].get('items') and not any(row['concept_id'] in created_design_concept_ids for row in latest_task['images']):
                latest_task['concepts'] = [row for row in latest_task['concepts'] if row['id'] not in created_design_concept_ids]
                store.put(latest_task, latest_task['revision'])
                for created_id in created_design_concept_ids:
                    (ROOT / 'private' / ('design-' + ident(created_id) + '.json')).unlink(missing_ok=True)
        if action == 'compose' and 'concept_id' in locals():
            latest_task = store.get(task_id)
            if not any(row['concept_id'] == concept_id for row in latest_task['images']):
                latest_task['concepts'] = [row for row in latest_task['concepts'] if row['id'] != concept_id]
                store.put(latest_task, latest_task['revision'])
        job = store.get(task_id)['job']
        if job.get('cloud_pending') and (job.get('cloud_operation') == 'plan' or not isinstance(error, (ValueError, RuntimeError))):
            message = ('云端构思结果尚未确认，已有方案和图片仍保留；请联系维护人员核对本次结果'
                       if job.get('cloud_operation') == 'plan' else '云端可能已完成，但本地登记未确认；请先检查输出记录，不要重复生图')
            update_job(task_id, status='uncertain', message=message)
            return
        diagnostic = getattr(error, 'diagnostic', None)
        details = {'layout_diagnostic': diagnostic} if isinstance(diagnostic, dict) and diagnostic else {}
        update_job(task_id, status='failed', message=str(error) if isinstance(error, (ValueError, RuntimeError)) else '本地服务暂不可用，已保存进度', **details)
    finally:
        if action == 'plan' and 'job_id' in locals():
            for file in (private_file(job_id), private_file(job_id, 'result.json')):
                try:
                    file.unlink(missing_ok=True)
                except OSError:
                    pass
        workers.pop(task_id, None)


def require_revision(task, data):
    if data.get('revision') != task['revision']:
        raise ValueError('任务已更新，请刷新后重试')
    if task.get('job') and task['job']['status'] == 'running':
        raise ValueError('本任务正在运行，完成或取消后再修改')


async def reference_choices(refs):
    if not isinstance(refs, list) or len(refs) > 12:
        raise ValueError('每个任务最多选择12份资料')
    normalized = []
    for ref in refs:
        if not isinstance(ref, dict):
            raise ValueError('参考资料格式无效')
        entry = await asyncio.to_thread(catalog.get, ref.get('id'))
        if ref.get('role') not in ROLES:
            raise ValueError('参考用途无效')
        row = {'id': ref['id'], 'role': ref['role']}
        if 'use' in ref:
            if ref['use'] not in ('style', 'material', 'shape', 'object', 'pattern', 'reference'):
                raise ValueError('图像参考用途无效')
            row.update(use=ref['use'], take=clean(ref.get('take', ''), 160))
        selection = selection_value(ref.get('source_selection'))
        if selection:
            if entry.get('kind') != 'image':
                raise ValueError('局部参考必须是图片')
            proof = await asyncio.to_thread(selection_proof, Path(entry['source']), selection)
            if ref.get('source_sha256') != proof['image_sha256']:
                raise ValueError('局部参考的原图确认已失效，请重新选取局部')
            row.update(source_selection=selection, source_sha256=proof['image_sha256'])
        normalized.append(row)
    return normalized


def task_row(task):
    images = task['images']
    cover = next((image for image in reversed(images) if image['concept_id'] == task.get('selected')), images[-1] if images else None)
    saved = [image for image in images if image.get('exports')]
    if saved:
        cover = max(saved, key=lambda row: row['exports'][-1]['created'])
    job = recovery_job_view(task)
    return {**{k: task[k] for k in ('id', 'revision', 'brief', 'status', 'updated')}, 'job': job,
            'hidden': bool(task.get('hidden')), 'image_count': len(images), 'archived': len(task['archives']),
            'interior_ready': bool(task.get('interior_input')),
            'cloud_pending_count': len(unconfirmed_cloud_jobs(task)),
            'cover': {k: cover[k] for k in ('id', 'width', 'height')} if cover else None}


def task_reference_info(task):
    result = []
    for ref in task.get('references', [])[:12]:
        try:
            entry = catalog.get(ref['id'])
            result.append({'id': ref['id'], 'kind': entry.get('kind', 'unknown'),
                           'title': entry.get('title', '')})
        except (OSError, ValueError, KeyError, TypeError):
            result.append({'id': ref.get('id', '') if isinstance(ref, dict) else '', 'kind': 'missing', 'title': ''})
    return result


def library_rows():
    rows = []
    for t in store.list():
        if not t['archives'] or t.get('hidden'):
            continue
        archives = []
        for i, a in enumerate(t['archives']):
            image = next(({'id': image['id'], 'version': image['version'], 'width': image['width'], 'height': image['height'],
                           'extension': Path(image['file']).suffix.lstrip('.')}
                          for image in t['images'] if image['id'] == a.get('image_id')), None)
            row = {**a, 'index': i, 'image': image}
            row['dimension_files'] = archived_dimensions(t, a, ARCHIVE / '设计成果')
            if a.get('evidence_bundle'):
                try:
                    bundle_file(t, a, ARCHIVE / '设计成果')
                    row['evidence_state'] = 'ok'
                except (ValueError, OSError):
                    row['evidence_state'] = 'invalid'
            if a.get('image_id'):
                try:
                    archive_file(t, a, a['image_id'], VAULT, ARCHIVE / '设计成果')
                    row['image_state'] = 'ok'
                except (ValueError, OSError):
                    row['image_state'] = 'missing'
            else:
                row['legacy_images'] = [{k: r[k] for k in ('id', 'label', 'state', 'extension')}
                                        for r in archive_images(t, a, VAULT, ARCHIVE / '设计成果')]
            archives.append(row)
        rows.append({'id': t['id'], 'title': t['brief']['title'], 'category': t['brief']['category'],
                     'brief': {key: t['brief'].get(key, '') for key in ('title', 'category', 'goal', 'materials', 'setting')},
                     'archives': archives})
    return rows


def register(server):
    global store
    store = Store(ROOT)
    store.interrupted()
    store.upgrade_references(catalog)
    action_lock = asyncio.Lock()
    if getattr(server, 'app', None) is not None:
        server.app.middlewares.append(studio_entry)

    async def endpoint(request):
        try:
            require_local(request)
            if request.method == 'POST':
                upload = request.match_info.get('path', '').endswith('/upload')
                limit = MAX_UPLOAD_BYTES if upload else 2 * 1024 * 1024
                if request.headers.get('X-Zyb-Studio') != '1' or request.content_length is None or request.content_length > limit:
                    raise ValueError('请求无效')
                origin = request.headers.get('Origin')
                if origin and origin != f'{request.scheme}://{request.host}':
                    raise ValueError('不接受跨站请求')
            if request.method == 'POST' and not request.match_info.get('path', '').endswith(('/segment', '/selection')):
                async with action_lock:
                    result = await dispatch(request)
            else:
                result = await dispatch(request)
            if isinstance(result, dict) and all(key in result for key in ('id', 'revision', 'references', 'images')):
                result = {**result, 'job': recovery_job_view(result)}
                result = {**result, 'reference_info': await asyncio.to_thread(task_reference_info, result)}
            return result if isinstance(result, web.StreamResponse) else web.json_response(result, headers={'Cache-Control': 'no-store'})
        except (ValueError, KeyError, TypeError) as error:
            message = str(error) if isinstance(error, ValueError) else '请求字段不完整或类型不正确'
            return web.json_response({'error': message}, status=400)
        except OSError:
            return web.json_response({'error': '文件访问失败，请刷新资料状态'}, status=503)

    async def dispatch(request):
        path = request.match_info.get('path', '')
        if request.method == 'GET':
            if path == 'status':
                return {'version': 22.0, 'known_dimensions': True, 'floorplan_trial': True, 'photographic_interior': True,
                        'interior_layout_version': LAYOUT_VERSION,
                        'interior_style_binding': True, 'interior_coverage': True, 'interior_batch_resume': True,
                        'interior_object_identity': True, 'interior_set_download': True, 'interior_view_semantics': True,
                        'categories': CATEGORIES, 'roles': ROLES, 'upload_max_bytes': MAX_UPLOAD_BYTES,
                        'segment_ready': segmenter.ready(), 'multi_image_edit': True,
                        'active': list(workers), **planner_status(), **image_status(OPS / 'studio-image.json'),
                        **vision_status(OPS / 'studio-vision.json')}
            if path == 'tasks':
                return {'tasks': [task_row(t) for t in store.list()]}
            if path == 'library':
                return {'items': await asyncio.to_thread(library_rows)}
            if path == 'materials':
                return {'materials': await asyncio.to_thread(catalog.materials)}
            if path == 'styles':
                return await asyncio.to_thread(style_library, catalog)
            if path == 'floorplan-example':
                source = (ARCHIVE / EXAMPLE).resolve(strict=True)
                if not source.is_relative_to(ARCHIVE.resolve()):
                    raise ValueError('平面图示例路径无效')
                return web.FileResponse(source, headers={'Cache-Control': 'private, max-age=3600'})
            if path.startswith('asset-proof/'):
                _, task_id, asset_id = path.split('/')
                task = store.get(task_id)
                row = next((v for v in task.get('edit_assets', []) if v['id'] == ident(asset_id)), None)
                if row is None:
                    raise ValueError('参考图不属于当前设计')
                source = (OUTPUT / row['file']).resolve(strict=True)
                if not source.is_relative_to((OUTPUT / '产品设计' / ident(task_id)).resolve()):
                    raise ValueError('参考图路径无效')
                return {'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
            if path.startswith('dimensions/'):
                _, task_id, image_id = path.split('/')
                task = store.get(task_id)
                row = source_image(task, image_id)
                digest = await asyncio.to_thread(base_digest, task, row, OUTPUT)
                if row.get('dimensions') and row['dimensions']['source_sha256'] != digest:
                    raise ValueError('底图与已有标注不一致，旧标注已保留。请恢复原图，或将新图上传为独立版本')
                export_base = '/zyb/studio/dimension-file/' + task['id'] + '/' + row['id'] + '/'
                exports = []
                for record in row.get('dimension_exports', []):
                    links = {}
                    if re.fullmatch(r'[a-f0-9]{64}', str(record.get('document_sha256', ''))):
                        url = export_base + record['document_sha256']
                        links = {'url': url, 'metadata_url': url + '?format=json'}
                    exports.append({**record, **links})
                history = [{'document': document, 'index': index,
                            'url': '/zyb/studio/dimension-history-file/' + task['id'] + '/' + row['id'] + '/' + str(index)}
                           for index, document in enumerate(row.get('dimensions_history', []))]
                return {'source_sha256': digest, 'document': row.get('dimensions'), 'history': history,
                        'exports': exports, 'known_values': known_values(task, row)}
            if path.startswith('dimension-history-file/'):
                _, task_id, image_id, index = path.split('/')
                task = store.get(task_id)
                row = source_image(task, image_id)
                history = row.get('dimensions_history', [])
                if not re.fullmatch(r'0|[1-9][0-9]{0,8}', index) or int(index) >= len(history):
                    raise ValueError('历史尺寸记录不存在')
                receipt = {'schema': 1, 'kind': 'dimension_history', 'task_id': task['id'], 'image_id': row['id'],
                           'version': row['version'], 'document': history[int(index)],
                           'warning': '历史尺寸记录，保留当时数值与依据；不是从效果图测量，也不能代替施工或加工图纸。'}
                return web.Response(body=json.dumps(receipt, ensure_ascii=False, indent=2).encode(), content_type='application/json', headers={
                    'Cache-Control': 'private, max-age=3600',
                    'Content-Disposition': 'attachment; filename="dimension-history-' + row['id'][:12] + '-' + index + '.json"'})
            if path.startswith('dimension-file/'):
                _, task_id, image_id, document_sha = path.split('/')
                file_format = request.query.get('format', 'png')
                if file_format not in ('png', 'json'):
                    raise ValueError('尺寸标注下载格式无效')
                task = store.get(task_id)
                row = source_image(task, image_id)
                record = next((v for v in row.get('dimension_exports', []) if v['document_sha256'] == document_sha), None)
                if record is None or not re.fullmatch(r'[a-f0-9]{64}', document_sha):
                    raise ValueError('尺寸图导出记录不存在')
                folder = export_folder() / ('尺寸标注-' + ident(task_id)[:12])
                target = (folder / record['file']).resolve(strict=True)
                if not target.is_relative_to(folder.resolve()) or hashlib.sha256(target.read_bytes()).hexdigest() != record['image_sha256']:
                    raise ValueError('尺寸图文件与记录不一致')
                if file_format == 'json':
                    metadata = target.with_suffix('.json')
                    if metadata.is_symlink():
                        raise ValueError('尺寸记录文件不能是符号链接')
                    binary = metadata.read_bytes()
                    receipt = json.loads(binary)
                    digest = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                    if digest != document_sha:
                        raise ValueError('尺寸记录文件与导出记录不一致')
                    return web.Response(body=binary, content_type='application/json', headers={
                        'Cache-Control': 'private, max-age=3600',
                        'Content-Disposition': 'attachment; filename="dimensions-' + image_id[:12] + '.json"'})
                return web.FileResponse(target, headers={'Cache-Control': 'private, max-age=3600',
                                                         'Content-Disposition': 'attachment; filename="dimensions-' + image_id[:12] + '.png"'})
            if path.startswith('archive-dimension/'):
                parts = path.split('/')
                task = store.get(parts[1]) if len(parts) == 5 else None
                if task is None or not parts[2].isdigit() or int(parts[2]) >= len(task['archives']):
                    raise ValueError('保存记录不存在')
                file_format = request.query.get('format', 'png')
                try:
                    file = await asyncio.to_thread(archived_dimension_file, task, task['archives'][int(parts[2])],
                                                  parts[3], parts[4], ARCHIVE / '设计成果', file_format)
                except FileNotFoundError as error:
                    raise ValueError(str(error)) from error
                return web.FileResponse(file, headers={'Cache-Control': 'private, max-age=3600',
                    'Content-Disposition': 'attachment; filename="archived-dimensions-' + parts[3][:12] + '.' + file_format + '"'})
            if path.startswith('archive-image/'):
                parts = path.split('/')
                task = store.get(parts[1]) if len(parts) == 4 else None
                if task is None or not parts[2].isdigit() or int(parts[2]) >= len(task['archives']):
                    raise ValueError('保存记录不存在')
                file = await asyncio.to_thread(archive_file, task, task['archives'][int(parts[2])], parts[3], VAULT, ARCHIVE / '设计成果')
                return web.FileResponse(file, headers={'Cache-Control': 'private, max-age=3600'})
            if path.startswith('archive-evidence/'):
                parts = path.split('/')
                task = store.get(parts[1]) if len(parts) == 3 else None
                if task is None or not parts[2].isdigit() or int(parts[2]) >= len(task['archives']):
                    raise ValueError('存档记录不存在')
                file = await asyncio.to_thread(bundle_file, task, task['archives'][int(parts[2])], ARCHIVE / '设计成果')
                return web.FileResponse(file, headers={'Cache-Control': 'private, no-cache',
                    'Content-Disposition': 'attachment; filename="design-evidence-' + task['id'][:12] + '-'
                        + task['archives'][int(parts[2])]['evidence_bundle']['sha256'][:12] + '.zip"'})
            if path.startswith('interior-set/'):
                _, task_id, batch_id = path.split('/')
                task = store.get(task_id)
                package = await asyncio.to_thread(interior_set_zip, task, batch_id, OUTPUT)
                fallback = 'interior-' + task['id'][:8] + '-' + batch_id[:8] + '.zip'
                return web.Response(body=package['body'], content_type='application/zip', headers={
                    'Cache-Control': 'private, no-store',
                    'Content-Disposition': 'attachment; filename="' + fallback + '"; filename*=UTF-8\'\'' + quote(package['filename']),
                    'X-Zyb-Image-Count': str(package['count']), 'X-Zyb-View-Count': str(package['total'])})
            if path.startswith('tasks/'):
                return store.get(path[6:])
            if path == 'references':
                if request.query.get('selected'):
                    ids = request.query['selected'].split(',')
                    if len(ids) > 12:
                        raise ValueError('最多读取12份选中资料')
                    rows = [catalog.public(await asyncio.to_thread(catalog.get, ref_id)) for ref_id in ids]
                    return {'references': rows}
                return {'references': await asyncio.to_thread(catalog.search, request.query.get('q', ''))}
            if path.startswith('reference-image/'):
                binary = await asyncio.to_thread(thumbnail, path.split('/')[-1])
                return web.Response(body=binary, content_type='image/jpeg', headers={'Cache-Control': 'private, max-age=60'})
            if path.startswith('reference-source/'):
                entry = await asyncio.to_thread(catalog.get, path.split('/')[-1])
                source = Path(entry['source']).resolve(strict=True)
                if entry['kind'] != 'image' or not source.is_relative_to(ARCHIVE.resolve()):
                    raise ValueError('参考图片路径无效')
                return web.FileResponse(source, headers={'Cache-Control': 'private, max-age=60'})
            if path.startswith('local-result/'):
                _, task_id, job_id = path.split('/')
                task = store.get(task_id)
                if job_id not in local_recoveries(task):
                    raise ValueError('待处理结果不存在')
                _, raw = local_result(task, job_id)
                return web.FileResponse(raw, headers={'Cache-Control': 'no-store'})
            if path.startswith('registration-result/'):
                _, task_id, job_id = path.split('/')
                task = store.get(task_id)
                if job_id not in registration_recoveries(task):
                    raise ValueError('待登记结果不存在')
                _, raw = registration_result(task, job_id)
                return web.FileResponse(raw, headers={'Cache-Control': 'no-store'})
            if path.startswith(('image/', 'asset/')):
                _, task_id, image_id = path.split('/')
                task = store.get(task_id)
                pool = task.get('edit_assets', []) if path.startswith('asset/') else task['images']
                row = next((v for v in pool if v['id'] == ident(image_id)), None)
                if row is None:
                    raise ValueError('图像不存在')
                file = (OUTPUT / row['file']).resolve(strict=True)
                if not file.is_relative_to(OUTPUT.resolve()):
                    raise ValueError('图像路径无效')
                return web.FileResponse(file, headers={'Cache-Control': 'private, max-age=3600'})
            raise ValueError('接口不存在')
        parts = path.split('/')
        if len(parts) == 3 and parts[0] == 'tasks' and parts[2] == 'upload':
            task = store.get(parts[1])
            try:
                revision = int(request.query.get('revision', '0'))
            except ValueError:
                raise ValueError('任务版本无效，请刷新后重试') from None
            require_revision(task, {'revision': revision})
            if request.content_type != 'application/octet-stream':
                raise ValueError('上传格式无效')
            if not request.content_length:
                raise ValueError('请选择图片')
            binary = await request.content.readexactly(request.content_length)
            result = await asyncio.to_thread(store.import_image, task, binary, request.query.get('name', ''),
                                             request.query.get('mode', ''), OUTPUT)
            if request.query.get('mode') in ('material', 'reference'):
                return result
            concept_id = result['selected']
            source_id = next(c['source_image_id'] for c in result['concepts'] if c['id'] == concept_id)
            basis = generation_basis(result, concept_id, None, 'upload', '', ROOT / 'private')
            target = basis_file(ROOT / 'private', source_id)
            if not target.exists():
                try:
                    write_private(target, json.dumps(basis, ensure_ascii=False))
                except (OSError, ValueError) as error:
                    raise ValueError('图片已保存，但版本记录未完成。请刷新后重新上传同一图片补齐记录') from error
            return result
        raw = await request.content.readexactly(request.content_length) if request.content_length else b'{}'
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError('请求字段必须是对象')
        if path == 'tasks':
            return store.create(data)
        if path == 'library/open':
            task = store.get(data.get('task_id'))
            target = await asyncio.to_thread(archive_location, task, data.get('index'), VAULT, ARCHIVE, data.get('target'))
            args = ['/usr/bin/open', '-R', str(target)] if data['target'] == 'note' else ['/usr/bin/open', str(target)]
            result = await asyncio.to_thread(subprocess.run, args, capture_output=True, timeout=10)
            if result.returncode:
                raise ValueError('未能打开访达，请确认已登录这台Mac')
            return {'opened': True}
        if path == 'library/save':
            task = store.get(data.get('task_id'))
            index = data.get('index')
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(task['archives']):
                raise ValueError('存档记录不存在')
            image = source_image(task, data.get('image_id'))
            source = await asyncio.to_thread(archive_file, task, task['archives'][index], image['id'], VAULT, ARCHIVE / '设计成果')
            record, target = await asyncio.to_thread(save_export, task, image, ARCHIVE, export_folder(), archived=source)
            remember_export(task['id'], image['id'], record, target.parent.name)
            return {'saved': True, 'path': str(target)}
        parts = path.split('/')
        if len(parts) != 3 or parts[0] != 'tasks':
            raise ValueError('接口不存在')
        task, action = store.get(parts[1]), parts[2]
        if action == 'reference-budget':
            require_revision(task, data)
            mode = data.get('mode')
            if mode not in ('refine', 'compose', 'design'):
                raise ValueError('参考图片计数方式无效')
            values = data.get('edits', [])
            if not isinstance(values, list) or any(not isinstance(row, dict) for row in values):
                raise ValueError('图片参考格式无效')
            counts = [{key: row[key] for key in ('source', 'id', 'source_selection', 'source_sha256') if key in row}
                      for row in values]
            rows = await asyncio.to_thread(resolve_edits, task, counts, catalog, OUTPUT,
                                           compose=mode != 'refine', enforce_limit=False)
            floorplan = (source_image(task, data.get('image_id')).get('floorplan_snapshot') if mode == 'refine'
                         else task.get('floorplan') if mode == 'design' else None)
            if mode == 'design' and floorplan:
                floorplan = {**floorplan, 'guide_ids': [row['id'] for row in floorplan['guides']]}
            anchor = await asyncio.to_thread(structure_anchor, floorplan, task, OUTPUT) if floorplan else None
            anchor_path = str((OUTPUT / anchor['file']).resolve(strict=True)) if anchor else None
            interior = source_image(task, data.get('image_id')).get('floorplan_input') if mode == 'refine' else None
            layout_path = str(await asyncio.to_thread(interior_layout_reference, task, interior, OUTPUT)) if interior and interior.get('mode') == 'floorplan_photographic' else None
            budget = reference_budget(rows, base_count=int(mode == 'refine'), anchor_path=anchor_path, layout_path=layout_path)
            return {**budget, 'revision': task['revision']}
        if action in ('segment', 'selection'):
            rows = await asyncio.to_thread(resolve_edits, task, [data.get('image')], catalog, OUTPUT)
            if len(rows[0]['paths']) != 1:
                raise ValueError('请选择一张具体的参考照片')
            if action == 'segment':
                try:
                    selection = await asyncio.to_thread(segmenter.segment, rows[0]['paths'][0], data.get('points'), data.get('labels'), data.get('existing'), data.get('operation', 'add'))
                except RuntimeError:
                    raise ValueError('点选暂不可用，可以改用框选、圈选或画笔') from None
                return {'selection': selection}
            selected = selection_value(data.get('selection'))
            if not selected:
                raise ValueError('请先选择范围')
            return await asyncio.to_thread(selection_proof, rows[0]['paths'][0], selected)
        if action == 'asset-name':
            row = next((v for v in task.get('edit_assets', []) if v['id'] == ident(data.get('asset_id'))), None)
            name = clean(data.get('name'), 80)
            if not row or not name:
                raise ValueError('请填写这份设计中的参考图名称')
            row['name'] = name
            return store.put(task, task['revision'])
        if action == 'save-interior-set':
            batch = ident(data.get('batch'))
            concepts = [row for row in task['concepts'] if row.get('interior_view') and row.get('batch') == batch]
            if not concepts:
                raise ValueError('找不到这套室内视角，请刷新后重新打开')
            images = [max((image for image in task['images'] if image['concept_id'] == concept['id']),
                          key=lambda row: row['version'], default=None) for concept in concepts]
            completed = [row for row in images if row]
            if not completed:
                raise ValueError('这套图还在生成，平面图和进度已经保存')
            paths = []
            for image in completed:
                record, target = await asyncio.to_thread(save_export, task, image, OUTPUT, export_folder())
                remember_export(task['id'], image['id'], record, target.parent.name)
                paths.append(str(target))
            return {'saved': True, 'count': len(paths), 'total': len(concepts), 'path': str(Path(paths[0]).parent)}
        if action in ('save-image', 'reveal-image'):
            row = next((v for v in task['images'] if v['id'] == ident(data.get('image_id'))), None)
            if row is None:
                raise ValueError('图片版本不存在')
            if action == 'save-image':
                record, target = await asyncio.to_thread(save_export, task, row, OUTPUT, export_folder())
                remember_export(task['id'], row['id'], record, target.parent.name)
                return {'saved': True, 'path': str(target)}
            target = await asyncio.to_thread(reveal_export, task, row, export_folder())
            result = await asyncio.to_thread(subprocess.run, ['/usr/bin/open', '-R', str(target)], capture_output=True, timeout=10)
            if result.returncode:
                raise ValueError('图片已保存，但访达未能打开，请确认已登录这台Mac')
            return {'opened': True, 'path': str(target)}
        if action == 'cancel':
            job = task.get('job') or {}
            if job.get('status') == 'running':
                if job.get('operation_id'):
                    if not data.get('operation_id'):
                        raise ValueError('停止按钮已更新，请重新加载整个页面后再停止；未停止当前任务')
                    if ident(data['operation_id']) != ident(job['operation_id']):
                        raise ValueError('停止对应的处理批次已更新，请刷新后重试；未停止当前任务')
                else:
                    if data.get('revision') != task['revision']:
                        raise ValueError('任务已更新，请刷新后重试；未停止当前任务')
                if not job.get('cancel'):
                    job['cancel'] = True
                    return store.put(task, task['revision'])
            return task
        if action == 'rename':
            if data.get('revision') != task['revision']:
                raise ValueError('任务已更新，请刷新后重试')
            title = clean(data.get('title'), 150)
            if not title:
                raise ValueError('名称不能为空')
            task['brief']['title'] = title
            return store.put(task, task['revision'])
        require_revision(task, data)
        if action == 'interior-input':
            references = await reference_choices(data['references']) if 'references' in data else None
            resolved = await asyncio.to_thread(resolve_edits, task, data.get('edits', []), catalog, OUTPUT, compose=True)
            record = await asyncio.to_thread(interior_input, task, data.get('input'), resolved, OUTPUT)
            previous = task.get('interior_input')
            if previous and any(previous[key] != record[key] for key in ('source_id', 'source_sha256')):
                raise ValueError('更换风格不改动原始平面图，请另外准备新的室内设计')
            record['edits'] = public_edits(interior_reference_order(record, resolved))
            if task.get('interior_input') == record and (references is None or task['references'] == references):
                return task
            if references is not None:
                await asyncio.to_thread(preserve_references, task, catalog)
                task['references'] = references
            task['interior_input'] = record
            return store.put(task, task['revision'])
        if action == 'recover-results':
            if workers:
                raise ValueError('已有任务正在处理，请完成后再恢复登记')
            pending = registration_recoveries(task)
            if not pending:
                raise ValueError('没有待登记的结果')
            task['job'].update(action=action, status='running', message='正在恢复已有图片的登记，不会重新生图',
                               cancel=False, operation_id=uuid.uuid4().hex, result_recovery=pending, registration_error='')
            task = store.put(task, task['revision'])
            workers[task['id']] = asyncio.create_task(work(task['id'], action, {}))
            return task
        if action == 'floorplan':
            if task.get('interior_input'):
                raise ValueError('室内实景设计保留原始平面图，请另外创建矩形结构试例，不改动这份设计')
            if (task.get('job') or {}).get('status') == 'running':
                raise ValueError('正在出图，请完成后再调整平面底稿')
            changed = await asyncio.to_thread(prepare_guides, task, data, OUTPUT)
            return store.put(task, task['revision']) if changed else task
        if action == 'dimensions':
            row = source_image(task, data.get('image_id'))
            if 'expected_document' not in data:
                raise ValueError('尺寸标注界面已更新，请刷新页面后重新打开标注')
            if data['expected_document'] != row.get('dimensions'):
                raise ValueError('标注已被另一窗口更新。未保存，请关闭标注并重新打开，核对后再保存')
            changed = await asyncio.to_thread(save_dimensions, task, row, OUTPUT, data)
            return store.put(task, task['revision']) if changed else task
        if action == 'dimension-export':
            row = source_image(task, data.get('image_id'))
            if 'expected_items' in data or 'expected_source_sha256' in data:
                document = row.get('dimensions') or {}
                if (data.get('expected_source_sha256') != document.get('source_sha256')
                        or annotation_values(data.get('expected_items')) != document.get('items')):
                    raise ValueError('标注已被另一窗口更新。未导出，请关闭标注并重新打开，核对后再导出')
            target, record = await asyncio.to_thread(export_dimensions, task, row, OUTPUT, export_folder())
            records = row.setdefault('dimension_exports', [])
            if not any(v['document_sha256'] == record['document_sha256'] for v in records):
                records.append(record)
                store.put(task, task['revision'])
            url = '/zyb/studio/dimension-file/' + task['id'] + '/' + row['id'] + '/' + record['document_sha256']
            return {'saved': True, 'path': str(target), 'url': url, 'metadata_url': url + '?format=json'}
        if action == 'discard-local':
            if data.get('confirmed') is not True:
                raise ValueError('请先确认结束本次合成')
            pending = local_recoveries(task)
            if pending:
                for job_id in pending:
                    record = read_local_recovery(task, job_id)
                    task['job'].setdefault('items', {})[record['item_id']] = 'discarded'
                    task['job'].get('local_jobs', {}).pop(job_id, None)
                unknown = any(state in ('running', 'uncertain') for state in task['job'].get('items', {}).values())
                task['job'].update(local_recovery=[], status='uncertain' if unknown else 'cancelled', cloud_pending=unknown,
                                   message='本次合成已结束，生成的原始结果仍保存在本机；未确认的请求仍需核查')
            return store.put(task, task['revision'])
        if action == 'finish-local':
            if workers or (task.get('job') or {}).get('status') == 'running':
                raise ValueError('已有任务正在处理，请完成后再继续合成')
            pending = local_recoveries(task)
            if not pending:
                raise ValueError('没有待合成的图片')
            task['job'].update(action=action, status='running', message='正在处理已生成的图片', cancel=False,
                               operation_id=uuid.uuid4().hex, local_recovery=pending)
            task = store.put(task, task['revision'])
            workers[task['id']] = asyncio.create_task(work(task['id'], action, {}))
            return task
        if action == 'brief':
            brief = store.brief(data.get('brief'))
            if brief != task['brief']:
                task.setdefault('brief_history', []).append({'brief': task['brief'], 'created': stamp()})
                task['brief'] = brief
                task['brief_revision'] = task.get('brief_revision', 1) + 1
                task['selected'], task['status'] = None, '任务已更新，待重新构思'
                reset_review(task)
            task['is_demo'] = data['brief'].get('is_demo') in (True, '1')
        elif action == 'hide':
            task['hidden'] = {'at': stamp()}
        elif action == 'unhide':
            task.pop('hidden', None)
        elif action == 'references':
            normalized = await reference_choices(data.get('references', []))
            await asyncio.to_thread(preserve_references, task, catalog)
            task['references'] = normalized
        elif action == 'select':
            concept_id = ident(data['concept_id'])
            if not any(v['id'] == concept_id for v in task['concepts']):
                raise ValueError('候选方向不存在')
            task['selected'], task['status'] = concept_id, '待深化与评审'
            reset_review(task)
        elif action == 'feedback':
            decision, comment = data.get('decision'), clean(data.get('comment', ''))
            if decision not in DECISIONS or not comment:
                raise ValueError('请选择反馈结论并填写原因')
            image_id = data.get('image_id')
            image = source_image(task, image_id) if image_id else None
            feedback = {'created': stamp(), 'concept_id': image['concept_id'] if image else task.get('selected'),
                        'decision': decision, 'comment': comment}
            if image:
                feedback['image_id'] = image['id']
                review = review_for_image(task, image['id'])
                if review.get('reviewer') and review.get('design') in ('通过', '不通过'):
                    feedback['review_snapshot'] = dict(review)
            task['feedback'].append(feedback)
        elif action == 'review':
            reviewer = clean(data.get('reviewer', ''), 100)
            if not reviewer:
                raise ValueError('请填写评审人或整理人')
            values = {k: data.get(k, '待核验') for k in ('design', 'culture', 'process')}
            if any(v not in CHECKS for v in values.values()):
                raise ValueError('评审状态无效')
            image_id = data.get('image_id')
            image = None
            if image_id is not None:
                image = source_image(task, image_id)
            record = {'reviewer': reviewer, **values, **({'image_id': image_id} if image_id else {})}
            if image and (image.get('floorplan_snapshot') or image.get('floorplan_input', {}).get('mode') == 'floorplan_photographic'):
                floor_checks = {key: data.get('floor_' + key, '待核验') for key, _ in FLOORPLAN_CHECKS}
                if any(value not in CHECKS for value in floor_checks.values()):
                    raise ValueError('平面核对状态无效')
                record['floorplan_checks'] = floor_checks
            save_review(task, record)
        elif action == 'archive':
            image_id = data.get('image_id') or task['review'].get('image_id')
            if image_id:
                if task['review'].get('image_id') != image_id:
                    raise ValueError('请先保存这个图片版本的检查记录，再存档')
                task['selected'] = source_image(task, image_id)['concept_id']
            archived = await asyncio.to_thread(archive_note, task, catalog, OUTPUT, ARCHIVE / '设计成果', include_manifest=True, private_root=ROOT / 'private')
            note = archived['note']
            if not any(row['note'] == note for row in task['archives']):
                task['archives'].append({'note': note, 'created': stamp(), 'concept_id': task['selected'],
                                         'image_id': task['review'].get('image_id') or next(v['id'] for v in reversed(task['images']) if v['concept_id'] == task['selected']),
                                         'image_ids': [v['id'] for v in task['images'] if v['concept_id'] == task['selected']],
                                         **{key: archived[key] for key in ('dimension_schema', 'dimension_files', 'image_schema', 'image_files', 'evidence_bundle')}})
            task['status'] = '已归档概念版本'
        elif action == 'check':
            if registration_recoveries(task):
                raise ValueError('请先恢复已保存图片的登记，再核对结果')
            if local_recoveries(task):
                raise ValueError('请先处理待合成的图片，再核对结果')
            if workers:
                raise ValueError('已有设计任务运行，完成后再核对')
            if data.get('vision_consent') is not True:
                raise ValueError('请通过核对按钮提交本次检查')
            if unconfirmed_cloud_jobs(task) and data.get('acknowledge_uncertain') is not True:
                raise ValueError('上次云端请求未确认，请先核对记录后再确认新调用')
            vision_config(OPS / 'studio-vision.json')
            image_row(task, data.get('image_id'))
            options = {'image_id': data['image_id'], 'standard': clean(data.get('standard', ''))}
            operation_id = uuid.uuid4().hex
            retain_cloud_job(task, action, operation_id, data.get('acknowledge_uncertain') is True)
            task['job'] = {'action': action, 'status': 'running', 'message': '等待视觉辅助核对', 'started': stamp(), 'cancel': False,
                           'operation_id': operation_id}
            task = store.put(task, task['revision'])
            workers[task['id']] = asyncio.create_task(work(task['id'], action, options))
            return task
        elif action in ('plan', 'generate', 'refine', 'export', 'compose', 'design'):
            if action == 'design' and task.get('interior_input'):
                interior_unresolved_states(task, [])
            if registration_recoveries(task):
                raise ValueError('已有图片尚未登记，请先恢复已有结果，避免重复生图')
            if local_recoveries(task):
                raise ValueError('还有已生成的图片待合成，请先继续处理，避免重复生图')
            if workers:
                raise ValueError('已有设计任务运行，完成后再提交；不会并行占用内存')
            remote_plan = action == 'plan' and not planner_status()['local_only']
            if remote_plan and data.get('plan_consent') is not True:
                raise ValueError('请确认云端构思发送内容：设计要求、所选参考资料信息和同品类历史已确认的评审意见')
            if remote_plan and unconfirmed_cloud_jobs(task) and data.get('acknowledge_uncertain') is not True:
                raise ValueError('上次云端请求未确认，请先核对记录后再确认新调用')
            options = {'quality': data.get('quality', '精细'), 'size': data.get('size', '2K'), 'aspect': data.get('aspect', '1:1'),
                       'cloud_consent': data.get('cloud_consent') is True, 'cloud_consent_provider': data.get('cloud_consent_provider'),
                       'plan_consent': data.get('plan_consent') is True,
                       'render': action == 'plan' and data.get('render') is True,
                       'concept_ids': data.get('concept_ids', []), 'image_id': data.get('image_id'), 'change': clean(data.get('change', ''))}
            if action == 'design':
                if task.get('interior_input'):
                    options['interior_snapshot'] = task['interior_input']
                    options['refresh_layout'] = data.get('refresh_layout') is True
                count = data.get('count', 3)
                if type(count) is not int or not 1 <= count <= 3:
                    raise ValueError('请选择生成1、2或3个方案')
                options['count'] = count
                retry = data.get('retry_concept_id')
                resume = data.get('resume_batch_id')
                if retry is not None and resume is not None:
                    raise ValueError('请选择补齐整套图或重试一个视角')
                if resume is not None:
                    if options.get('refresh_layout'):
                        raise ValueError('补齐时沿用这套图的空间布局；重新识别请开始新一轮')
                    candidates = interior_resume_concepts(task, resume)
                    recipes = [design_recipe(task, row['id'])[1] for row in candidates]
                    first = recipes[0]
                    fields = ('change', 'edits', 'size', 'aspect', 'quality', 'interior_snapshot', 'interior_finish_scheme',
                              'interior_object_scheme')
                    if any(any(recipe.get(key) != first.get(key) for key in fields) for recipe in recipes[1:]):
                        raise ValueError('这套图的参考设置不一致，请保留已有结果并逐张处理')
                    resolved = await asyncio.to_thread(resolve_edits, task, first['edits'], catalog, OUTPUT, compose=True)
                    await asyncio.to_thread(interior_input, task, first['interior_snapshot'], resolved, OUTPUT)
                    verified_files = {}
                    for concept, recipe in zip(candidates, recipes):
                        await asyncio.to_thread(validate_frozen_design_references, task, concept, resolved, OUTPUT,
                                                recipe['edits'], verified_files=verified_files)
                        await asyncio.to_thread(interior_layout_reference, task,
                            {**recipe['interior_snapshot'], **concept['interior_view']}, OUTPUT, verified_files=verified_files)
                    options.update({key: first[key] for key in ('change', 'size', 'aspect', 'quality', 'interior_snapshot')})
                    if 'interior_finish_scheme' in first:
                        options['interior_finish_scheme'] = first['interior_finish_scheme']
                    if 'interior_object_scheme' in first:
                        options['interior_object_scheme'] = first['interior_object_scheme']
                    data = {**data, 'edits': first['edits']}
                    options.update(resume_batch_id=ident(resume), resume_concept_ids=[row['id'] for row in candidates], count=1)
                if retry is not None:
                    concept, recipe = design_recipe(task, retry)
                    if concept.get('interior_view') and interior_unresolved_states(task, [concept['id']]):
                        raise ValueError('这张图的结果尚未确认，请先恢复已有结果')
                    options.update({k: recipe[k] for k in ('change', 'size', 'aspect', 'quality')})
                    if recipe.get('floorplan_snapshot'):
                        options['floorplan_snapshot'] = recipe['floorplan_snapshot']
                    if recipe.get('interior_snapshot'):
                        options['interior_snapshot'] = recipe['interior_snapshot']
                        if 'interior_finish_scheme' in recipe:
                            options['interior_finish_scheme'] = recipe['interior_finish_scheme']
                        if 'interior_object_scheme' in recipe:
                            options['interior_object_scheme'] = recipe['interior_object_scheme']
                    else:
                        options.pop('interior_snapshot', None)
                    data = {**data, 'edits': recipe['edits']}
                    options.update(retry_concept_id=retry, count=1)
            if options['quality'] not in ('快速', '精细') or options['size'] not in ('1K', '2K', '3K', '4K') or options['aspect'] not in ('1:1', '4:3', '3:4', 'source'):
                raise ValueError('生成参数无效')
            if action == 'generate' and (not isinstance(options['concept_ids'], list) or not 1 <= len(options['concept_ids']) <= 3):
                raise ValueError('一次生成一至三个方向')
            if action == 'generate' and any(c.get('composition') and c['id'] in options['concept_ids'] for c in task['concepts']):
                raise ValueError('请打开多图组合，确认参考图后再生成新方案')
            if action in ('refine', 'export'):
                parent = source_image(task, options['image_id'])
                if action == 'refine' and parent.get('floorplan_snapshot'):
                    options['floorplan_snapshot'] = json.loads(json.dumps(parent['floorplan_snapshot']))
            if action in ('refine', 'compose', 'design') and not options['change']:
                raise ValueError('请填写本次修改内容')
            if action in ('refine', 'compose', 'design'):
                options['edits'] = data.get('edits', [])
                if action == 'design' and isinstance(options['edits'], list) and any(not isinstance(row, dict) or not row.get('use') for row in options['edits']):
                    raise ValueError('请选择每张参考图的用途')
                resolved = await asyncio.to_thread(resolve_edits, task, options['edits'], catalog, OUTPUT, compose=action in ('compose', 'design'))
                if options.get('retry_concept_id') and options.get('interior_snapshot'):
                    await asyncio.to_thread(validate_frozen_design_references, task, concept, resolved, OUTPUT, recipe['edits'])
                if options.get('interior_snapshot'):
                    await asyncio.to_thread(interior_input, task, options['interior_snapshot'], resolved, OUTPUT)
                if action == 'refine' and parent.get('floorplan_input', {}).get('mode') == 'floorplan_photographic':
                    layout_path = str(await asyncio.to_thread(interior_layout_reference, task, parent['floorplan_input'], OUTPUT))
                    if not reference_budget(resolved, base_count=1, layout_path=layout_path)['within_limit']:
                        raise ValueError('本次会保留这一版的布局依据，请减少补充图片后再生成')
                floorplan = task.get('floorplan')
                if floorplan and action == 'design' and not options.get('retry_concept_id'):
                    guide_ids = {row['id'] for row in floorplan['guides']}
                    full_guides = {row['id'] for row in resolved if row['source'] == 'upload' and row['id'] in guide_ids
                                   and row.get('use') == 'shape' and not row.get('source_selection')}
                    if full_guides != guide_ids:
                        raise ValueError('请完整选用平面复核图、结构灰模和深度底稿，将用途设为造型；可在页面恢复结构底稿')
                    if full_guides:
                        for row in floorplan['guides']:
                            source = (OUTPUT / row['file']).resolve(strict=True)
                            if (not source.is_relative_to((OUTPUT / '产品设计' / ident(task['id'])).resolve())
                                    or hashlib.sha256(source.read_bytes()).hexdigest() != row['source_sha256']):
                                raise ValueError('结构底稿已变化，请重新准备平面图')
                        options['floorplan_snapshot'] = {k: floorplan[k] for k in ('spec', 'source_id', 'source_sha256', 'fingerprint')}
                        options['floorplan_snapshot']['guide_ids'] = [row['id'] for row in floorplan['guides']]
                if options.get('floorplan_snapshot'):
                    anchor = await asyncio.to_thread(structure_anchor, options['floorplan_snapshot'], task, OUTPUT)
                    anchor_path = str((OUTPUT / anchor['file']).resolve(strict=True))
                    if action == 'refine':
                        if not reference_budget(resolved, base_count=1, anchor_path=anchor_path)['within_limit']:
                            raise ValueError('平面改图会带上本版结构灰模，请减少其他参考图片后再提交')
                options['edit_mode'] = data.get('edit_mode', 'full')
                if options['edit_mode'] not in ('full', 'local') or (action in ('compose', 'design') and options['edit_mode'] != 'full'):
                    raise ValueError('修改方式无效')
                if action == 'compose' and (len(resolved) < 2 or options['aspect'] == 'source'):
                    raise ValueError('组合新方案需至少两张参考图，并选择新图比例')
                if action == 'design' and (not resolved or options['aspect'] == 'source'):
                    raise ValueError('请添加至少一张参考图片，并选择新图比例')
                if options['edit_mode'] == 'local':
                    options['selection'] = selection_value(data.get('selection'))
                    if not options['selection']:
                        raise ValueError('请先选出要修改的范围')
                    source = source_image(task, options['image_id'])
                    path = (OUTPUT / source['file']).resolve(strict=True)
                    if not path.is_relative_to((OUTPUT / '产品设计' / ident(task['id'])).resolve()):
                        raise ValueError('图片路径无效')
                    original = await asyncio.to_thread(open_image, path)
                    approved = mask_for(options['selection'], original.size)
                    proof = await asyncio.to_thread(selection_proof, path, options['selection'])
                    submitted_proof = data.get('selection_proof')
                    if not isinstance(submitted_proof, dict) or any(submitted_proof.get(k) != proof[k] for k in ('image_sha256', 'mask_sha256')):
                        raise ValueError('底图或选区已变化，请重新确认修改范围')
                    options['selection_proof'] = proof
                    for row in resolved:
                        if row['target_selection']:
                            target = mask_for(row['target_selection'], original.size)
                            if ImageChops.subtract(target, approved).getbbox():
                                raise ValueError('参考图的放置范围超出了本次修改区，请调整选区')
                        if row['fidelity'] == 'original':
                            await asyncio.to_thread(object_placement, row, original.size, approved)
                    options['aspect'] = 'source'
                elif any(row['fidelity'] == 'original' for row in resolved):
                    raise ValueError('原样放入需要使用局部修改，并确认修改范围')
                variants = data.get('variants', [])
                if not isinstance(variants, list) or len(variants) > 3:
                    raise ValueError('一次最多比较三款材质')
                for variant in variants:
                    if not variant:
                        raise ValueError('每张对比图至少选一份材质样图')
                    await asyncio.to_thread(resolve_edits, task, variant, catalog, OUTPUT)
                if variants:
                    if action in ('compose', 'design'):
                        raise ValueError('组合新方案不使用材质比较')
                    target = comparison_target(data.get('variant_target', ''), catalog)
                    options.update(variants=variants, variant_group=uuid.uuid4().hex,
                                   change=f'将图1的{target}替换为本次材质样图所示石材，其他部分不变。')
            elif options['aspect'] == 'source':
                raise ValueError('只有修改图片时可沿用原图比例')
            if action in ('generate', 'refine', 'compose', 'design') or options['render']:
                if unconfirmed_cloud_jobs(task) and data.get('acknowledge_uncertain') is not True:
                    raise ValueError('上次云端请求状态不确定；检查输出与云端记录后再确认新调用')
                image_config = read_config(OPS / 'studio-image.json')
                if image_config['provider'] != 'ark' and (action in ('compose', 'design') or options['aspect'] == 'source' or options.get('edits') or options.get('variants')):
                    raise ValueError('当前引擎不支持图片参考或原图比例')
                if image_config['provider'] == 'ark':
                    if options['size'] not in ('1K', '2K'):
                        raise ValueError('当前云端模型仅提供原生1K、2K档位')
                    if not options['cloud_consent'] or options['cloud_consent_provider'] != image_config['provider']:
                        raise ValueError('请重新读取工作台并确认本次云端出图及参考图发送范围；未发送本次出图请求')
                    if options['render']:
                        await asyncio.to_thread(check_reference_images, catalog, task['references'])
                    elif action == 'generate':
                        for concept in task['concepts']:
                            if concept['id'] in options['concept_ids'] and not concept.get('source_image_id'):
                                await asyncio.to_thread(reference_entries, catalog, concept)
            operation_id = uuid.uuid4().hex
            retain_cloud_job(task, action, operation_id, data.get('acknowledge_uncertain') is True)
            task['job'] = {'action': action, 'status': 'running', 'message': '等待设计引擎', 'started': stamp(), 'cancel': False,
                           'operation_id': operation_id,
                           **({'concept_ids': options.get('resume_concept_ids') or
                               ([options['retry_concept_id']] if options.get('retry_concept_id') else [])} if action == 'design' else {}),
                           **({'render': True} if options['render'] else {})}
            task = store.put(task, task['revision'])
            workers[task['id']] = asyncio.create_task(work(task['id'], action, options))
            return task
        else:
            raise ValueError('操作不存在')
        return store.put(task, task['revision'])

    server.routes.get('/zyb/studio/{path:.*}')(endpoint)
    server.routes.post('/zyb/studio/{path:.*}')(endpoint)


NODES = {'ZybStudioText': ZybStudioText, 'ZybStudioImage': ZybStudioImage, 'ZybStudioPlanner': ZybStudioPlanner}
