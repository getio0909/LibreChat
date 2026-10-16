"""Version-bound, source-attributed dimensions; never measurements inferred from pixels."""
import hashlib
import io
import json
import math
import os
import re
import uuid
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .studio_store import clean, ident, stamp

SOURCES = ('实测', '图纸', '产品资料', '设计目标')
UNITS = {'mm': 1, 'cm': 10, 'm': 1000}


def known_values(task, row):
    result = []
    floorplan = row.get('floorplan_snapshot')
    if floorplan:
        spec = floorplan.get('spec', {})
        for key, name in (('width_mm', '空间净宽'), ('length_mm', '空间净长')):
            if key in spec:
                result.append(dict(name=name, value=spec[key], unit='mm', source=spec.get(key + '_kind', ''), note=spec.get(key + '_source', '来源依据未记录，需核对后使用。')))
        if spec.get('height_kind') == 'known' and 'height_mm' in spec:
            result.append(dict(name='空间层高', value=spec['height_mm'], unit='mm',
                               source=spec.get('height_mm_kind', ''), note=spec.get('height_mm_source', '来源依据未记录，需核对后使用。')))
        return result
    text = ' '.join(task.get('brief', {}).get(key, '') for key in ('dimensions', 'goal'))
    pattern = re.compile(r'(净宽度|净长度|净高度|总高度|总宽度|总长度|净宽|净长|净高|总高|总宽|总长|高度|宽度|长度|深度|厚度|高|宽|长|深|厚)(?:为|是|：|:|=)?\s*(\d+(?:\.\d+)?)\s*(毫米|厘米|米|mm|cm|m)(?![a-zA-Z0-9])')
    units = {'毫米': 'mm', '厘米': 'cm', '米': 'm'}
    seen = set()
    for match in pattern.finditer(text):
        if re.search(r'不|超过|至少|至多|小于|大于|约|大概|最大|最小|以下|以上|最高|最低|最宽|最窄|最长|最短|最深|最浅|最厚|最薄|最矮|最\s*$', text[max(0, match.start() - 5):match.start()]):
            continue
        if re.match(r'\s*(?:以下|以上|左右|上下|以内|以外|[²³]|平方|立方|[-—~至到]\s*\d)', text[match.end():]):
            continue
        label, value, unit = match.groups()
        value, unit = float(value), units.get(unit, unit)
        name = label
        if 0 < value * UNITS[unit] <= 1_000_000 and (name, value, unit) not in seen:
            seen.add((name, value, unit))
            result.append(dict(name=name, value=value, unit=unit, source='设计目标',
                               note='当前设计要求中的目标数值，可能晚于此图片；不是生成时尺寸或实测结果，需核对后使用。'))
    return result


def coordinate(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('标注位置必须在图片范围内')
    return round(value, 6)


def annotation_values(values):
    if not isinstance(values, list) or len(values) > 24:
        raise ValueError('每个版本最多保存24条尺寸标注')
    result, ids = [], set()
    for row in values:
        if not isinstance(row, dict):
            raise ValueError('尺寸标注格式无效')
        row_id = ident(row.get('id'))
        name, note = clean(row.get('name'), 60), clean(row.get('note'), 200)
        value, unit, source = row.get('value'), row.get('unit'), row.get('source')
        if not name or not note or source not in SOURCES:
            raise ValueError('请填写物件或部位名称、尺寸来源和依据')
        if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
                or unit not in UNITS or not 0 < value * UNITS[unit] <= 1_000_000):
            raise ValueError('尺寸必须是大于0的数值，且不能超过1000米')
        points = row.get('points')
        if not isinstance(points, list) or len(points) != 2 or any(not isinstance(p, list) or len(p) != 2 for p in points):
            raise ValueError('每条标注需要两个端点')
        points = [[coordinate(v) for v in point] for point in points]
        if math.dist(*points) < .003 or row_id in ids:
            raise ValueError('标注端点过近或编号重复')
        ids.add(row_id)
        result.append(dict(id=row_id, name=name, value=value, unit=unit, value_mm=round(value * UNITS[unit], 6),
                           source=source, note=note, points=points))
    return result


def base_path(task, row, output):
    path = (Path(output) / row['file']).resolve(strict=True)
    expected = (Path(output) / '产品设计' / ident(task['id'])).resolve(strict=True)
    if not path.is_relative_to(expected) or not path.is_file():
        raise ValueError('标注底图路径无效')
    return path


def base_digest(task, row, output):
    return hashlib.sha256(base_path(task, row, output).read_bytes()).hexdigest()


def save_dimensions(task, row, output, data):
    digest = base_digest(task, row, output)
    if data.get('source_sha256') != digest:
        raise ValueError('底图已变化，请重新打开尺寸标注')
    items = annotation_values(data.get('items'))
    previous = row.get('dimensions')
    if previous and previous['source_sha256'] != digest:
        raise ValueError('底图与已有标注不一致，旧标注已保留。请恢复原图，或将新图上传为独立版本')
    if previous and previous['source_sha256'] == digest and previous['items'] == items:
        return False
    if previous:
        row.setdefault('dimensions_history', []).append(previous)
    row['dimensions'] = dict(items=items, source_sha256=digest, saved=stamp(), schema=1)
    return True


def cjk_font(size):
    for name in ('/System/Library/Fonts/STHeiti Medium.ttc',
                 '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'):
        if Path(name).is_file():
            return ImageFont.truetype(name, size)
    raise ValueError('缺少中文字体，无法导出尺寸图；标注记录仍可保存')


def number_text(value):
    text = format(Decimal(str(value)), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def wrap_text(text, font, width):
    lines, line = [], ''
    for char in text:
        if char == '\n' or (line and font.getlength(line + char) > width):
            lines.append(line); line = ''
        if char != '\n':
            line += char
    lines.append(line)
    return lines


def sheet(task, row, output):
    document = row.get('dimensions')
    if not document or not document.get('items'):
        raise ValueError('请先保存至少一条尺寸标注')
    path = base_path(task, row, output)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if document['source_sha256'] != digest:
        raise ValueError('底图与标注记录不一致，未导出')
    items = annotation_values(document['items'])
    with Image.open(path) as original:
        source = ImageOps.exif_transpose(original).convert('RGB')
        source.thumbnail((2200, 1800), Image.Resampling.LANCZOS)
    width = max(1440, source.width + 128)
    margin, top = (width - source.width) // 2, 184
    image_height = max(200, source.height)
    photo_top = top + (image_height - source.height) // 2
    font = cjk_font(23)
    entries = []
    for index, item in enumerate(items, 1):
        name = f'{index:02}  {item["name"]}'
        value = f'{number_text(item["value"])} {item["unit"]}  ·  {item["source"]}'
        heading = name + '  ·  ' + value
        lines = [heading] if font.getlength(heading) <= width - 128 else wrap_text(name, font, width - 128) + [value]
        entries.append((lines, wrap_text(item['note'], font, width - 128)))
    heights = [max(88, 22 + 34 * (len(heading) + len(notes))) for heading, notes in entries]
    image = Image.new('RGB', (width, top + image_height + 138 + sum(heights)), '#f6f2e9')
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 146), fill='#182b2b')
    draw.text((64, 25), '庄严宝刹  /  已知尺寸', font=cjk_font(36), fill='#e5c583')
    draw.text((64, 82), task['brief']['title'][:28] + f'  ·  版本 {row["version"]}', font=cjk_font(25), fill='#ffffff')
    image.paste(source, (margin, photo_top))
    for index, item in enumerate(items, 1):
        points = [(margin + p[0] * source.width, photo_top + p[1] * source.height) for p in item['points']]
        a, b = points
        draw.line(points, fill='#ffffff', width=8)
        draw.line(points, fill='#986928', width=3)
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        for p in points:
            tick = (-dy / length * 10, dx / length * 10)
            draw.line([(p[0] - tick[0], p[1] - tick[1]), (p[0] + tick[0], p[1] + tick[1])], fill='#986928', width=4)
        name = f'{index:02}  {item["name"]}'
        value = f'{number_text(item["value"])} {item["unit"]}'
        text = name + '  ' + value
        label_width = min(740, width - 96)
        lines = [text] if font.getlength(text) <= label_width else wrap_text(name, font, label_width) + [value]
        label_height = len(lines) * 34 + 10
        if label_height > image_height - 16:
            lines, label_height = [f'{index:02}'], 44
        text_width = max(draw.textbbox((0, 0), line, font=font)[2] for line in lines)
        center = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
        x = max(18, min(width - text_width - 34, center[0] - text_width / 2))
        y = max(top + 8, min(top + image_height - label_height - 8, center[1] - label_height / 2))
        draw.rounded_rectangle((x - 12, y - 5, x + text_width + 12, y + label_height - 5), radius=7, fill='#182b2b')
        for offset, line in enumerate(lines):
            draw.text((x, y + offset * 34), line, font=font, fill='#ffffff')
    legend = top + image_height + 40
    draw.text((64, legend), '部位编号对应下列完整记录；不按效果图量取，非施工或加工图纸。', font=font, fill='#454d49')
    y = legend + 62
    for index, item in enumerate(items, 1):
        draw.line((64, y - 9, width - 64, y - 9), fill='#d8ceba', width=1)
        heading, notes = entries[index - 1]
        for offset, line in enumerate(heading):
            draw.text((64, y + offset * 34), line, font=font, fill='#182b2b')
        for offset, line in enumerate(notes, len(heading)):
            draw.text((64, y + offset * 34), line, font=font, fill='#666d64')
        y += heights[index - 1]
    binary = io.BytesIO()
    image.save(binary, format='PNG')
    image_bytes = binary.getvalue()
    return image_bytes, dict(schema=2, layout_version=3, title=task['brief']['title'],
                                  task_id=task['id'], image_id=row['id'], source_sha256=digest,
                                  image_sha256=hashlib.sha256(image_bytes).hexdigest(),
                                  version=row['version'], items=items, kind='known_dimensions',
                                  warning='数值来自标注依据，不是从效果图推算；不作为施工或加工图纸。')


def export_dimensions(task, row, output, root):
    binary, receipt = sheet(task, row, output)
    document_sha = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    root = Path(root)
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise ValueError('尺寸图导出目录不能包含符号链接')
    folder = root / ('尺寸标注-' + ident(task['id'])[:12])
    if folder.is_symlink():
        raise ValueError('尺寸图导出目录不能是符号链接')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / (ident(row['id']) + '-' + document_sha[:12] + '.png')
    metadata = target.with_suffix('.json')
    for path, content in ((target, binary), (metadata, json.dumps(receipt, ensure_ascii=False, indent=2).encode())):
        if path.is_symlink():
            raise ValueError('尺寸图导出路径无效')
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError('同名尺寸图内容不同，未覆盖')
            continue
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as stream:
                os.chmod(temporary, 0o600)
                stream.write(content)
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                if path.is_symlink() or path.read_bytes() != content:
                    raise ValueError('同名尺寸图已由其他操作创建，未覆盖') from error
        finally:
            temporary.unlink(missing_ok=True)
    return target, dict(file=target.name, document_sha256=document_sha, image_sha256=hashlib.sha256(binary).hexdigest(), saved=stamp())
