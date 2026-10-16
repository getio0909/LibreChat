import hashlib
import copy
import io
import json
import os
import re
import sqlite3
import time
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import yaml
from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError
if __package__:
    from .studio_styles import verified_metadata, definition as style_definition
else:
    import importlib.util
    _styles_spec = importlib.util.spec_from_file_location('studio_style_metadata', Path(__file__).with_name('studio_styles.py'))
    _styles_module = importlib.util.module_from_spec(_styles_spec)
    _styles_spec.loader.exec_module(_styles_module)
    verified_metadata = _styles_module.verified_metadata
    style_definition = _styles_module.definition

FIELDS = ('title', 'category', 'goal', 'audience', 'setting', 'materials', 'dimensions',
          'cost', 'process', 'must_have', 'must_avoid', 'culture', 'deliverable')
ROLES = ('形态', '材质', '风格', '文化依据')
CATEGORIES = ('摆件与工艺品', '法器与供器', '家具与佛龛', '雕塑与纹样', '空间与展陈', '其他设计')
DECISIONS = ('继续深化', '待客户确认', '不采用')
CHECKS = ('待核验', '通过', '不通过')
FLOORPLAN_CHECKS = (('walls', '墙体'), ('openings', '开口'), ('zones', '功能区'), ('objects', '物件数量与位置'), ('view', '视角'))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_PIXELS = 40_000_000
UPLOAD_MODES = ('scene', 'edit', 'material', 'reference')
MEASURE = r'(?:个|张|款|套|尊|件|幅|组|座|间|把)'
VERB = r'(?:设计|画|做|找|生成|出|弄|定制|制作)'
TITLE_LEAD = re.compile(r'^(?:(?:帮我|帮忙|麻烦你?|请你?|给我|我想要?|我要|想要?)+' + VERB + '?(?:一?' + MEASURE + ')?'
                        r'|' + VERB + '一?' + MEASURE + '|一' + MEASURE + ')')
TITLE_TAIL = re.compile(r'(?:的?效果图|的?照片|的?图片|的图)$')
FILE_LINK = re.compile(r'file://[^\s)>\]]+')
ARCHIVED_NAME = re.compile(r'([a-f0-9]{32})\.(png|jpe?g|webp)', re.IGNORECASE)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def review_for_image(task, image_id):
    current = task.get('review', {})
    if current.get('image_id') == image_id:
        return current
    return next((row for row in reversed(task.get('review_history', [])) if row.get('image_id') == image_id), {})


def retain_review(task):
    review = task.get('review', {})
    image_id = review.get('image_id')
    if not image_id or not any(image['id'] == image_id for image in task.get('images', [])):
        return
    fields = ('image_id', 'reviewer', 'design', 'culture', 'process', 'floorplan_checks')
    record = {key: copy.deepcopy(review.get(key, {} if key == 'floorplan_checks' else '')) for key in fields}
    history = task.setdefault('review_history', [])
    previous = next((row for row in reversed(history) if row.get('image_id') == image_id), None)
    if not previous or any(previous.get(key) != record[key] for key in fields):
        history.append({**record, 'created': stamp()})


def reset_review(task):
    retain_review(task)
    task['review'].update(design='待核验', culture='待核验', process='待核验')
    task['review'].pop('image_id', None)
    task['review'].pop('floorplan_checks', None)


def save_review(task, review):
    retain_review(task)
    task['review'] = review
    retain_review(task)


def short_title(goal):
    text = ' '.join(str(goal).split())
    head = re.split(r'[，。；;,!！？?：:、（(]', text, maxsplit=1)[0].strip()
    cjk = re.compile(r'[一-鿿]')
    if cjk.search(head):
        head = head.split(' ', 1)[0]
    head = TITLE_TAIL.sub('', TITLE_LEAD.sub('', head, count=1)).strip()
    limit = 16 if cjk.search(head or text) else 40
    return (head or text)[:limit].strip()


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise ValueError('任务或版本编号无效')
    return value


def clean(value, limit=1200):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError('字段类型或长度无效')
    return value.strip()


def md(value):
    return re.sub(r'[\[\]<>`|#*!\\]', '', str(value)).replace('\r', ' ').replace('\n', ' ')


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError('目标不能是符号链接')
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('x', encoding='utf-8') as stream:
        temp.chmod(0o600)
        stream.write(value)
    temp.replace(path)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / 'tasks.sqlite3'
        if self.db.is_symlink():
            raise ValueError('数据库路径无效')
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, revision INTEGER, updated TEXT, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS create_requests(request_id TEXT PRIMARY KEY, task_id TEXT, digest TEXT)')
        self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self):
        with self.connect() as db:
            rows = db.execute('SELECT data FROM tasks ORDER BY updated DESC').fetchall()
        return [json.loads(row[0]) for row in rows]

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM tasks WHERE id=?', (ident(task_id),)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        return json.loads(row[0])

    def put(self, task, expected):
        task = copy.deepcopy(task)
        task['revision'] = expected + 1
        task['updated'] = stamp()
        with self.connect() as db:
            changed = db.execute('UPDATE tasks SET revision=?,updated=?,data=? WHERE id=? AND revision=?',
                                 (task['revision'], task['updated'], json.dumps(task, ensure_ascii=False), task['id'], expected))
            if changed.rowcount != 1:
                raise ValueError('任务已被另一窗口更新，请刷新后重试')
        return task

    def create(self, values):
        brief = self.brief(values)
        request_id = values.get('client_request_id')
        if request_id is not None:
            ident(request_id)
        workspace = values.get('workspace', '')
        if workspace not in ('', 'reference_design'):
            raise ValueError('设计入口无效')
        digest = hashlib.sha256(json.dumps([brief, values.get('is_demo'), workspace], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        task = dict(id=uuid.uuid4().hex, revision=1, created=stamp(), updated=stamp(),
                    brief=brief, is_demo=values.get('is_demo') in (True, '1'), references=[], concepts=[], images=[], selected=None, feedback=[],
                    job=None, archives=[], status='待构思', review={'reviewer': '', 'design': '待核验',
                    'culture': '待核验', 'process': '待核验'})
        if workspace:
            task['workspace'] = workspace
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_id:
                old = db.execute('SELECT task_id,digest FROM create_requests WHERE request_id=?', (request_id,)).fetchone()
                if old:
                    if old[1] != digest:
                        raise ValueError('这次创建已提交，请先打开已保存的设计')
                    return json.loads(db.execute('SELECT data FROM tasks WHERE id=?', (old[0],)).fetchone()[0])
            db.execute('INSERT INTO tasks VALUES(?,?,?,?)', (task['id'], 1, task['updated'], json.dumps(task, ensure_ascii=False)))
            if request_id:
                db.execute('INSERT INTO create_requests VALUES(?,?,?)', (request_id, task['id'], digest))
        return task

    @staticmethod
    def brief(values):
        if not isinstance(values, dict):
            raise ValueError('任务字段必须是对象')
        brief = {k: clean(values.get(k, ''), 150 if k in ('title', 'category') else 1200) for k in FIELDS}
        brief['category'] = brief['category'] or '其他设计'
        if not brief['goal'] or brief['category'] not in CATEGORIES:
            raise ValueError('请描述设计想法并选择有效品类')
        brief['title'] = brief['title'] or short_title(brief['goal'])
        return brief

    def interrupted(self):
        for task in self.list():
            if task.get('job') and task['job']['status'] == 'running':
                unresolved = 'uncertain' in (task['job'].get('items') or {}).values()
                if task['job'].get('local_recovery'):
                    task['job'].update(status='interrupted', message='图片已保存，合成中断。可继续处理已生成的图片，不会重新生图。')
                elif task['job'].get('result_recovery'):
                    unknown = task['job'].get('cloud_pending') or any(value in ('running', 'uncertain') for value in (task['job'].get('items') or {}).values())
                    task['job'].update(status='uncertain' if unknown else 'registration-pending',
                                       message='本机已有待登记图片，可恢复已有结果。' + ('另有请求尚未确认，不会自动重试。' if unknown else '不会重新生图。'))
                elif task['job'].get('cloud_pending') or unresolved:
                    task['job'].update(status='uncertain', message='服务连接中断，尚未确认本次结果；已有内容保留，可联系维护人员核查。')
                else:
                    task['job'].update(status='interrupted', message='服务重启，已保留完成的结果；可手动重试。')
                self.put(task, task['revision'])

    def upgrade_references(self, catalog):
        changed = 0
        for task in self.list():
            if preserve_references(task, catalog):
                self.put(task, task['revision'])
                changed += 1
        return changed

    def context_feedback(self, category):
        result = []
        for task in self.list():
            if task['brief']['category'] == category and not task.get('is_demo') and not task.get('hidden'):
                for row in task['feedback'][-3:]:
                    image_id = row.get('image_id')
                    image = next((image for image in task['images'] if image['id'] == image_id), None)
                    review = row.get('review_snapshot') or review_for_image(task, image_id)
                    if (image and image['concept_id'] == row.get('concept_id') and review.get('image_id') == image_id
                            and review.get('reviewer') and review.get('design') in ('通过', '不通过')
                            and row['decision'] != '待客户确认'):
                        result.append({k: row[k] for k in ('decision', 'comment')})
        return result[:8]

    def import_image(self, task, binary, filename, mode, output):
        if mode not in UPLOAD_MODES:
            raise ValueError('图片用途无效')
        if not binary or len(binary) > MAX_UPLOAD_BYTES:
            raise ValueError('单张图片不能超过20MB')
        name = clean(filename, 250).replace('\\', '/').rsplit('/', 1)[-1]
        output = Path(output).resolve(strict=True)
        digest = hashlib.sha256(binary).hexdigest()
        asset_mode = mode in ('material', 'reference')
        pool = task.get('edit_assets', []) if asset_mode else task['images']
        existing = next((v for v in pool if v.get('source_sha256') == digest
                         and (asset_mode or v.get('upload_mode') == mode) and (output / v['file']).is_file()
                         and (output / v['file']).resolve().is_relative_to(output)), None)
        if existing:
            if asset_mode:
                return task
            task['selected'] = existing['concept_id']
            reset_review(task)
            return self.put(task, task['revision'])
        try:
            with Image.open(io.BytesIO(binary), formats=('JPEG', 'PNG', 'WEBP')) as source:
                if source.format not in ('JPEG', 'MPO', 'PNG', 'WEBP') or (source.format != 'MPO' and getattr(source, 'n_frames', 1) != 1):
                    raise ValueError('请上传单张JPG、PNG或WebP图片，不支持动图')
                source.seek(0)
                if source.width * source.height > MAX_UPLOAD_PIXELS or min(source.size) < 32:
                    raise ValueError('图片需至少32像素，且不能超过4000万像素')
                source.load()
                oriented = ImageOps.exif_transpose(source)
                has_alpha = 'A' in oriented.getbands() or 'transparency' in oriented.info
                if source.info.get('icc_profile'):
                    alpha = oriented.convert('RGBA').getchannel('A') if has_alpha else None
                    try:
                        profile = ImageCms.ImageCmsProfile(io.BytesIO(source.info['icc_profile']))
                        color_mode = {'RGB': 'RGB', 'GRAY': 'L', 'CMYK': 'CMYK'}.get(str(profile.profile.xcolor_space).strip())
                        if color_mode is None:
                            raise ValueError('Unsupported color space')
                        oriented = ImageCms.profileToProfile(oriented.convert(color_mode), profile,
                            ImageCms.createProfile('sRGB'), outputMode='RGB')
                    except (ImageCms.PyCMSError, OSError, ValueError) as error:
                        raise ValueError('图片色彩配置无法读取，请导出为sRGB图片后上传') from error
                    if alpha is not None:
                        oriented.putalpha(alpha)
                else:
                    oriented = oriented.convert('RGBA' if has_alpha else 'RGB')
                image = Image.frombytes(oriented.mode, oriented.size, oriented.tobytes())
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ValueError('无法读取这张图片，请另存为JPG或PNG后再上传') from error
        task = copy.deepcopy(task)
        image_id, concept_id = uuid.uuid4().hex, uuid.uuid4().hex
        folder = output / '产品设计' / ident(task['id']) / image_id
        for component in (folder.parent.parent, folder.parent, folder):
            if component.is_symlink():
                raise ValueError('图片保存目录无效')
        folder.mkdir(parents=True, mode=0o700)
        path = folder / 'original.png'
        if asset_mode:
            task.setdefault('edit_assets', []).append(dict(id=image_id, file=path.relative_to(output).as_posix(),
                width=image.width, height=image.height, source_name=name, source_sha256=digest,
                name=('上传材质' if mode == 'material' else '参考照片') + str(len(task.get('edit_assets', [])) + 1), upload_mode=mode, created=stamp()))
            try:
                with path.open('xb') as stream:
                    path.chmod(0o600)
                    image.save(stream, format='PNG')
                task['edit_assets'][-1]['content_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
                return self.put(task, task['revision'])
            except Exception:
                path.unlink(missing_ok=True)
                folder.rmdir()
                raise
        label = '现场照片设计' if mode == 'scene' else '上传图片修改'
        concept = dict(id=concept_id, name=label, concept=task['brief']['goal'], form='以已上传图片为基础',
                       materials=task['brief'].get('materials') or '以图片和设计要求为准',
                       rationale='直接使用上传图片，不经过文字方案构思。', references=[],
                       risks=['效果图仍需核对，不能替代尺寸测量或加工图纸。'],
                       brief_revision=task.get('brief_revision', 1), reference_snapshot=[],
                       reference_snapshot_version=2, source_image_id=image_id, upload_mode=mode,
                       batch=uuid.uuid4().hex, created=stamp())
        row = dict(id=image_id, concept_id=concept_id, parent_id=None, version=1, kind='upload',
                   file=path.relative_to(output).as_posix(), width=image.width, height=image.height,
                   quality='原图', size='2K', aspect='4:3' if image.width > image.height * 1.1 else
                   '3:4' if image.height > image.width * 1.1 else '1:1', created=stamp(),
                   source_name=name, source_sha256=digest, upload_mode=mode)
        task['concepts'].append(concept)
        task['images'].append(row)
        task['selected'], task['status'] = concept_id, '已上传图片，待设计'
        reset_review(task)
        try:
            with path.open('xb') as stream:
                path.chmod(0o600)
                image.save(stream, format='PNG')
            row['content_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            return self.put(task, task['revision'])
        except Exception:
            path.unlink(missing_ok=True)
            folder.rmdir()
            raise


def archive_location(task, index, vault, archive, target='note'):
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(task['archives']):
        raise ValueError('保存记录不存在')
    vault, archive = Path(vault).resolve(), Path(archive).resolve()
    if target == 'images':
        folder = archive / '设计成果' / ident(task['id'])
        if (archive / '设计成果').is_symlink() or folder.is_symlink() or not folder.is_dir():
            raise ValueError('归档图片目录无法定位')
        return folder
    relative = Path(task['archives'][index]['note'])
    if relative.is_absolute() or '..' in relative.parts or relative.parts[:1] != ('50-设计任务',):
        raise ValueError('保存记录路径无效')
    note = vault / relative
    if note.is_symlink() or not note.is_file() or not note.resolve().is_relative_to(vault / '50-设计任务'):
        raise ValueError('保存记录无法定位，请联系管理员检查')
    if (vault / '50-设计任务').is_symlink():
        raise ValueError('资料库目录无效')
    if target == 'note':
        return note
    raise ValueError('打开目标无效')


def archive_folder(task, archive_root):
    root = Path(archive_root)
    folder = root / ident(task['id'])
    if root.is_symlink() or folder.is_symlink():
        raise ValueError('存档图片目录无效')
    return folder


def archive_images(task, archive, vault, archive_root):
    relative = Path(str(archive.get('note', '')))
    if relative.is_absolute() or '..' in relative.parts or relative.parts[:1] != ('50-设计任务',):
        return []
    folder = Path(vault).resolve() / '50-设计任务'
    note = folder.parent / relative
    try:
        if (folder.is_symlink() or note.is_symlink() or not note.is_file()
                or not note.resolve().is_relative_to(folder) or note.stat().st_size > 262144):
            return []
        text = note.read_text(encoding='utf-8-sig')
        parts = text.split('---', 2)
        meta = yaml.safe_load(parts[1]) if len(parts) == 3 and not parts[0].strip() else None
        base = archive_folder(task, archive_root)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return []
    if not isinstance(meta, dict) or meta.get('task_id') != task['id']:
        return []
    known = {image['id'] for image in task['images']}
    lexical, resolved = os.path.normpath(base), base.resolve()
    rows, seen = [], set()
    for line in text.splitlines():
        for match in FILE_LINK.finditer(line):
            link = urlsplit(match.group(0))
            if link.netloc not in ('', 'localhost'):
                continue
            path = Path(unquote(link.path))
            name = ARCHIVED_NAME.fullmatch(path.name)
            if (not name or name.group(1) not in known or name.group(1) in seen
                    or os.path.normpath(path.parent) != lexical or path.is_symlink()):
                continue
            state = 'missing'
            if path.exists():
                if not path.is_file() or path.resolve().parent != resolved:
                    continue
                state = 'ok'
            version = re.search(r'版本\s*(\d+)', line)
            rows.append({'id': name.group(1), 'label': '版本 ' + version.group(1) if version else None, 'state': state,
                         'extension': name.group(2).lower(), 'path': path if state == 'ok' else None})
            seen.add(name.group(1))
    return rows


def archive_file(task, archive, image_id, vault, archive_root):
    ident(image_id)
    if archive.get('image_id') or archive.get('image_ids'):
        image = next((row for row in task['images'] if row['id'] == image_id), None)
        if image_id not in (archive.get('image_ids') or [archive['image_id']]):
            raise ValueError('这次存档里没有这张图片')
        folder = archive_folder(task, archive_root)
        record = None
        if archive.get('image_schema') == 1:
            record = next((row for row in archive.get('image_files', []) if row.get('image_id') == image_id), None)
            filename = (record or {}).get('file', '')
            if not record or not re.fullmatch(re.escape(image_id) + r'\.(png|jpg|jpeg|webp)', filename):
                raise ValueError('存档图片清单无效')
            path = folder / filename
        else:
            if image is None:
                raise ValueError('这次存档里没有这张图片')
            path = folder / (image_id + Path(image['file']).suffix.lower())
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.resolve().parent != folder.resolve())):
            raise ValueError('存档图片路径无效')
        if not path.exists():
            raise ValueError('这张存档图片找不到了，请联系管理员核对')
        if archive.get('image_schema') == 1:
            if hashlib.sha256(path.read_bytes()).hexdigest() != record.get('sha256'):
                raise ValueError('存档图片校验不一致，未使用当前图片替代')
        return path
    row = next((row for row in archive_images(task, archive, vault, archive_root) if row['id'] == image_id), None)
    if row is None:
        raise ValueError('这次存档里没有这张图片')
    if row['state'] != 'ok':
        raise ValueError('这张存档图片找不到了，请联系管理员核对')
    return row['path']


def archived_dimension_file(task, archive, image_id, document_sha, archive_root, file_format='png'):
    ident(image_id)
    if file_format not in ('png', 'json') or not re.fullmatch(r'[a-f0-9]{64}', str(document_sha)):
        raise ValueError('尺寸存档下载参数无效')
    record = next((row for row in archive.get('dimension_files', [])
                   if row.get('image_id') == image_id and row.get('document_sha256') == document_sha), None)
    if record is None:
        raise ValueError('这次存档里没有这份尺寸记录')
    folder = archive_folder(task, archive_root) / '尺寸标注'
    path = folder / (image_id + '-' + document_sha[:12] + '.' + file_format)
    if folder.is_symlink() or path.is_symlink():
        raise ValueError('尺寸存档文件不能是符号链接')
    if not path.is_file():
        raise FileNotFoundError('尺寸存档文件找不到了，请联系管理员核对')
    if path.resolve().parent != folder.resolve():
        raise ValueError('尺寸存档路径无效')
    binary = path.read_bytes()
    if file_format == 'png':
        if hashlib.sha256(binary).hexdigest() != record.get('image_sha256'):
            raise ValueError('尺寸存档图片与记录不一致')
    else:
        document = json.loads(binary)
        digest = hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if (not isinstance(document, dict) or digest != document_sha or document.get('task_id') != task['id'] or document.get('image_id') != image_id
                or document.get('version') != record.get('version')
                or document.get('image_sha256') != record.get('image_sha256')
                or document.get('source_sha256') != record.get('source_sha256')):
            raise ValueError('尺寸存档记录与快照不一致')
    return path


def archived_dimensions(task, archive, archive_root):
    rows = []
    for record in archive.get('dimension_files', []):
        row = {key: record[key] for key in ('image_id', 'version', 'document_sha256', 'item_count') if key in record}
        for file_format in ('png', 'json'):
            try:
                archived_dimension_file(task, archive, record.get('image_id'), record.get('document_sha256'), archive_root, file_format)
                row[file_format + '_state'] = 'ok'
            except FileNotFoundError:
                row[file_format + '_state'] = 'missing'
            except (ValueError, OSError, TypeError):
                row[file_format + '_state'] = 'invalid'
        rows.append(row)
    return rows


class Catalog:
    AREAS = ('20-图片笔记', '40-外部参考/素材笔记', '30-PDF文档')

    def __init__(self, vault, archive, materials_file=None):
        self.vault, self.archive = Path(vault), Path(archive)
        self.materials_file = Path(materials_file) if materials_file else None
        self.entries, self.scanned_at = {}, 0
        self.lock = threading.RLock()

    def scan(self, refresh=False):
        with self.lock:
            if refresh:
                self.scanned_at = 0
            return self._scan()

    def _scan(self):
        if self.scanned_at and time.monotonic() - self.scanned_at < 90:
            return self.entries
        entries = {}
        try:
            products = self.products()
        except (OSError, ValueError, TypeError):
            products = []
        for area in self.AREAS:
            for note in (self.vault / area).rglob('*.md'):
                try:
                    if (note.is_symlink() or not note.resolve().is_relative_to(self.vault.resolve())
                            or not note.resolve().is_relative_to((self.vault / area).resolve())):
                        continue
                    if any(part.startswith('.') for part in note.relative_to(self.vault).parts) or note.stat().st_size > 262144:
                        continue
                    parts = note.read_text(encoding='utf-8-sig').split('---', 2)
                except (OSError, UnicodeError):
                    continue
                if len(parts) != 3 or parts[0].strip():
                    continue
                try:
                    meta = yaml.safe_load(parts[1])
                except yaml.YAMLError:
                    continue
                if not isinstance(meta, dict) or meta.get('effective_status') in ('eliminated', 'missing', 'pending_review'):
                    continue
                if area.startswith('40-') and (meta.get('effective_status') != 'available' or meta.get('status') != 'ok'):
                    continue
                raw = meta.get('source_path') or meta.get('file')
                if not isinstance(raw, str) or not Path(raw).is_absolute():
                    continue
                source = Path(raw).resolve()
                if not source.is_file() or not source.is_relative_to(self.archive.resolve()):
                    continue
                kind = 'image' if source.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') else 'document'
                if kind == 'document' and source.suffix.lower() not in ('.pdf', '.dwg', '.mp4'):
                    continue
                title = next((s[2:].strip() for s in parts[2].splitlines() if s.startswith('# ')), str(meta.get('title') or note.stem))[:200]
                rel = note.relative_to(self.vault).as_posix()
                ref_id = hashlib.sha256(rel.encode()).hexdigest()[:24]
                fields = {k: str(meta[k])[:500] for k in ('subjects', 'materials', 'style', 'colors', 'use_cases',
                          'aliases', 'source_group', 'keywords') if k in meta}
                product = next((p for p in products if note.parent.name == p['group']), None)
                if product:
                    fields['product_name'] = product['name']
                    fields['product_aliases'] = ' '.join(product['aliases'])
                    fields['supplier'] = ' '.join(product.get('search_terms', []))
                lines = parts[2].splitlines()
                for index, line in enumerate(lines):
                    if line.startswith('> [!summary]'):
                        description = []
                        for value in lines[index + 1:]:
                            if not value.startswith('>'):
                                break
                            description.append(value.lstrip('> ').strip())
                        fields['visual_description'] = ' '.join(description)[:1400]
                        break
                if not fields.get('visual_description') and area.startswith('40-'):
                    description, after_title = [], False
                    for line in lines:
                        value = line.strip()
                        if not after_title:
                            after_title = value in ('# ' + title, '## ' + title)
                            continue
                        if not value:
                            if description:
                                break
                            continue
                        if re.match(r'[#>\[!`~<%|*-]', value) or '://' in value or '[[' in value:
                            break
                        description.append(value)
                    if description:
                        fields['visual_description'] = ' '.join(description)[:1400]
                text = title + ' ' + json.dumps(fields, ensure_ascii=False)
                origin = '收集参考' if area.startswith('40-') else '客户提供资料'
                if meta.get('ai_generated') is True or meta.get('source_type') == 'ai_generated':
                    origin = 'AI生成标记'
                source_id = hashlib.sha256(source.relative_to(self.archive.resolve()).as_posix().encode()).hexdigest()
                entries[ref_id] = dict(id=ref_id, source_id=source_id, title=title, note=rel, kind=kind, fields=fields,
                                       origin=origin, rights='授权待确认', source=str(source), search=text.casefold())
                entries[ref_id].update(verified_metadata(rel, source))
        self.entries, self.scanned_at = entries, time.monotonic()
        return entries

    def get(self, ref_id):
        entry = self.scan().get(ref_id)
        if not entry:
            raise ValueError('资料已失效或不在允许目录')
        source = Path(entry['source']).resolve(strict=True)
        note = (self.vault / entry['note']).resolve(strict=True)
        if not source.is_relative_to(self.archive.resolve()) or not note.is_relative_to(self.vault.resolve()):
            raise ValueError('资料路径无效')
        if entry.get('verified_sha256') or style_definition(entry['note']):
            verified = verified_metadata(entry['note'], source)
            if not verified:
                raise ValueError('精选风格原文件已变化，请先重新核对来源')
            return {**entry, **verified}
        return entry

    def snapshot(self, ref):
        snapshot = {k: ref[k] for k in ('id', 'role', 'title', 'note', 'source_id', 'rights',
                    'credit', 'source_url', 'license_url', 'verified_sha256', 'content_sha256',
                    'use', 'take', 'source_selection', 'source_sha256') if k in ref}
        snapshot.setdefault('role', '历史用途待确认')
        try:
            entry = self.get(ref['id'])
        except (ValueError, OSError):
            return snapshot
        for key in ('title', 'note', 'source_id', 'rights'):
            snapshot.setdefault(key, entry[key])
        for key in ('credit', 'source_url', 'license_url', 'verified_sha256'):
            if key in entry:
                snapshot.setdefault(key, entry[key])
        if entry['kind'] == 'image':
            digest = hashlib.sha256(Path(entry['source']).read_bytes()).hexdigest()
            if ref.get('source_selection') and ref.get('source_sha256') and ref['source_sha256'] != digest:
                raise ValueError('局部参考图片已变化，请重新选取局部')
            snapshot.setdefault('content_sha256', digest)
        return snapshot

    def resolve_snapshot(self, ref):
        entries = self.scan()
        entry = entries.get(ref['id'])
        if entry and ref.get('source_id') and entry['source_id'] != ref['source_id']:
            entry = None
        if entry is None and ref.get('source_id'):
            matches = [row for row in entries.values() if row['source_id'] == ref['source_id']]
            if len(matches) == 1:
                entry = matches[0]
        if entry:
            try:
                current = self.get(entry['id'])
                if (ref.get('content_sha256') and
                        hashlib.sha256(Path(current['source']).read_bytes()).hexdigest() != ref['content_sha256']):
                    return None
                return current
            except (ValueError, OSError):
                pass
        return None

    @staticmethod
    def public(row):
        return {k: v for k, v in row.items() if k not in ('source', 'search')}

    def search(self, query, limit=36):
        query = clean(query, 300).casefold()
        tokens = set(re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]+', query))
        if query and not tokens:
            return []
        ranked = []
        for row in self.scan().values():
            if all(t in row['search'] for t in tokens):
                score = sum(4 if t in row['title'].casefold() else 1 for t in tokens)
                if row['fields'].get('product_name') and query:
                    score += 20
                ranked.append((score, row['id'], row))
        ranked.sort(key=lambda x: (-x[0], x[1]))
        return [self.public(row) for _, _, row in ranked[:limit]]

    def products(self):
        if self.materials_file is None or not self.materials_file.exists():
            return []
        if self.materials_file.is_symlink() or self.materials_file.stat().st_size > 65536:
            raise ValueError('石材目录配置无效')
        rows = json.loads(self.materials_file.read_text(encoding='utf-8'))
        if not isinstance(rows, list) or len(rows) > 30:
            raise ValueError('石材目录配置无效')
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or not re.fullmatch(r'[a-z0-9-]{1,50}', str(row.get('id', ''))):
                raise ValueError('石材编号无效')
            if (row['id'] in seen or not isinstance(row.get('aliases', []), list) or not isinstance(row.get('notes'), list)
                    or not isinstance(row.get('search_terms', []), list)):
                raise ValueError('石材目录配置无效')
            aliases = [clean(v, 80) for v in row.get('aliases', [])]
            notes = [clean(v, 300) for v in row['notes']]
            search_terms = [clean(v, 80) for v in row.get('search_terms', [])]
            if not 1 <= len(notes) <= 2 or len(aliases) > 12 or len(search_terms) > 12:
                raise ValueError('每款石材需配置一至两张样图')
            if any(Path(v).is_absolute() or '..' in Path(v).parts or not v.endswith('.md') for v in notes):
                raise ValueError('石材样图笔记路径无效')
            result.append(dict(id=row['id'], name=clean(row.get('name'), 80), group=clean(row.get('group'), 120),
                               aliases=aliases, notes=notes, search_terms=search_terms))
            seen.add(row['id'])
        return result

    def materials(self):
        entries = {row['note']: row for row in self.scan().values()}
        result = []
        for product in self.products():
            samples = [entries.get(note) for note in product['notes']]
            valid = all(row and row['kind'] == 'image' and Path(row['note']).parent.name == product['group'] for row in samples)
            result.append({k: product[k] for k in ('id', 'name', 'aliases')} | {
                'available': bool(valid), 'references': [self.public(row) for row in samples] if valid else []})
        return result


def preserve_references(task, catalog):
    changed = False
    for concept in task['concepts']:
        if concept.get('reference_snapshot_version') == 2:
            continue
        refs = concept.get('reference_snapshot')
        if refs is None:
            refs = [{'id': ref_id} for ref_id in concept.get('references', [])]
            concept['reference_basis_unknown'] = True
        concept['reference_snapshot'] = [catalog.snapshot(ref) for ref in refs]
        concept['reference_snapshot_version'] = 2
        changed = True
    return changed


def validate_plan(raw, references):
    if not isinstance(raw, dict) or not isinstance(raw.get('concepts'), list) or len(raw['concepts']) != 3:
        raise ValueError('方案模型未返回三个完整方向，请重试')
    allowed = {r['id'] for r in references}
    concepts, private = [], {}
    for row in raw['concepts']:
        if not isinstance(row, dict):
            raise ValueError('方案字段格式无效')
        concept = {k: clean(row.get(k, ''), 1800) for k in ('name', 'concept', 'form', 'materials', 'rationale')}
        for ref in references:
            if ref.get('title'):
                for key in concept:
                    concept[key] = concept[key].replace(ref['id'], '《' + ref['title'] + '》')
        if not all(concept.values()):
            raise ValueError('方案缺少必要设计字段，请重试')
        refs = row.get('references', [])
        if not isinstance(refs, list) or any(not isinstance(r, str) or r not in allowed for r in refs):
            raise ValueError('方案引用了无效资料，未保存')
        risks = row.get('risks', [])
        if not isinstance(risks, list):
            raise ValueError('方案风险格式无效')
        risks = [clean(x, 400) for x in risks[:8]]
        if not references:
            risks = ['未使用参考资料；产品结构、尺寸和文化细节待核验。'] + risks[:7]
        concept.update(id=uuid.uuid4().hex, references=list(dict.fromkeys(refs)), risks=risks,
                       basis='参考辅助构思' if references else '无参考构思',
                       reference_snapshot=[{'id': ref['id'], 'role': ref.get('role', '形态')} for ref in references])
        image_text = clean(row.get('image_description_en', ''), 3500)
        if len(image_text) < 40:
            raise ValueError('方案缺少完整图像定义')
        private[concept['id']] = image_text
        concepts.append(concept)
    return concepts, private


def archive_note(task, catalog, output_root, archive_root, *, include_manifest=False, private_root=None):
    from .studio_archive import evidence_bundle, immutable_file, own_file
    catalog.scan(refresh=True)
    preserve_references(task, catalog)
    selected = next((c for c in task['concepts'] if c['id'] == task.get('selected')), None)
    if not selected:
        raise ValueError('先选定方案，再归档')
    images = [i for i in task['images'] if i['concept_id'] == selected['id']]
    if not images:
        raise ValueError('选定方案还没有图像')
    review = task['review']
    if not review['reviewer']:
        raise ValueError('请记录评审人；待确认方案也需记录整理人')
    reviewed_id = review.get('image_id') or images[-1]['id']
    reviewed_image = next((image for image in images if image['id'] == reviewed_id), None)
    if reviewed_image is None:
        raise ValueError('评审版本与选定方向不一致，请重新确认图片')
    vault = catalog.vault.resolve()
    folder = vault / '50-设计任务'
    folder.mkdir(mode=0o700, exist_ok=True)
    if folder.is_symlink():
        raise ValueError('归档笔记目录不能是链接')
    archive_root = Path(archive_root)
    if archive_root.is_symlink() or not archive_root.resolve().is_relative_to(catalog.archive.resolve()):
        raise ValueError('成果根目录无效')
    artifact_dir = archive_root / task['id']
    if artifact_dir.is_symlink():
        raise ValueError('成果目录不能是符号链接')
    artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not artifact_dir.resolve().is_relative_to(Path(archive_root).resolve()):
        raise ValueError('成果目录无效')
    source_refs = list(selected['reference_snapshot'])
    uploaded_refs, image_refs = {}, {}
    for image in images:
        for edit_index, edit in enumerate(image.get('edits', [])):
            if edit.get('source') == 'upload':
                asset = next((v for v in task.get('edit_assets', []) if v['id'] == edit['id']), {})
                uploaded_refs[(image['id'], edit_index)] = dict(id=edit['id'], image_id=image['id'], name=edit.get('name', '参考照片'),
                    sha256=edit.get('source_sha256') or asset.get('source_sha256', ''), use=edit.get('use', 'reference'))
            elif edit.get('source') == 'image':
                source = next((row for row in task['images'] if row['id'] == edit['id']), None)
                image_refs[(image['id'], edit_index)] = dict(id=edit['id'], image_id=image['id'], name=edit.get('name', '历史图片'),
                    sha256=edit.get('source_sha256', ''), use=edit.get('use', 'reference'),
                    version=source['version'] if source else None, file=source['file'] if source else None)
            for ref in edit.get('references', []):
                if not any(v['id'] == ref['id'] and v.get('source_id') == ref.get('source_id') for v in source_refs):
                    source_refs.append(ref)
    resolved_refs = [(ref, catalog.resolve_snapshot(ref)) for ref in source_refs]
    archive_state = {k: task[k] for k in ('brief', 'selected', 'images', 'feedback', 'review')}
    dimension_files, dimension_artifacts = [], []
    for image in images:
        if not image.get('dimensions', {}).get('items'):
            continue
        from .studio_dimensions import sheet
        binary, receipt = sheet(task, image, output_root)
        document_sha = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        dimension_files.append(dict(image_id=image['id'], version=image['version'], document_sha256=document_sha,
                                    image_sha256=receipt['image_sha256'], source_sha256=receipt['source_sha256'],
                                    item_count=len(receipt['items'])))
        dimension_artifacts.append((image, binary, receipt, document_sha))
    validated, image_files = [], []
    for image in images:
        source = own_file(task, output_root, image)
        target = artifact_dir / (ident(image['id']) + source.suffix.lower())
        if target.is_symlink():
            raise ValueError('已有成果不能是符号链接')
        binary = source.read_bytes()
        if image.get('content_sha256') and hashlib.sha256(binary).hexdigest() != image['content_sha256']:
            raise ValueError('图片版本与登记时内容不一致，未存档')
        if target.exists() and target.read_bytes() != binary:
            raise ValueError('已有成果校验不一致')
        validated.append((image, target, binary))
        image_files.append(dict(image_id=image['id'], version=image['version'], file=target.name,
                                sha256=hashlib.sha256(binary).hexdigest(), bytes=len(binary)))
    bundle_binary, bundle_record = evidence_bundle(task, images, catalog, output_root, private_root,
        dimension_artifacts, {image['id']: (target.suffix, binary) for image, target, binary in validated})
    bundle_target = artifact_dir / ('evidence-' + bundle_record['sha256'] + '.zip')
    if bundle_target.is_symlink() or (bundle_target.exists() and bundle_target.read_bytes() != bundle_binary):
        raise ValueError('已有存档依据包校验不一致，未覆盖')
    archive_state['note_schema'] = 5
    archive_state['selected_concept'] = {k: selected[k] for k in ('id', 'name', 'concept', 'form', 'materials',
        'rationale', 'risks', 'source_image_id') if k in selected}
    archive_state['image_files'] = image_files
    archive_state['evidence_bundle'] = bundle_record
    archive_state['brief_revision'] = task.get('brief_revision', 1)
    archive_state['concept_brief_revision'] = selected.get('brief_revision')
    archive_state['images'] = [{k: v for k, v in image.items() if k not in ('favorite', 'favorite_at', 'exports')} for image in images]
    checks = [row for image in images for row in
              [next((v for v in reversed(task.get('visual_checks', [])) if v['image_id'] == image['id']), None)] if row]
    if checks:
        archive_state['visual_checks'] = checks
    archive_state.update(reference_snapshot=source_refs,
                         reference_basis_unknown=selected.get('reference_basis_unknown', False),
                         source_notes=[entry['note'] if entry else None for _, entry in resolved_refs])
    if uploaded_refs:
        archive_state['uploaded_references'] = list(uploaded_refs.values())
    if image_refs:
        archive_state['image_references'] = list(image_refs.values())
    archive_state['dimension_files'] = dimension_files
    fingerprint = hashlib.sha256(json.dumps(archive_state,
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
    title = re.sub(r'[^\w\u4e00-\u9fff -]', '', task['brief']['title'])[:60].strip() or '设计任务'
    name = title + '-' + task['id'][:8] + '-' + fingerprint
    note = folder / (name + '.md')
    if note.is_symlink():
        raise ValueError('归档笔记不能是符号链接')
    note_exists = note.exists()
    data = {'title': task['brief']['title'], 'date': stamp(), 'tags': ['设计/概念方案'],
            'task_id': task['id'], 'version': task['revision'], 'status': '概念评审记录',
            'reviewer': review['reviewer'], 'design_review': review['design'],
            'culture_review': review['culture'], 'process_review': review['process'], 'review_image_id': reviewed_id,
            'client_confirmation': '未单独记录', 'dimension_schema': 1, 'dimension_files': dimension_files,
            'image_schema': 1, 'image_files': image_files, 'evidence_bundle': bundle_record}
    body = ['---', yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip(), '---', '',
            '# ' + md(task['brief']['title']), '', '> [!warning] 概念阶段',
            '> 图像不是尺寸图、三维模型或加工文件。评审记录不替代工艺与文化核验。', '',
            '## 选定方向', '', '### ' + md(selected['name']), '', md(selected['concept']), '',
            '- 形态：' + md(selected['form']), '- 材质建议：' + md(selected['materials']),
            '- 设计依据：' + md(selected['rationale']), '', '## 待确认', '']
    if selected.get('brief_revision') and selected['brief_revision'] != task.get('brief_revision', 1):
        body += ['> [!warning] 设计要求已更新',
                 '> 选定方向按第 ' + str(selected['brief_revision']) + ' 版要求构思；当前为第 '
                 + str(task.get('brief_revision', 1)) + ' 版。下面的待确认事项是当前要求，不代表已有图片已经满足。', '']
    for key, label in [('dimensions', '尺寸'), ('cost', '成本'), ('process', '工艺'), ('culture', '文化约束')]:
        body.append('- ' + label + '：' + (md(task['brief'][key]) or '待确认'))
    body += ['- ' + md(r) for r in selected['risks']]
    body += ['', '## 关联资料', '']
    if selected.get('reference_basis_unknown'):
        body.append('- 历史来源记录不完整；不能确认为无参考构思，资料依据待核验。')
    elif selected.get('source_image_id'):
        body.append('- 以本任务上传图片为基础，原图保留在图像版本中。' + ('' if source_refs else '未引用其他资料库条目。'))
    elif not source_refs and not uploaded_refs and not image_refs:
        body.append('- 无参考构思：未引用资料；结构、尺寸及文化细节待核验。')
    for ref in uploaded_refs.values():
        role = {'material': '材质', 'shape': '造型', 'style': '风格', 'object': '物件', 'pattern': '纹样', 'reference': '按说明参考'}.get(ref['use'], '参考')
        body.append('- 上传参考：' + md(ref['name']) + '；用途：' + role + '；编号：' + md(ref['id']) + '；SHA256：' + (md(ref['sha256']) or '未记录'))
    for ref in image_refs.values():
        version = '版本 ' + str(ref['version']) if ref['version'] is not None else '历史版本待定位'
        role = {'material': '材质', 'shape': '造型', 'style': '风格', 'object': '物件', 'pattern': '纹样', 'reference': '按说明参考'}.get(ref['use'], '参考')
        body.append('- 历史图片参考：' + md(ref['name']) + '；' + version + '；用途：' + role
                    + '；编号：' + md(ref['id']) + '；SHA256：' + (md(ref['sha256']) or '未记录'))
    for ref, entry in resolved_refs:
        title = md(ref.get('title') or ('资料编号 ' + ref['id']))
        details = md(ref['role']) + '；' + md(ref.get('rights', '授权待确认'))
        for key, label in (('credit', '作者'), ('source_url', '出处'), ('license_url', '许可链接'), ('verified_sha256', '素材 SHA256')):
            if ref.get(key):
                details += '；' + label + '：' + md(ref[key])
        if entry is None:
            body.append(f'- {title} — {details}；当前资料不可定位或已停用，保留历史来源待核验。')
            continue
        link = entry['note'].removesuffix('.md')
        if any(c in link for c in ('[', ']', '|', '#', '\n')):
            body.append(f'- {title} — {details}；笔记路径含特殊字符，链接待整理。')
            continue
        body.append(f'- [[{link}|{title}]] — {details}')
    body += ['', '## 图像版本', '', '本次确认对应版本 ' + str(reviewed_image['version']) + '，其他版本保留用于比较。', '']
    reference_artifacts = []
    for ref in image_refs.values():
        if not ref['file']:
            continue
        source = (Path(output_root) / ref['file']).resolve(strict=True)
        expected = (Path(output_root) / '产品设计' / ident(task['id'])).resolve(strict=True)
        if not source.is_relative_to(expected) or not source.is_file() or source.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.webp'):
            raise ValueError('历史参考图片路径无效')
        binary = source.read_bytes()
        if ref['sha256'] and hashlib.sha256(binary).hexdigest() != ref['sha256']:
            raise ValueError('历史参考图片已变化，未存档；原记录保留')
        target = artifact_dir / (ident(ref['id']) + source.suffix.lower())
        if target.is_symlink() or (target.exists() and target.read_bytes() != binary):
            raise ValueError('历史参考图片的已有存档校验不一致')
        reference_artifacts.append((ref, target, binary))
    dimension_targets = []
    if dimension_artifacts:
        dimension_folder = artifact_dir / '尺寸标注'
        if dimension_folder.is_symlink():
            raise ValueError('尺寸存档目录不能是符号链接')
        dimension_folder.mkdir(mode=0o700, exist_ok=True)
        for image, binary, receipt, document_sha in dimension_artifacts:
            target = dimension_folder / (ident(image['id']) + '-' + document_sha[:12] + '.png')
            for path, content in ((target, binary), (target.with_suffix('.json'), json.dumps(receipt, ensure_ascii=False, indent=2).encode())):
                if path.is_symlink() or (path.exists() and path.read_bytes() != content):
                    raise ValueError('尺寸存档文件已存在且校验不一致，未覆盖')
                dimension_targets.append((path, content))
    for target, binary in dimension_targets:
        if not target.exists():
            temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary.open('xb') as handle:
                    temporary.chmod(0o600); handle.write(binary); handle.flush(); os.fsync(handle.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if target.is_symlink() or target.read_bytes() != binary:
                        raise ValueError('尺寸存档文件被其他操作修改，未覆盖')
            finally:
                temporary.unlink(missing_ok=True)
    for ref, target, binary in reference_artifacts:
        immutable_file(target, binary)
        body.append('- [历史参考 ' + md(ref['name']) + '](' + target.as_uri() + ')；版本 '
                    + str(ref['version']) + '；SHA256：' + hashlib.sha256(binary).hexdigest())
    for image, target, binary in validated:
        immutable_file(target, binary)
        details = '；上传原图（已移除隐私元数据）' if image['kind'] == 'upload' else '；' + md(image.get('model') or '历史模型未记录') + '；' + md(image.get('processing') or image['kind'])
        if image.get('reference_count') is not None:
            details += '；实际参考图' + str(image['reference_count']) + '张'
        parent = next((v for v in images if v['id'] == image.get('parent_id')), None)
        if parent:
            details += '；由版本' + str(parent['version']) + '修改'
        if image.get('edits'):
            label = '材质参考' if all(v.get('use', 'material') == 'material' for v in image['edits']) else '图片参考'
            details += '；' + label + '：' + '、'.join(md(v['name']) for v in image['edits'])
        if image.get('local_edit', {}).get('outside_changed_pixels') == 0:
            details += '；所选修改范围以外与底图像素一致（不代表内容或工艺验收通过）'
        body.append(f'- [版本 {image["version"]} · {image["width"]}×{image["height"]}]({target.as_uri()})' + details
                    + '；SHA256：' + hashlib.sha256(binary).hexdigest())
        for edit in image.get('edits', []):
            body.append('  - 参考：' + md(edit.get('name', '未命名参考')) + '；用途：'
                + md({'material': '材质', 'style': '风格', 'shape': '造型', 'object': '物件', 'pattern': '纹样'}.get(edit.get('use'), '参考'))
                + '；取用：' + (md(edit.get('take', '')) or '未单独记录')
                + '；放置：' + (md(edit.get('target', '')) or '未单独记录')
                + ('；保留局部选区，坐标见版本依据包' if edit.get('source_selection') or edit.get('target_selection') or edit.get('region') else ''))
    immutable_file(bundle_target, bundle_binary)
    body += ['', '## 版本依据包', '', '[下载独立归档 ZIP](' + bundle_target.as_uri() + ')；SHA256：' + bundle_record['sha256'],
             '', '包内包含图片版本、可定位的参考素材、参考取用与放置位置、已保存的尺寸快照，以及已记录的生成时设计要求。缺失的历史依据不会用当前要求补写；未保存生成时内容校验值的参考图仅作归档时快照。模型提示词和服务配置不在包内。', '']
    if any(bundle_record[key] for key in ('missing_input_count', 'unknown_basis_count', 'unverified_input_count', 'unverified_version_count')):
        body += ['- 依据缺口：' + str(bundle_record['missing_input_count']) + ' 条参考或结构资料不可定位，'
                 + str(bundle_record['unknown_basis_count']) + ' 个版本未记录生成时要求，'
                 + str(bundle_record['unverified_input_count']) + ' 条图片参考未保存生成时内容校验值，'
                 + str(bundle_record['unverified_version_count']) + ' 个历史版本未保存登记时内容校验值。详情见包内清单。', '']
    if dimension_files:
        body += ['', '## 已知尺寸快照', '', '以下文件绑定归档时的图片与标注；不是透视测量结果，也不作为施工或加工图纸。', '']
        for image, binary, receipt, document_sha in dimension_artifacts:
            target = artifact_dir / '尺寸标注' / (ident(image['id']) + '-' + document_sha[:12] + '.png')
            body += ['- 版本 ' + str(image['version']) + '：[' + str(len(receipt['items'])) + ' 条尺寸标注 PNG](' + target.as_uri()
                     + ') · [尺寸记录 JSON](' + target.with_suffix('.json').as_uri() + ')；底图 SHA256：' + receipt['source_sha256']]
    else:
        body += ['', '## 已知尺寸快照', '', '本次存档没有已保存的尺寸标注。', '']
    if checks:
        body += ['', '## AI视觉辅助核对', '', '结果仅供人工复核，不代表客户验收，也不替代尺寸、材料、工艺及文化核验。', '']
        for check in checks:
            image, target, binary = next(row for row in validated if row[0]['id'] == check['image_id'])
            body += ['### 图像版本 ' + str(image['version']), '',
                     '- 核对时间：' + md(check['created']), '- 核对模型：' + md(check['reported_model']),
                     '- 核对依据：' + md(check['basis'])]
            if hashlib.sha256(binary).hexdigest() != check['image_sha256']:
                body += ['- 图像校验值已变化，旧核对结果失效，需重新核对。', '']
                continue
            for row in check['checks']:
                body.append('- ' + md(row['label']) + '：' + md(row['verdict']) + ' — ' + md(row['observation']))
            body += ['', md(check['limitation']), '']
    body += ['', '## 评审与反馈', '']
    for feedback in task['feedback']:
        if feedback.get('concept_id') != selected['id']:
            continue
        if feedback.get('image_id') and feedback['image_id'] != reviewed_image['id']:
            continue
        source = '' if feedback.get('image_id') else '（历史意见，未标记图片版本）'
        body.append('- ' + md(feedback['decision']) + source + '：' + md(feedback['comment']))
    body += ['', '- 设计：' + review['design'], '- 文化：' + review['culture'], '- 工艺：' + review['process'], '']
    if reviewed_image.get('floorplan_snapshot'):
        spec = reviewed_image['floorplan_snapshot']['spec']
        body += ['## 平面试例人工核对', '', '按本版本的原图和结构底稿核对外观关系；不代表毫米精度、施工或加工验收。', '',
                 '- 净宽：' + str(spec.get('width_mm', '未记录')) + ' mm；' + md(spec.get('width_mm_kind') or '来源类型未记录') + '；' + md(spec.get('width_mm_source') or '来源依据未记录'),
                 '- 净长：' + str(spec.get('length_mm', '未记录')) + ' mm；' + md(spec.get('length_mm_kind') or '来源类型未记录') + '；' + md(spec.get('length_mm_source') or '来源依据未记录'),
                 '- 层高：' + str(spec.get('height_mm', '未记录')) + ' mm；' + ('试做假设，待确认' if spec.get('height_kind') == 'assumed' else '已知值' if spec.get('height_kind') == 'known' else '状态未记录')
                    + '；' + md(spec.get('height_mm_kind') or '来源类型未记录') + '；' + md(spec.get('height_mm_source') or '来源依据未记录')]
        for key, label in FLOORPLAN_CHECKS:
            body.append('- ' + label + '：' + review.get('floorplan_checks', {}).get(key, '待核验'))
        body.append('')
    if reviewed_image.get('floorplan_input', {}).get('mode') == 'floorplan_photographic':
        interior = reviewed_image['floorplan_input']
        body += ['## 室内实景依据与人工核对', '', '原始平面图用于空间布局参考。以下记录由人工填写，不代表尺寸实测或施工验收。', '',
                 '- 平面图编号：' + md(interior['source_id']), '- 平面图校验值：' + md(interior['source_sha256']),
                 '- 设计风格：' + md(interior['style_label']), '- 原始平面图的保存状态见存档依据包。']
        for key, label in FLOORPLAN_CHECKS:
            body.append('- ' + label + '：' + review.get('floorplan_checks', {}).get(key, '待核验'))
        body.append('')
    if note_exists:
        relative = note.relative_to(vault).as_posix()
        return dict(note=relative, dimension_schema=1, dimension_files=dimension_files,
                    image_schema=1, image_files=image_files, evidence_bundle=bundle_record) if include_manifest else relative
    temp = note.with_name(note.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            temp.chmod(0o600)
            stream.write('\n'.join(body))
            stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temp, note)
        except FileExistsError as error:
            raise ValueError('存档笔记已由其他操作创建，请刷新后核对；未覆盖已有笔记') from error
    finally:
        temp.unlink(missing_ok=True)
    relative = note.relative_to(vault).as_posix()
    return dict(note=relative, dimension_schema=1, dimension_files=dimension_files,
                image_schema=1, image_files=image_files, evidence_bundle=bundle_record) if include_manifest else relative
