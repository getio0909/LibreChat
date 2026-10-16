import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

default_plugin = (Path('/Users/ipd/workspace/ComfyUI/custom_nodes/zyb_obsidian_design')
                  if sys.platform == 'darwin' else Path(__file__).parent / 'zyb_obsidian_design')
plugin = Path(os.environ.get('ZYB_PLUGIN_PATH', str(default_plugin)))
spec = importlib.util.spec_from_file_location('studio_store', plugin / 'studio_store.py')
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zyb-studio-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault, self.archive, self.output = (self.root / name for name in ('vault', 'archive', 'output'))
        for path in (self.vault / '20-图片笔记', self.archive, self.output):
            path.mkdir(parents=True)
        self.source = self.archive / 'stone.png'
        self.source.write_bytes(b'fixture')
        self.note = self.vault / '20-图片笔记/stone.md'
        self.note.write_text('---\nsource_path: ' + str(self.source) + '\nmaterials: [石材]\nsecret: PRIVATE_SENTINEL\n---\n# 石材样例\n> [!summary]\n> 浅色石材，细直纹。\n\n## 内部字段\nOMITTED_SENTINEL\n')
        self.catalog = studio.Catalog(self.vault, self.archive)
        self.store = studio.Store(self.root / 'data')
        self.brief = {'title': '技术样例', 'category': studio.CATEGORIES[0], 'goal': '桌面摆件'}

    def plan(self):
        ref = next(iter(self.catalog.scan()))
        return {'concepts': [dict(name='方向 ' + str(i), concept='独立摆件', form='圆形基座',
                materials='石材建议', rationale='依据形态资料', references=[ref], risks=['待工艺核验'],
                image_description_en='A single tabletop stone ornament, with subtle grooves and an understated plain backdrop.') for i in range(3)]}

    def task(self):
        task = self.store.create(self.brief)
        ref = next(iter(self.catalog.scan()))
        task['references'] = [{'id': ref, 'role': '形态'}]
        task['concepts'], private = studio.validate_plan(self.plan(), task['references'])
        task['selected'] = task['concepts'][0]['id']
        task['review']['reviewer'] = '技术验收'
        (self.output / 'image.png').write_bytes(b'image-fixture')
        task['images'] = [dict(id='a'*32, concept_id=task['selected'], file='image.png',
                              version=1, width=1024, height=1024, kind='generate')]
        return task, private

    def test_create(self):
        t = self.store.create(self.brief)
        self.assertEqual(self.store.get(t['id']), t)
        self.assertEqual(self.store.db.stat().st_mode & 0o777, 0o600)

    def visual_check(self):
        return {'image_id': 'a'*32, 'image_sha256': hashlib.sha256(b'image-fixture').hexdigest(),
                'created': '2026-09-22T00:00:00Z', 'reported_model': 'deepseek-flash',
                'basis': '图像生成时的设计约束', 'limitation': '仍需人工核验',
                'checks': [{'label': '主体与数量', 'verdict': '疑似不符', 'observation': '多出一个可见部件。'}]}

    def test_archive_visual_check_is_version_bound_and_not_approval(self):
        task, private = self.task()
        task['visual_checks'] = [self.visual_check(), {**self.visual_check(), 'image_id': 'f'*32}]
        note = studio.archive_note(task, self.catalog, self.output, self.archive / 'results')
        text = (self.vault / note).read_text()
        self.assertEqual(text.count('### 图像版本 1'), 1)
        self.assertIn('多出一个可见部件', text)
        self.assertIn('设计：待核验', text)
        self.assertTrue(all(value not in text for value in private.values()))

    def test_new_visual_check_creates_new_archive_without_overwrite(self):
        task, _ = self.task()
        first = studio.archive_note(task, self.catalog, self.output, self.archive / 'results')
        before = (self.vault / first).read_bytes()
        task['visual_checks'] = [self.visual_check()]
        second = studio.archive_note(task, self.catalog, self.output, self.archive / 'results')
        self.assertNotEqual(first, second)
        self.assertEqual((self.vault / first).read_bytes(), before)

    def test_visual_check_stale_hash_is_not_archived_as_current(self):
        task, _ = self.task()
        task['visual_checks'] = [{**self.visual_check(), 'image_sha256': '0'*64}]
        note = studio.archive_note(task, self.catalog, self.output, self.archive / 'results')
        text = (self.vault / note).read_text()
        self.assertIn('旧核对结果失效', text)
        self.assertNotIn('多出一个可见部件', text)

    def test_goal_only_creates_without_references(self):
        t = self.store.create({'goal': '简洁的木质佛龛'})
        self.assertEqual(t['brief']['title'], '简洁的木质佛龛')
        self.assertEqual(t['brief']['category'], '其他设计')
        self.assertEqual(t['references'], [])
        self.assertEqual(t['status'], '待构思')

    def test_explicit_title_and_category_preserved(self):
        t = self.store.create({**self.brief, 'title': '已定名称', 'category': '家具与佛龛'})
        self.assertEqual(t['brief']['title'], '已定名称')
        self.assertEqual(t['brief']['category'], '家具与佛龛')

    def test_invalid_brief(self):
        for value in ([], None, {}, {'title': 'a', 'category': 'unknown', 'goal': 'b'}):
            with self.assertRaises(ValueError):
                self.store.create(value)

    def test_conflict_does_not_mutate(self):
        t = self.store.create(self.brief)
        self.store.put(t, 1)
        original = copy.deepcopy(t)
        with self.assertRaises(ValueError):
            self.store.put(t, 1)
        self.assertEqual(t, original)

    def test_restart_marks_interrupted(self):
        t = self.store.create(self.brief)
        t['job'] = {'status': 'running'}
        self.store.put(t, 1)
        self.store.interrupted()
        self.assertEqual(self.store.get(t['id'])['job']['status'], 'interrupted')

    def test_ids(self):
        for value in ('../source', '/etc/hosts', 'a'*31, None):
            with self.assertRaises(ValueError):
                studio.ident(value)

    def test_catalog_allowlist(self):
        refs = self.catalog.search('石材')
        self.assertEqual(len(refs), 1)
        data = json.dumps(refs, ensure_ascii=False)
        self.assertNotIn('SENTINEL', data)
        self.assertNotIn(str(self.source), data)
        self.assertIn('细直纹', data)

    def external_note(self, body, title=''):
        note = self.vault / '40-外部参考/素材笔记/fixture.md'
        note.parent.mkdir(parents=True)
        note.write_text('---\ntitle: ' + json.dumps(title, ensure_ascii=False) + '\nfile: ' + str(self.source) + '\nstatus: ok\neffective_status: available\nsecret: PRIVATE_SENTINEL\n---\n' + body)

    def test_external_level_two_title_matches_metadata(self):
        self.external_note('## 砖塔佛龛\n\n砖塔外墙的小佛龛浮雕。\n\n**文件**: OMITTED_SENTINEL', title='砖塔佛龛')
        ref = self.catalog.search('砖塔佛龛')[0]
        self.assertEqual(ref['title'], '砖塔佛龛')
        self.assertEqual(ref['fields']['visual_description'], '砖塔外墙的小佛龛浮雕。')
        self.assertNotIn('SENTINEL', json.dumps(ref))

    def test_external_unrelated_heading_is_not_a_description(self):
        self.external_note('## 内部字段\n\nOMITTED_SENTINEL', title='砖塔佛龛')
        ref = self.catalog.search('砖塔佛龛')[0]
        self.assertNotIn('visual_description', ref['fields'])

    def test_external_intro_is_available_but_other_sections_are_not(self):
        self.external_note('# 组合佛龛\n\n木质柜体，中间开放龛位。\n两侧收纳。\n\n[file link](file:///PRIVATE_SENTINEL)\n## 内部字段\nOMITTED_SENTINEL\n')
        refs = self.catalog.search('组合佛龛')
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]['fields']['visual_description'], '木质柜体，中间开放龛位。 两侧收纳。')
        self.assertNotIn('SENTINEL', json.dumps(refs))

    def test_external_intro_stops_before_metadata(self):
        self.external_note('# 佛龛样例\n\n[file link](file:///PRIVATE_SENTINEL)\nOMITTED_SENTINEL\n')
        ref = self.catalog.search('佛龛样例')[0]
        self.assertNotIn('visual_description', ref['fields'])

    def test_external_summary_takes_precedence(self):
        self.external_note('# 佛龛样例\n\n介绍文字。\n\n> [!summary]\n> 已整理的视觉描述。\n\nOMITTED_SENTINEL\n')
        ref = self.catalog.search('佛龛样例')[0]
        self.assertEqual(ref['fields']['visual_description'], '已整理的视觉描述。')

    def test_retired_note(self):
        self.note.write_text(self.note.read_text().replace('materials:', 'effective_status: eliminated\nmaterials:'))
        self.assertEqual(self.catalog.search(''), [])

    def test_bad_note_does_not_break_catalog(self):
        (self.note.parent / 'bad.md').write_bytes(b'\xff')
        self.assertEqual(len(self.catalog.search('')), 1)

    def test_outside_source(self):
        outside = self.root / 'outside.png'
        outside.write_bytes(b'fixture')
        self.note.write_text(self.note.read_text().replace(str(self.source), str(outside)))
        self.assertFalse(self.catalog.search(''))

    def test_plan_is_private(self):
        public, private = studio.validate_plan(self.plan(), [{'id': next(iter(self.catalog.scan()))}])
        self.assertEqual(len(public), 3)
        self.assertEqual(len(private), 3)
        self.assertNotIn('image_description_en', json.dumps(public))

    def test_no_reference_plan_records_its_basis(self):
        raw = self.plan()
        for row in raw['concepts']:
            row['references'] = []
        public, private = studio.validate_plan(raw, [])
        self.assertEqual(len(public), 3)
        self.assertEqual(len(private), 3)
        for row in public:
            self.assertEqual(row['basis'], '无参考构思')
            self.assertEqual(row['references'], [])
            self.assertEqual(row['reference_snapshot'], [])
            self.assertIn('未使用参考资料', row['risks'][0])

    def test_no_reference_plan_rejects_fabricated_sources(self):
        with self.assertRaisesRegex(ValueError, '无效资料'):
            studio.validate_plan(self.plan(), [])

    def test_bad_plans(self):
        for value in ({}, {'concepts': []}, {'concepts': [None]*3}):
            with self.assertRaises(ValueError):
                studio.validate_plan(value, [])
        bad = self.plan()
        bad['concepts'][0]['references'] = [{}]
        with self.assertRaises(ValueError):
            studio.validate_plan(bad, [])

    def test_archive_idempotency_and_links(self):
        t, private = self.task()
        first = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        t['revision'] += 1
        second = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        self.assertEqual(first, second)
        content = (self.vault / first).read_text()
        self.assertIn('[[20-图片笔记/stone|石材样例]]', content)
        self.assertNotIn(next(iter(private.values())), content)
        self.assertNotIn('SENTINEL', content)
        self.assertEqual(self.source.read_bytes(), b'fixture')
        self.assertEqual(len(list((self.archive / '设计成果').rglob('*.png'))), 1)

    def test_archive_needs_reviewer(self):
        t, _ = self.task()
        t['review']['reviewer'] = ''
        with self.assertRaises(ValueError):
            studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')

    def test_archive_no_reference_does_not_invent_links(self):
        t, _ = self.task()
        raw = self.plan()
        for row in raw['concepts']:
            row['references'] = []
        t['concepts'], _ = studio.validate_plan(raw, [])
        t['selected'] = t['concepts'][0]['id']
        t['images'][0]['concept_id'] = t['selected']
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        content = (self.vault / note).read_text()
        self.assertIn('无参考构思', content)
        self.assertNotIn('[[', content)

    def test_archive_keeps_concept_reference_snapshot(self):
        t, _ = self.task()
        t['references'] = []
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        self.assertIn('[[20-图片笔记/stone|石材样例]]', (self.vault / note).read_text())

    def test_legacy_sources_survive_switch_to_no_reference(self):
        t, _ = self.task()
        for concept in t['concepts']:
            concept.pop('reference_snapshot')
        self.assertTrue(studio.preserve_references(t, self.catalog))
        t['references'] = []
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        content = (self.vault / note).read_text()
        self.assertIn('[[20-图片笔记/stone|石材样例]]', content)
        self.assertNotIn('无参考构思：未引用资料', content)

    def test_legacy_sources_use_concept_ids_not_current_selection(self):
        t, _ = self.task()
        for concept in t['concepts']:
            concept.pop('reference_snapshot')
        t['references'] = [{'id': 'unrelated', 'role': '材质'}]
        studio.preserve_references(t, self.catalog)
        self.assertEqual(t['concepts'][0]['reference_snapshot'][0]['id'], next(iter(self.catalog.scan())))
        self.assertEqual(t['concepts'][0]['reference_snapshot'][0]['role'], '历史用途待确认')

    def test_legacy_unknown_basis_is_not_declared_no_reference(self):
        t, _ = self.task()
        for concept in t['concepts']:
            concept.pop('reference_snapshot')
            concept['references'] = []
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        content = (self.vault / note).read_text()
        self.assertIn('历史来源记录不完整', content)
        self.assertNotIn('无参考构思：未引用资料', content)
        self.assertNotIn('[[', content)

    def test_reference_upgrade_is_idempotent_and_preserves_task(self):
        t, _ = self.task()
        for concept in t['concepts']:
            concept.pop('reference_snapshot')
        t = self.store.put(t, t['revision'])
        self.assertEqual(self.store.upgrade_references(self.catalog), 1)
        upgraded = self.store.get(t['id'])
        for key in ('brief', 'references', 'images', 'selected', 'feedback', 'review', 'archives', 'job'):
            self.assertEqual(upgraded[key], t[key])
        self.assertEqual(upgraded['revision'], t['revision'] + 1)
        self.assertEqual(self.store.upgrade_references(self.catalog), 0)
        self.assertEqual(self.store.get(t['id']), upgraded)

    def test_snapshot_excludes_private_and_full_note_data(self):
        ref = self.catalog.snapshot({'id': next(iter(self.catalog.scan())), 'role': '形态'})
        self.assertEqual(set(ref), {'id', 'role', 'title', 'note', 'source_id', 'rights'})
        self.assertNotIn(str(self.source), json.dumps(ref))
        self.assertNotIn('SENTINEL', json.dumps(ref))

    def test_archive_resolves_renamed_note_without_waiting_for_cache(self):
        t, _ = self.task()
        studio.preserve_references(t, self.catalog)
        self.note.rename(self.note.with_name('renamed.md'))
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        content = (self.vault / note).read_text()
        self.assertIn('[[20-图片笔记/renamed|石材样例]]', content)
        self.assertNotIn('[[20-图片笔记/stone', content)

    def test_source_identity_survives_note_move_and_title_change(self):
        t, _ = self.task()
        studio.preserve_references(t, self.catalog)
        old = copy.deepcopy(t['concepts'][0]['reference_snapshot'][0])
        target = self.vault / '20-图片笔记/nested/renamed.md'
        target.parent.mkdir()
        self.note.rename(target)
        target.write_text(target.read_text().replace('# 石材样例', '# 新标题'))
        entry = next(iter(self.catalog.scan(refresh=True).values()))
        self.assertEqual(entry['source_id'], old['source_id'])
        self.assertNotEqual(entry['id'], old['id'])
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        content = (self.vault / note).read_text()
        self.assertIn('[[20-图片笔记/nested/renamed|石材样例]]', content)
        self.assertEqual(t['concepts'][0]['reference_snapshot'][0], old)

    def test_deleted_reference_does_not_block_or_create_broken_link(self):
        t, _ = self.task()
        studio.preserve_references(t, self.catalog)
        self.note.unlink()
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        content = (self.vault / note).read_text()
        self.assertIn('石材样例', content)
        self.assertIn('保留历史来源待核验', content)
        self.assertNotIn('[[', content)
        self.assertNotIn('无参考构思：未引用资料', content)

    def test_retired_reference_not_linked_as_current(self):
        t, _ = self.task()
        studio.preserve_references(t, self.catalog)
        self.note.write_text(self.note.read_text().replace('materials:', 'effective_status: eliminated\nmaterials:'))
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        self.assertNotIn('[[', (self.vault / note).read_text())

    def test_ambiguous_renamed_reference_is_not_guessed(self):
        t, _ = self.task()
        studio.preserve_references(t, self.catalog)
        body = self.note.read_text()
        self.note.rename(self.note.with_name('first.md'))
        self.note.with_name('second.md').write_text(body)
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        self.assertNotIn('[[', (self.vault / note).read_text())

    def test_reused_note_path_does_not_retarget_to_other_asset(self):
        t, _ = self.task()
        studio.preserve_references(t, self.catalog)
        self.note.rename(self.note.with_name('renamed.md'))
        other = self.archive / 'other.png'
        other.write_bytes(b'other-fixture')
        self.note.write_text('---\nsource_path: ' + str(other) + '\n---\n# 不同资料\n')
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        content = (self.vault / note).read_text()
        self.assertIn('[[20-图片笔记/renamed|石材样例]]', content)
        self.assertNotIn('[[20-图片笔记/stone', content)

    def test_legacy_missing_source_does_not_block_archive(self):
        t, _ = self.task()
        for concept in t['concepts']:
            concept.pop('reference_snapshot')
        self.note.unlink()
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        self.assertIn('资料编号', (self.vault / note).read_text())

    def test_archive_fingerprint_tracks_resolved_sources(self):
        t, _ = self.task()
        first = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        t['references'] = []
        self.assertEqual(first, studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果'))
        self.note.rename(self.note.with_name('renamed.md'))
        second = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        self.assertNotEqual(first, second)
        self.assertIn('[[20-图片笔记/renamed|石材样例]]', (self.vault / second).read_text())

    def test_snapshot_does_not_follow_note_directory_outside_vault(self):
        t, _ = self.task()
        studio.preserve_references(t, self.catalog)
        directory = self.note.parent
        outside = self.root / 'outside-notes'
        directory.rename(outside)
        directory.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.catalog.scan(refresh=True), {})
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        self.assertNotIn('[[', (self.vault / note).read_text())

    def test_archive_cannot_escape(self):
        t, _ = self.task()
        with self.assertRaises(ValueError):
            studio.archive_note(t, self.catalog, self.output, self.root / 'outside')

    def test_archive_prevalidates_all_images(self):
        t, _ = self.task()
        t['images'].append({**t['images'][0], 'id': 'b'*32, 'file': 'missing.png'})
        with self.assertRaises(FileNotFoundError):
            studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        self.assertFalse(list((self.archive / '设计成果').rglob('*.png')))

    def test_archive_preserves_jpeg_filename_and_bytes(self):
        t, _ = self.task()
        binary = b'jpeg-byte-preservation-fixture'
        (self.output / 'image.jpg').write_bytes(binary)
        t['images'][0].update(file='image.jpg', provider='ark', model='Seedream', reference_count=1)
        note = studio.archive_note(t, self.catalog, self.output, self.archive / '设计成果')
        target = self.archive / '设计成果' / t['id'] / ('a' * 32 + '.jpg')
        self.assertEqual(target.read_bytes(), binary)
        self.assertIn(target.as_uri(), (self.vault / note).read_text())

    def test_restart_marks_cloud_pending_uncertain(self):
        t = self.store.create(self.brief)
        t['job'] = {'status': 'running', 'cloud_pending': True}
        self.store.put(t, t['revision'])
        self.store.interrupted()
        self.assertEqual(self.store.get(t['id'])['job']['status'], 'uncertain')

    def test_private_write_permissions(self):
        path = self.root / 'private/value.json'
        studio.write_private(path, '{}')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_feedback_requires_review_and_excludes_demo(self):
        t = self.store.create(self.brief)
        t['feedback'].append({'decision': '不采用', 'comment': '结构无法确认'})
        t = self.store.put(t, t['revision'])
        self.assertFalse(self.store.context_feedback(t['brief']['category']))
        t['review'].update(reviewer='整理人', design='不通过')
        t = self.store.put(t, t['revision'])
        self.assertEqual(len(self.store.context_feedback(t['brief']['category'])), 1)
        t['is_demo'] = True
        self.store.put(t, t['revision'])
        self.assertFalse(self.store.context_feedback(t['brief']['category']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
