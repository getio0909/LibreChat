import base64
import hashlib
import io
import math
import os
import threading
import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageCms, ImageDraw, ImageFilter, ImageOps


MAX_POINTS = 4096
MODEL_REVISION = 'de431c4043854a71d8101e17995dfe596bf101a5'
MODEL_SHA256 = '48c14467e5cf9e51870511feb72c89688e82dd74523142c0538b663e193ac2a7'


def selection_value(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'shapes'} or not isinstance(value['shapes'], list):
        raise ValueError('选区无效，请重新选择')
    if not value['shapes']:
        raise ValueError('选区为空，请先选择范围')
    if len(value['shapes']) > 64:
        raise ValueError('选区过于复杂，请分次修改')
    shapes, total = [], 0
    for shape in value['shapes']:
        if not isinstance(shape, dict) or shape.get('type') not in ('rect', 'polygon', 'stroke') or shape.get('op', 'add') not in ('add', 'subtract'):
            raise ValueError('选区形状无效')
        points = shape.get('points')
        minimum = {'rect': 2, 'polygon': 3, 'stroke': 1}[shape['type']]
        if not isinstance(points, list) or not minimum <= len(points) <= (2 if shape['type'] == 'rect' else 1024):
            raise ValueError('选区点数无效')
        total += len(points)
        if total > MAX_POINTS:
            raise ValueError('选区过于复杂，请分次修改')
        for point in points:
            if not isinstance(point, list) or len(point) != 2 or any(
                    not isinstance(n, (int, float)) or isinstance(n, bool) or not math.isfinite(n) or not 0 <= n <= 999 for n in point):
                raise ValueError('选区超出图片范围')
        row = {'type': shape['type'], 'op': shape.get('op', 'add'), 'points': [[round(n, 3) for n in p] for p in points]}
        if shape['type'] == 'stroke':
            radius = shape.get('radius')
            if not isinstance(radius, (int, float)) or isinstance(radius, bool) or not math.isfinite(radius) or not 1 <= radius <= 150:
                raise ValueError('画笔尺寸无效')
            row['radius'] = radius
        shapes.append(row)
    if not any(s['op'] == 'add' for s in shapes):
        raise ValueError('请先选择要使用的区域')
    return {'shapes': shapes}


def mask_for(selection, size):
    selection = selection_value(selection)
    if not selection:
        return Image.new('L', size, 255)
    mask = Image.new('L', size, 0)
    draw = ImageDraw.Draw(mask)
    w, h = size
    for shape in selection['shapes']:
        points = [(round(x / 999 * (w - 1)), round(y / 999 * (h - 1))) for x, y in shape['points']]
        fill = 255 if shape['op'] == 'add' else 0
        if shape['type'] == 'polygon':
            draw.polygon(points, fill=fill)
        elif shape['type'] == 'rect':
            a, b = points
            draw.rectangle((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), fill=fill)
        else:
            radius = max(1, round(shape['radius'] / 999 * min(w, h)))
            draw.line(points, fill=fill, width=radius * 2, joint='curve')
            for x, y in points:
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
    if not mask.getbbox() or sum(mask.histogram()[1:]) < 16:
        raise ValueError('选区为空或过小，请重新选择')
    return mask


def open_image(path, keep_alpha=False):
    with Image.open(path) as source:
        if source.width * source.height > 40_000_000 or source.format not in ('PNG', 'JPEG', 'WEBP'):
            raise ValueError('图片格式或尺寸不支持')
        oriented = ImageOps.exif_transpose(source)
        alpha = 'A' in oriented.getbands() or 'transparency' in oriented.info
        if alpha:
            image = oriented.convert('RGBA')
            if not keep_alpha:
                image = Image.alpha_composite(Image.new('RGBA', image.size, 'white'), image).convert('RGB')
        else:
            image = oriented.convert('RGB')
        if source.info.get('icc_profile'):
            image.info['icc_profile'] = source.info['icc_profile']
        return image


def match_color(image, target=None):
    source_profile = image.info.get('icc_profile')
    if source_profile == target:
        return image
    try:
        source = ImageCms.ImageCmsProfile(io.BytesIO(source_profile)) if source_profile else ImageCms.createProfile('sRGB')
        destination = ImageCms.ImageCmsProfile(io.BytesIO(target)) if target else ImageCms.createProfile('sRGB')
        corrected = ImageCms.profileToProfile(image.convert('RGB'), source, destination, outputMode='RGB')
        if image.mode == 'RGBA':
            corrected.putalpha(image.getchannel('A'))
        return corrected
    except (ImageCms.PyCMSError, OSError, ValueError) as error:
        raise ValueError('图片颜色配置无法匹配，请转换为标准图片后重试') from error


def image_data(image):
    image = match_color(image.copy())
    image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.convert('RGB').save(buffer, format='JPEG', quality=95)
    return 'data:image/jpeg;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')


def selected_reference(path, selection):
    image = open_image(path, keep_alpha=True)
    mask = mask_for(selection, image.size)
    if image.mode == 'RGBA':
        mask = ImageChops.multiply(mask, image.getchannel('A'))
    box = mask.getbbox()
    if box is None:
        raise ValueError('所选部分完全透明，请重新选择')
    image = match_color(image)
    foreground = Image.composite(image.convert('RGB'), Image.new('RGB', image.size, (238, 238, 238)), mask)
    return image_data(foreground.crop(box))


def context_box(mask):
    x1, y1, x2, y2 = mask.getbbox()
    w, h = mask.size
    margin = max(32, round(max(x2 - x1, y2 - y1) * .25))
    x1, y1, x2, y2 = max(0, x1 - margin), max(0, y1 - margin), min(w, x2 + margin), min(h, y2 + margin)
    if (x2 - x1) / (y2 - y1) > 8 or (y2 - y1) / (x2 - x1) > 8:
        return (0, 0, w, h)
    return (x1, y1, x2, y2)


def local_context(path, selection):
    image = open_image(path)
    mask = mask_for(selection, image.size)
    box = context_box(mask)
    return image_data(image.crop(box)), box, image.size


def selection_proof(path, selection):
    image = open_image(path)
    mask = mask_for(selection, image.size)
    return {'image_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'mask_sha256': hashlib.sha256(mask.tobytes()).hexdigest(),
            'width': image.width, 'height': image.height,
            'selected_pixels': sum(mask.histogram()[1:])}


def region_in_crop(selection, size, crop):
    box = mask_for(selection, size).getbbox()
    x1, y1, x2, y2 = crop
    w, h = x2 - x1, y2 - y1
    return [max(0, min(999, round((v - origin) / extent * 999)))
            for v, origin, extent in zip(box, (x1, y1, x1, y1), (w, h, w, h))]


def save_exact(image, destination, **options):
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', **options)
    data = buffer.getvalue()
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('合成文件路径无效')
    if destination.exists():
        if destination.read_bytes() != data:
            raise ValueError('已保存的合成文件不同，未覆盖，请联系管理员核查')
        return
    temporary = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            temporary.chmod(0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def object_placement(row, size, approved):
    source = open_image(row['paths'][0], keep_alpha=True)
    source_mask = mask_for(row['source_selection'], source.size)
    if source.mode == 'RGBA':
        source_mask = ImageChops.multiply(source_mask, source.getchannel('A'))
    source_box = source_mask.getbbox()
    if source_box is None:
        raise ValueError('所选物件完全透明，请重新选择')
    target = mask_for(row['target_selection'], size)
    if ImageChops.subtract(target, approved).getbbox():
        raise ValueError('物件放置范围超出本次修改区')
    x1, y1, x2, y2 = target.getbbox()
    sw, sh = source_box[2] - source_box[0], source_box[3] - source_box[1]
    scale = min((x2 - x1) / sw, (y2 - y1) / sh)
    dimensions = max(1, round(sw * scale)), max(1, round(sh * scale))
    position = x1 + (x2 - x1 - dimensions[0]) // 2, y2 - dimensions[1]
    layer = Image.new('L', size)
    layer.paste(source_mask.crop(source_box).resize(dimensions, Image.Resampling.LANCZOS), position)
    if np.any((np.asarray(layer) > 0) & (np.asarray(approved) == 0)):
        raise ValueError('物件超出修改范围，请扩大范围后重新处理；未裁掉物件')
    return source, source_box, dimensions, position, layer


def composite_local(base_path, generated_path, selection, crop, destination, preserve=()):
    original = open_image(base_path, keep_alpha=True)
    mask = mask_for(selection, original.size)
    x1, y1, x2, y2 = crop
    generated = match_color(open_image(generated_path), original.info.get('icc_profile'))
    if abs((generated.width / generated.height) / ((x2 - x1) / (y2 - y1)) - 1) > .025:
        raise ValueError('修改图比例不匹配，结果已保留供核查，未合入原图')
    patch = generated.resize((x2 - x1, y2 - y1), Image.Resampling.LANCZOS)
    changed = original.copy()
    changed.paste(patch, (x1, y1))
    # Feather inward only: no pixel outside the approved selection may change.
    radius = max(1, min(4, round(min(original.size) / 600)))
    alpha = ImageChops.multiply(mask, mask.filter(ImageFilter.GaussianBlur(radius)))
    result = Image.composite(changed, original, alpha)
    for row in preserve:
        source, source_box, dimensions, position, layer = object_placement(row, original.size, mask)
        source = match_color(source, original.info.get('icc_profile'))
        cut = source.convert('RGB').convert(original.mode).crop(source_box).resize(dimensions, Image.Resampling.LANCZOS)
        full = result.copy()
        full.paste(cut, position)
        result = Image.composite(full, result, layer)
    outside = np.asarray(mask) == 0
    before, after = np.asarray(original), np.asarray(result)
    changed_outside = int(np.count_nonzero(np.any(before[outside] != after[outside], axis=1)))
    if changed_outside:
        raise RuntimeError('修改范围校验失败，未登记结果')
    destination = Path(destination)
    selection_path = destination.with_name('selection.png')
    save_exact(mask, selection_path)
    save_exact(result, destination, **({'icc_profile': original.info['icc_profile']} if original.info.get('icc_profile') else {}))
    saved = open_image(destination, keep_alpha=True)
    if saved.size != original.size or saved.mode != original.mode or np.any(np.asarray(saved)[outside] != before[outside]):
        destination.unlink(missing_ok=True)
        raise RuntimeError('保存后的图片未通过范围校验，未登记结果')
    ring = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(9)), mask)
    pixels = np.asarray(ring) > 0
    residual = float(np.abs(np.asarray(changed.convert('RGB'), dtype=float)[pixels] - np.asarray(original.convert('RGB'), dtype=float)[pixels]).mean()) if pixels.any() else 0
    return {'outside_changed_pixels': changed_outside, 'selected_pixels': int(np.count_nonzero(~outside)),
            'patch_upscale': round(max((x2 - x1) / generated.width, (y2 - y1) / generated.height), 3),
            'edge_residual': round(residual, 2),
            'source_sha256': hashlib.sha256(Path(base_path).read_bytes()).hexdigest(),
            'result_sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
            'selection_sha256': hashlib.sha256(selection_path.read_bytes()).hexdigest(),
            'width': original.width, 'height': original.height}


class Segmenter:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock = threading.Lock()
        self.model = self.processor = self.embedding = self.cache_key = None

    def ready(self):
        return all((self.folder / f).is_file() for f in ('config.json', 'preprocessor_config.json', 'model.safetensors'))

    def segment(self, path, points, labels, existing=None, operation='add'):
        if operation not in ('add', 'subtract'):
            raise ValueError('选区操作无效')
        if not isinstance(points, list) or not 1 <= len(points) <= 24 or not isinstance(labels, list) or len(labels) != len(points):
            raise ValueError('请选择图片中的物体')
        if any(type(label) is not int or label not in (0, 1) for label in labels) or 1 not in labels:
            raise ValueError('请先点选要保留的物体')
        selection_value({'shapes': [{'type': 'stroke', 'points': points, 'radius': 1}]})
        if not self.ready():
            raise ValueError('点选工具暂不可用，可以使用圈选或画笔')
        import cv2
        import torch
        from transformers import Sam2Model, Sam2Processor
        with self.lock:
            if self.model is None:
                if hashlib.sha256((self.folder / 'model.safetensors').read_bytes()).hexdigest() != MODEL_SHA256:
                    raise ValueError('点选工具文件校验失败，请联系管理员')
                device = 'mps' if torch.backends.mps.is_available() else 'cpu'
                self.processor = Sam2Processor.from_pretrained(self.folder, local_files_only=True)
                self.model = Sam2Model.from_pretrained(self.folder, local_files_only=True, use_safetensors=True).eval().to(device)
            image = open_image(path)
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            key = (str(path), Path(path).stat().st_mtime_ns, image.size)
            coords = [[[[x / 999 * (image.width - 1), y / 999 * (image.height - 1)] for x, y in points]]]
            inputs = self.processor(images=image, input_points=coords, input_labels=[[labels]], return_tensors='pt').to(self.model.device)
            with torch.inference_mode():
                if self.cache_key == key and self.embedding is not None:
                    inputs.pop('pixel_values', None)
                    output = self.model(**inputs, image_embeddings=self.embedding)
                else:
                    output = self.model(**inputs)
                    self.cache_key, self.embedding = key, output.image_embeddings
            masks = self.processor.post_process_masks(output.pred_masks.cpu(), inputs['original_sizes'].cpu())[0]
            index = int(output.iou_scores.reshape(-1).argmax())
            mask = masks[0, index].numpy().astype('uint8') * 255
            if existing:
                previous = np.asarray(mask_for(existing, image.size))
                mask = np.maximum(previous, mask) if operation == 'add' else np.where(mask > 0, 0, previous).astype('uint8')
            elif operation == 'subtract':
                raise ValueError('请先选择物体，再减去不需要的部分')
            contours, hierarchy = cv2.findContours(mask, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
            shapes = []
            if hierarchy is not None:
                def visit(j, depth):
                    while j != -1:
                        if cv2.contourArea(contours[j]) < 12:
                            j = int(hierarchy[0][j][0])
                            continue
                        simplified = cv2.approxPolyDP(contours[j], max(.8, cv2.arcLength(contours[j], True) * .001), True).reshape(-1, 2)
                        if len(simplified) >= 3:
                            shapes.append({'type': 'polygon', 'op': 'add' if depth % 2 == 0 else 'subtract',
                                           'points': [[round(float(x) / (image.width - 1) * 999, 3), round(float(y) / (image.height - 1) * 999, 3)] for x, y in simplified]})
                            visit(int(hierarchy[0][j][2]), depth + 1)
                        j = int(hierarchy[0][j][0])
                visit(0, 0)
            if not shapes:
                if operation == 'subtract' and existing:
                    return {'shapes': []}
                raise ValueError('没有选中清晰的物体，请换个位置或使用圈选')
            result = selection_value({'shapes': shapes})
            mask_for(result, image.size)
            return result
