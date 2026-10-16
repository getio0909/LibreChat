import base64
import binascii
import io
import json
import os
import hashlib
import math
import shutil
import time
from pathlib import Path
import urllib.error
import urllib.request

from PIL import Image, ImageOps, ImageCms


BASE_URL = 'https://ark.cn-beijing.volces.com/api/v3'
MODEL = 'doubao-seedream-5-0-pro-260628'
SIZES = {'1K': 1024, '2K': 2048, '3K': 3072, '4K': 4096}
NATIVE = {'1K': {'1:1': (1024, 1024), '4:3': (1152, 864), '3:4': (864, 1152)},
          '2K': {'1:1': (2048, 2048), '4:3': (2304, 1728), '3:4': (1728, 2304)}}
MAX_REFERENCES = 10
MAX_RESPONSE = 64 * 1024 * 1024


class GenerationUncertain(RuntimeError):
    pass


def receipt(directory, values):
    file = directory / 'receipt.json'
    temporary = directory / 'receipt.tmp'
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(values, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, file)


def read_config(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('生图配置权限必须为600')
    config = json.loads(path.read_text())
    if config.get('provider') == 'local':
        return config
    if (config.get('provider') != 'ark' or config.get('base_url') != BASE_URL
            or config.get('model') != MODEL):
        raise ValueError('生图服务或模型配置无效')
    key = Path(config.get('key_file', ''))
    if not key.is_absolute() or not key.is_file() or key.is_symlink() or key.stat().st_mode & 0o077:
        raise ValueError('请检查生图密钥文件及600权限')
    timeout = config.get('timeout_seconds', 240)
    if not isinstance(timeout, (int, float)) or not 30 <= timeout <= 600:
        raise ValueError('生图请求超时配置无效')
    return config


def image_status(config_path):
    try:
        config = read_config(config_path)
        local = config['provider'] == 'local'
        return {'image_ready': True, 'image_provider': config['provider'],
                'image_model': 'flux-2-klein' if local else MODEL,
                'image_label': '本地 FLUX' if local else 'Seedream 5.0 Pro',
                'image_sizes': ['1K', '2K', '3K', '4K'] if local else list(NATIVE),
                'image_dimensions': None if local else NATIVE,
                'image_max_references': MAX_REFERENCES}
    except (OSError, ValueError, TypeError):
        return {'image_ready': False, 'image_provider': 'unconfigured',
                'image_model': '', 'image_label': '生图未就绪'}


def reference_data(path, roots, *, linework=False):
    path = Path(path).resolve(strict=True)
    if not any(path.is_relative_to(Path(root).resolve()) for root in roots):
        raise ValueError('参考图不在允许目录')
    try:
        with Image.open(path) as source:
            if source.format not in ('JPEG', 'PNG', 'WEBP') or source.width * source.height > 40_000_000:
                raise ValueError('参考图格式或尺寸不支持')
            im = ImageOps.exif_transpose(source)
            alpha = im.convert('RGBA').getchannel('A') if 'A' in im.getbands() or 'transparency' in im.info else None
            if source.info.get('icc_profile'):
                profile = ImageCms.ImageCmsProfile(io.BytesIO(source.info['icc_profile']))
                mode = {'RGB': 'RGB', 'CMYK': 'CMYK', 'GRAY': 'L', 'LAB': 'LAB'}.get(profile.profile.xcolor_space.strip(), im.mode)
                color = im if im.mode == mode else im.convert(mode)
                im = ImageCms.profileToProfile(color, profile,
                    ImageCms.createProfile('sRGB'), outputMode='RGB')
            im = im.convert('RGBA')
            if alpha is not None:
                im.putalpha(alpha)
            im = Image.alpha_composite(Image.new('RGBA', im.size, 'white'), im).convert('RGB')
        im.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
        binary = io.BytesIO()
        im.save(binary, format='PNG' if linework else 'JPEG', **({} if linework else {'quality': 92}))
    except (OSError, Image.DecompressionBombError, ImageCms.PyCMSError):
        raise ValueError('参考图无法安全读取') from None
    return ('data:image/png;base64,' if linework else 'data:image/jpeg;base64,') + base64.b64encode(binary.getvalue()).decode('ascii')


def reference_entries(catalog, concept):
    entries, seen = [], set()
    for snapshot in concept.get('reference_snapshot', []):
        entry = catalog.resolve_snapshot(snapshot)
        if entry is None:
            raise ValueError('本方向的参考资料已失效，请恢复资料或重新构思；未提交生图')
        key = (entry['source'], json.dumps(snapshot.get('source_selection'), sort_keys=True))
        if entry['kind'] == 'image' and key not in seen:
            entries.append({**entry, 'role': snapshot['role'],
                            **{k: snapshot[k] for k in ('use', 'take', 'source_selection', 'source_sha256') if k in snapshot}})
            seen.add(key)
    if len(entries) > MAX_REFERENCES:
        raise ValueError(f'这组方案参考了 {len(entries)} 张图片，出图一次最多参考 {MAX_REFERENCES} 张。请减少参考图片后重新构思')
    return entries


def check_reference_images(catalog, references):
    sources = set()
    for ref in references:
        entry = catalog.get(ref['id'])
        if entry['kind'] == 'image':
            sources.add((entry['source'], json.dumps(ref.get('source_selection'), sort_keys=True)))
    if len(sources) > MAX_REFERENCES:
        raise ValueError(f'所选素材里有 {len(sources)} 张图片，出图一次最多参考 {MAX_REFERENCES} 张（文字资料不算）。'
                         f'请减掉 {len(sources) - MAX_REFERENCES} 张图片，或这次只出文字方案')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def source_dimensions(size, source_size):
    if size not in NATIVE or not isinstance(source_size, (tuple, list)) or len(source_size) != 2:
        raise ValueError('原图尺寸无效')
    w, h = source_size
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in (w, h)) or not 1 / 16 <= w / h <= 16:
        raise ValueError('原图比例超出支持范围，请另选标准比例')
    scale = math.sqrt(SIZES[size] ** 2 / (w * h))
    width, height = max(16, round(w * scale / 16) * 16), max(16, round(h * scale / 16) * 16)
    if not 921600 <= width * height <= 4624220 or not 1 / 16 <= width / height <= 16:
        raise ValueError('无法按原图比例生成，请另选标准比例')
    return width, height


def generate(config_path, text, references, size, aspect, destination, source_size=None):
    config = read_config(config_path)
    if config['provider'] != 'ark' or size not in NATIVE or (aspect != 'source' and aspect not in NATIVE[size]):
        raise ValueError('云端生成参数无效')
    if len(references) > MAX_REFERENCES:
        raise ValueError('参考图数量超过10张')
    destination = Path(destination)
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    if destination.is_symlink() or (destination / 'receipt.json').exists():
        raise ValueError('生成目录已存在请求记录，请检查状态后新建版本')
    if shutil.disk_usage(destination).free < 2 * MAX_RESPONSE:
        raise ValueError('磁盘剩余空间不足，未提交云端生图')
    key = Path(config['key_file']).read_text().strip()
    if not key or any(ch.isspace() for ch in key):
        raise ValueError('生图密钥格式无效')
    width, height = source_dimensions(size, source_size) if aspect == 'source' else NATIVE[size][aspect]
    body = {'model': config['model'], 'prompt': text, 'size': f'{width}x{height}',
            'response_format': 'b64_json', 'watermark': False, 'stream': False}
    if references:
        body['image'] = references[0] if len(references) == 1 else references
    request = urllib.request.Request(BASE_URL + '/images/generations',
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    record = {'state': 'pending', 'started': time.time(), 'model': MODEL,
              'requested_size': body['size'], 'reference_count': len(references),
              'reference_sha256': [hashlib.sha256(ref.encode()).hexdigest() for ref in references]}
    receipt(destination, record)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=None) as response:
            binary = response.read(MAX_RESPONSE + 1)
        if len(binary) > MAX_RESPONSE:
            raise GenerationUncertain('未收到完整图片，请先查看本次生成记录；已有图片保留')
    except urllib.error.HTTPError as error:
        record.update(state='rejected', http_status=error.code)
        receipt(destination, record)
        error.close()
        messages = {400: '生图参数或内容未被接受', 401: '火山引擎密钥无效',
                    402: '火山引擎余额不足', 403: '模型未开通或访问被拒绝',
                    429: '火山引擎请求限流'}
        if error.code >= 500:
            raise GenerationUncertain('云端服务异常，是否已生成暂不确定；先核对云端记录，未自动重试') from None
        raise RuntimeError(messages.get(error.code, '火山引擎服务暂不可用') + '；未自动重试，已有版本保留') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise GenerationUncertain('与生成服务的连接中断，结果尚未收到；请先查看本次生成记录，已有图片保留') from None
    try:
        result = json.loads(binary)
        rows = result['data']
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('error'):
            raise ValueError()
        image_bytes = base64.b64decode(rows[0]['b64_json'], validate=True)
        with Image.open(io.BytesIO(image_bytes)) as im:
            suffix = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}[im.format]
            width, height = im.size
            if width * height > 24_000_000:
                raise ValueError()
            if any(k.casefold() in ('prompt', 'workflow', 'parameters', 'description') for k in im.info):
                raise ValueError()
            im.verify()
    except (KeyError, TypeError, ValueError, OSError, binascii.Error, Image.DecompressionBombError):
        raise GenerationUncertain('未收到完整、可验证的图像；请先查看本次生成记录，未自动重试') from None
    path = destination / ('original' + suffix)
    try:
        temporary = destination / 'original.part'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(image_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        record.update(state='saved', width=width, height=height, file=path.name,
                      sha256=hashlib.sha256(image_bytes).hexdigest())
        receipt(destination, record)
    except OSError:
        raise GenerationUncertain('云端已返回图像但本地保存未确认；请检查本次输出目录，不要直接重新生图') from None
    return {'path': path, 'width': width, 'height': height, 'provider': 'ark', 'model': MODEL,
            'native_width': width, 'native_height': height, 'processing': '模型原图',
            'requested_size': body['size'],
            'reference_count': len(references), 'watermark': False}
