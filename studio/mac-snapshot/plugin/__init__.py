import hashlib
import json
import re
import time
from pathlib import Path

import numpy as np
import torch
import yaml
from PIL import Image, ImageOps

VAULT = Path('/Users/ipd/obsidian/WIKI')
ARCHIVE = Path('/Users/ipd/Archive')
AREAS = ('20-图片笔记', '40-外部参考/素材笔记')
PRESETS = Path('/Users/ipd/workspace/comfyui-ops/design-presets.json')
_catalog = {}
_catalog_time = 0.0
_catalog_categories = {}
CATEGORY_RULES = {
    '造像与雕塑': r'造像|雕塑|佛像|观音|菩萨|弥勒|释迦|立像|坐像|铜像|木雕|statue',
    '石刻浮雕与纹样': r'浮雕|石刻|石雕|纹样|纹饰|雕花|饰板|影壁|壁画|唐卡|藻井|stone.carving',
    '法器与供器': r'法器|供器|香炉|金刚杵|供灯|酥油灯|宝瓶|铜钟|木鱼|ritual',
    '家具与木作': r'家具|佛龛|供桌|供案|木作|木构|榫卯|屏风|座椅|圈椅|柜|案几|woodwork|furniture',
    '建筑与景观': r'建筑|景观|庭院|园林|牌坊|山门|佛塔|石窟|屋顶|梁架|architecture',
    '空间与展陈': r'空间|展陈|展览|展厅|展位|室内|大殿|禅堂|佛堂|展柜|exhibition|temple.space',
    '材质与纹理': r'石材|大理石|大板|材质|纹理|木纹|marble',
}
CATEGORIES = ['全部资料', *CATEGORY_RULES, '其他参考']
DESIGN_TYPES = ['通用参考设计', *CATEGORY_RULES]
REFERENCE_ROLES = ['参考形态与细节', '参考风格与配色', '参考材质与纹理', '组合两个主体']


def reference_categories(note, meta, body):
    text = note_title(note, meta, body) + ' ' + json.dumps(meta.get('subjects', []), ensure_ascii=False)
    matches = [name for name, pattern in CATEGORY_RULES.items() if re.search(pattern, text, re.I)]
    if not matches:
        topic = str(meta.get('source_group') or meta.get('topic_name') or meta.get('topic') or '')
        matches = [name for name, pattern in CATEGORY_RULES.items() if re.search(pattern, topic, re.I)]
    return matches or ['其他参考']


def contained(path, root):
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError('资料路径超出允许目录')
    if not resolved.is_file():
        raise ValueError('资料文件不存在')
    return resolved


def read_note(path):
    path = contained(path, VAULT)
    if not any(path.is_relative_to((VAULT / area).resolve()) for area in AREAS):
        raise ValueError('笔记不在设计资料目录')
    text = path.read_text(encoding='utf-8-sig')
    parts = text.split('---', 2)
    if len(parts) != 3 or parts[0].strip():
        raise ValueError('笔记缺少有效属性')
    meta = yaml.safe_load(parts[1])
    if not isinstance(meta, dict):
        raise ValueError('笔记属性无效')
    external = path.is_relative_to((VAULT / AREAS[1]).resolve())
    if external and (meta.get('effective_status') != 'available' or meta.get('status') != 'ok'):
        raise ValueError('外部资料尚未通过可用性核验')
    if meta.get('effective_status') in ('eliminated', 'missing', 'pending_review'):
        raise ValueError('资料已停用或待复核')
    raw_source = meta.get('source_path') or meta.get('file')
    if not isinstance(raw_source, str) or not Path(raw_source).is_absolute():
        raise ValueError('原图路径无效')
    source = contained(Path(raw_source), ARCHIVE)
    if source.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp'):
        raise ValueError('当前入口仅支持静态参考图片')
    return path, source, meta, parts[2]


def catalog():
    global _catalog, _catalog_time, _catalog_categories
    if _catalog and time.monotonic() - _catalog_time < 30:
        return _catalog
    candidates = {}
    for area in AREAS:
        for path in sorted((VAULT / area).rglob('*.md'), key=lambda p: (len(p.parts), str(p))):
            if any(part.startswith('.') for part in path.relative_to(VAULT).parts):
                continue
            try:
                note, source, meta, body = read_note(path)
            except (ValueError, OSError, yaml.YAMLError):
                continue
            rel = note.relative_to(VAULT)
            title = note_title(note, meta, body)
            group = str(meta.get('source_group') or meta.get('topic_name') or meta.get('topic') or note.parent.name)
            label = f'{"自有" if area == AREAS[0] else "外部"} / {group} / {title} [{hashlib.sha256(str(rel).encode()).hexdigest()[:8]}]'
            rank = (area == AREAS[0], meta.get('visual_review_status') == 'image_only', note.stat().st_mtime_ns)
            if source not in candidates or rank > candidates[source][0]:
                candidates[source] = (rank, label, str(rel), reference_categories(note, meta, body))
    ordered = sorted(candidates.values(), key=lambda item: (not item[0][0], item[1]))
    entries = {label: rel for _, label, rel, _ in ordered}
    _catalog_categories = {label: categories for _, label, _, categories in ordered}
    _catalog = entries
    _catalog_time = time.monotonic()
    return entries


def material_choices():
    return [label for label, rel in catalog().items()
            if rel.startswith(AREAS[0] + '/') and ('大板自家产品' in Path(rel).parent.name)]


def selected(label, material=False):
    entries = catalog()
    if label not in entries or (material and label not in material_choices()):
        raise ValueError('请选择列表中的有效资料')
    return read_note(VAULT / entries[label])


def note_title(note, meta, body):
    return next((line[2:].strip() for line in body.splitlines() if line.startswith('# ')),
                str(meta.get('title') or note.stem))


def visual_caption(body):
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith('> [!summary]'):
            caption = []
            for value in lines[i + 1:]:
                if not value.startswith('>'):
                    break
                caption.append(value.lstrip('> ').strip())
            return '\n'.join(caption)
    for i, line in enumerate(lines):
        if line.startswith('# '):
            paragraph = []
            for value in lines[i + 1:]:
                if not value.strip():
                    if paragraph:
                        break
                    continue
                if value.startswith(('#', '*', '[', '!', '>', '-', '```')):
                    break
                paragraph.append(value)
            return '\n'.join(paragraph)
    return ''


def note_context(note, meta, body):
    fields = {key: meta[key] for key in ('title', 'subjects', 'materials', 'style', 'colors', 'use_cases') if meta.get(key)}
    fields['title'] = note_title(note, meta, body)
    caption = visual_caption(body)
    if caption:
        fields['visual_description'] = caption
    business_text = next((meta[key] for key in ('design_prompt', 'generation_prompt', 'positive_prompt')
                          if isinstance(meta.get(key), str) and meta[key].strip()), '')
    if not business_text:
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if line.strip() in ('## 生图提示词', '## 设计提示词'):
                end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith('## ')), len(lines))
                business_text = '\n'.join(lines[i + 1:end]).strip()
                break
    return json.dumps({'note': str(note.relative_to(VAULT)), 'visual_fields': fields,
                       'design_text': business_text}, ensure_ascii=False)


class ZybObsidianReference:
    MATERIAL = False

    @classmethod
    def INPUT_TYPES(cls):
        values = material_choices() if cls.MATERIAL else list(catalog())
        options = {'tooltip': '搜索资料名称；读取Obsidian当前笔记及归档原图。分类为检索标签，并非人工鉴定。'}
        result = {'required': {'参考资料': (values, options)}}
        if not cls.MATERIAL:
            options['zyb_categories'] = {category: [i for i, label in enumerate(values) if category in _catalog_categories[label]]
                                         for category in CATEGORIES[1:]}
            result['optional'] = {'资料类别': (CATEGORIES, {'default': '全部资料'})}
        return result

    RETURN_TYPES = ('IMAGE', 'STRING', 'STRING')
    RETURN_NAMES = ('参考图片', '资料上下文', '来源核验')
    FUNCTION = 'load'
    CATEGORY = '庄严宝刹/本地资料'

    @classmethod
    def IS_CHANGED(cls, 参考资料, 资料类别='全部资料'):
        note, source, _, _ = selected(参考资料, cls.MATERIAL)
        h = hashlib.sha256(note.read_bytes())
        with source.open('rb') as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()

    def load(self, 参考资料, 资料类别='全部资料'):
        note, source, meta, body = selected(参考资料, self.MATERIAL)
        with Image.open(source) as im:
            rgb = ImageOps.exif_transpose(im).convert('RGB')
            pixels = np.array(rgb, dtype=np.float32) / 255.0
        image = torch.from_numpy(pixels).unsqueeze(0)
        evidence = '\n'.join((f'资料：{note_title(note, meta, body)}', f'笔记：{note}',
                              f'原图：{source}', f'原图尺寸：{pixels.shape[1]}×{pixels.shape[0]}',
                              f'检索范围：{资料类别}；标签：{"、".join(reference_categories(note, meta, body))}',
                              '用途：设计参考；分类不代表人工鉴定，亦非尺寸、实物或版权授权证明。'))
        return image, note_context(note, meta, body), evidence


class ZybObsidianMaterial(ZybObsidianReference):
    MATERIAL = True


class ZybDesignBrief:
    @classmethod
    def VALIDATE_INPUTS(cls, 设计要求, **kwargs):
        return True if 设计要求.strip() else '请先填写设计要求；仅查看原图请使用素材库。'

    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'设计要求': ('STRING', {'multiline': True, 'default': ''})},
                'optional': {'参考描述': ('STRING', {'forceInput': True}),
                             '材质描述': ('STRING', {'forceInput': True}),
                             '设计类型': (DESIGN_TYPES, {'default': '通用参考设计'}),
                             '第二参考用途': (REFERENCE_ROLES, {'default': '参考形态与细节'})}}

    RETURN_TYPES = ('STRING',)
    RETURN_NAMES = ('生图描述',)
    FUNCTION = 'compose'
    CATEGORY = '庄严宝刹/本地资料'

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return hashlib.sha256(PRESETS.read_bytes()).hexdigest()

    def compose(self, 设计要求, 参考描述=None, 材质描述=None, 设计类型='通用参考设计', 第二参考用途='参考形态与细节'):
        config = json.loads(PRESETS.read_text())
        primary = json.loads(参考描述) if 参考描述 else {'visual_fields': {}, 'design_text': ''}
        pieces = [config['dual_reference' if 材质描述 else 'single_reference'],
                  config['design_types'][设计类型],
                  'Reference 1 visual notes: ' + json.dumps(primary['visual_fields'], ensure_ascii=False)]
        if primary['design_text']:
            pieces.append('Design brief from reference note: ' + primary['design_text'])
        if 材质描述:
            material = json.loads(材质描述)
            pieces.append(config['reference_roles'][第二参考用途])
            pieces.append('Reference 2 visual notes: ' + json.dumps(material['visual_fields'], ensure_ascii=False))
            if material['design_text']:
                pieces.append('Reference 2 design brief: ' + material['design_text'])
        pieces.append('Requested change: ' + 设计要求)
        return ('\n'.join(pieces),)


class ZybOutputResolution:
    SIZES = {'1K': 1024, '2K': 2048, '3K': 3072, '4K': 4096}

    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {
            'image': ('IMAGE',),
            'upscaled': ('IMAGE', {'lazy': True}),
            '输出尺寸': (list(cls.SIZES), {'default': '1K',
                'tooltip': '长边1024/2048/3072/4096，保持比例。设计先生成约1MP底图；大于底图时使用AI放大，并非原生高分辨率重绘。'})}}

    RETURN_TYPES = ('IMAGE', 'INT')
    RETURN_NAMES = ('输出底图', '长边像素')
    FUNCTION = 'choose'
    CATEGORY = '庄严宝刹/输出'

    def check_lazy_status(self, image, 输出尺寸, upscaled=None):
        if max(image.shape[1:3]) < self.SIZES[输出尺寸] and upscaled is None:
            return ['upscaled']
        return []

    def choose(self, image, 输出尺寸, upscaled=None):
        size = self.SIZES[输出尺寸]
        return (upscaled if max(image.shape[1:3]) < size else image, size)


NODE_CLASS_MAPPINGS = {'ZybObsidianReference': ZybObsidianReference,
                       'ZybObsidianMaterial': ZybObsidianMaterial,
                       'ZybDesignBrief': ZybDesignBrief,
                       'ZybOutputResolution': ZybOutputResolution}
NODE_DISPLAY_NAME_MAPPINGS = {'ZybObsidianReference': 'Obsidian · 项目参考资料',
                              'ZybObsidianMaterial': 'Obsidian · 自有石材',
                              'ZybDesignBrief': '本次设计要求',
                              'ZybOutputResolution': '输出尺寸 · 保持比例'}
WEB_DIRECTORY = './web'

from .studio_routes import NODES as STUDIO_NODES, register as register_studio
from server import PromptServer

NODE_CLASS_MAPPINGS.update(STUDIO_NODES)
if getattr(PromptServer, 'instance', None) is not None:
    register_studio(PromptServer.instance)
