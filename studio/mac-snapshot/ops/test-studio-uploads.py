import ast
import copy
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, PngImagePlugin

plugin = Path(os.environ.get('ZYB_PLUGIN_PATH', str(Path(__file__).parent / 'zyb_obsidian_design')))
package = types.ModuleType('upload_test_plugin')
package.__path__ = [str(plugin)]
sys.modules[package.__name__] = package
studio = importlib.import_module(package.__name__ + '.studio_store')
routes = types.ModuleType(package.__name__ + '.studio_routes')
routes.__package__ = package.__name__
tree = ast.parse((plugin / 'studio_routes.py').read_text())
tree.body = [node for node in tree.body if not isinstance(node, ast.ClassDef)
             and not (isinstance(node, ast.Import) and any(a.name.split('.')[0] in ('comfy', 'torch', 'numpy') for a in node.names))
             and not (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'NODES' for t in node.targets))]
exec(compile(tree, str(plugin / 'studio_routes.py'), 'exec'), routes.__dict__)


def fixture(fmt='PNG', size=(96, 64)):
    out = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('prompt', 'PRIVATE_METADATA_SENTINEL')
    metadata.add_text('Description', 'PRIVATE_METADATA_SENTINEL')
    Image.new('RGB', size, (210, 200, 180)).save(out, format=fmt, **({'pnginfo': metadata} if fmt == 'PNG' else {}))
    return out.getvalue()


class UploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='zyb-upload-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.store = studio.Store(self.root / 'data')
        self.task = self.store.create({'goal': '室内陈设效果图', 'is_demo': True})

    def upload(self, **kwargs):
        values = dict(task=self.task, binary=fixture(), filename='room.png', mode='scene', output=self.output)
        values.update(kwargs)
        return self.store.import_image(**values)

    def test_upload_creates_selected_baseline_without_planning(self):
        task = self.upload()
        image = task['images'][0]
        self.assertEqual(task['selected'], task['concepts'][0]['id'])
        self.assertEqual(task['concepts'][0]['source_image_id'], image['id'])
        self.assertEqual(image['kind'], 'upload')
        self.assertEqual(image['version'], 1)
        self.assertIsNone(task['job'])
        self.assertEqual(task['review']['design'], '待核验')
        self.assertEqual(self.store.get(task['id']), task)

    def test_upload_preserves_pixels_and_strips_metadata(self):
        task = self.upload()
        path = self.output / task['images'][0]['file']
        with Image.open(path) as image:
            self.assertEqual(image.size, (96, 64))
            self.assertEqual(image.getpixel((10, 10)), (210, 200, 180))
            self.assertFalse(image.info)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(b'PRIVATE_METADATA_SENTINEL', path.read_bytes())

    def test_supported_formats(self):
        for fmt in ('JPEG', 'PNG', 'WEBP'):
            with self.subTest(fmt=fmt):
                task = self.store.create({'goal': '格式测试'})
                self.assertEqual(len(self.upload(task=task, binary=fixture(fmt))['images']), 1)

    def test_filename_never_controls_destination(self):
        task = self.upload(filename='../../outside.png')
        self.assertEqual(task['images'][0]['source_name'], 'outside.png')
        self.assertTrue((self.output / task['images'][0]['file']).resolve().is_relative_to(self.output.resolve()))

    def test_duplicate_preserves_versions(self):
        task = self.upload()
        before = copy.deepcopy(task['images'])
        updated = self.upload(task=task)
        self.assertEqual(updated['images'], before)
        self.assertEqual(len(updated['concepts']), 1)

    def test_new_upload_does_not_overwrite_old(self):
        task = self.upload()
        path = self.output / task['images'][0]['file']
        before = path.read_bytes()
        updated = self.upload(task=task, binary=fixture(size=(80, 80)), mode='edit')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(updated['images']), 2)
        self.assertNotEqual(task['selected'], updated['selected'])

    def test_missing_duplicate_source_creates_a_working_baseline(self):
        task = self.upload()
        (self.output / task['images'][0]['file']).unlink()
        updated = self.upload(task=task)
        self.assertEqual(len(updated['images']), 2)
        self.assertTrue((self.output / updated['images'][-1]['file']).is_file())

    def test_color_profile_converted_and_removed(self):
        buffer = io.BytesIO()
        profile = studio.ImageCms.ImageCmsProfile(studio.ImageCms.createProfile('sRGB')).tobytes()
        Image.new('RGBA', (64, 64), (120, 80, 60, 128)).save(buffer, format='PNG', icc_profile=profile)
        task = self.upload(binary=buffer.getvalue())
        with Image.open(self.output / task['images'][0]['file']) as image:
            self.assertEqual(image.getpixel((0, 0)), (120, 80, 60, 128))
            self.assertFalse(image.info)

    def test_mpo_phone_photo_uses_primary_frame(self):
        buffer = io.BytesIO()
        Image.new('RGB', (64, 64), 'red').save(buffer, format='MPO', save_all=True,
            append_images=[Image.new('RGB', (64, 64), 'blue')])
        task = self.upload(binary=buffer.getvalue(), filename='phone.jpg')
        with Image.open(self.output / task['images'][0]['file']) as image:
            self.assertGreater(image.getpixel((0, 0))[0], 240)
            self.assertLess(image.getpixel((0, 0))[2], 10)

    def test_invalid_color_profile_has_a_clear_error(self):
        buffer = io.BytesIO()
        Image.new('RGB', (64, 64)).save(buffer, format='PNG', icc_profile=b'invalid')
        with self.assertRaisesRegex(ValueError, '色彩配置'):
            self.upload(binary=buffer.getvalue())

    def test_icc_conversion_uses_profile_input_color_space(self):
        for mode, color_space, fmt in [('CMYK', 'CMYK', 'JPEG'), ('L', 'GRAY', 'PNG')]:
            with self.subTest(mode=mode):
                buffer = io.BytesIO()
                Image.new(mode, (64, 64)).save(buffer, format=fmt, icc_profile=b'PROFILE_FIXTURE')
                profile = types.SimpleNamespace(profile=types.SimpleNamespace(xcolor_space=color_space))
                def transform(source, *_args, **_kwargs):
                    self.assertEqual(source.mode, mode)
                    return source.convert('RGB')
                with patch.object(studio.ImageCms, 'ImageCmsProfile', return_value=profile), \
                        patch.object(studio.ImageCms, 'profileToProfile', side_effect=transform):
                    task = self.upload(task=self.store.create({'goal': '色彩空间验证'}), binary=buffer.getvalue())
                self.assertTrue((self.output / task['images'][-1]['file']).is_file())

    def test_invalid_inputs_leave_task_unchanged(self):
        for kwargs in ({'binary': b'<svg />'}, {'binary': b''}, {'mode': 'arbitrary'}, {'binary': fixture('GIF')}):
            with self.subTest(kwargs=kwargs.keys()), self.assertRaises(ValueError):
                self.upload(**kwargs)
        self.assertEqual(self.store.get(self.task['id']), self.task)
        self.assertFalse(list(self.output.rglob('original.png')))

    def test_byte_and_pixel_limits(self):
        with patch.object(studio, 'MAX_UPLOAD_BYTES', 20), self.assertRaises(ValueError):
            self.upload()
        with patch.object(studio, 'MAX_UPLOAD_PIXELS', 40), self.assertRaises(ValueError):
            self.upload()

    def test_symlink_output_rejected(self):
        (self.output / '产品设计').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.upload()

    def test_revision_conflict_cleans_only_new_file(self):
        self.store.put(self.task, self.task['revision'])
        with self.assertRaises(ValueError):
            self.upload()
        self.assertFalse(list(self.output.rglob('original.png')))


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix='zyb-upload-api-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        routes.OPS, routes.ROOT = self.root / 'ops', self.root / 'data'
        routes.VAULT, routes.ARCHIVE, routes.OUTPUT = (self.root / name for name in ('vault', 'archive', 'output'))
        for folder in (routes.OPS, routes.VAULT, routes.ARCHIVE, routes.OUTPUT):
            folder.mkdir()
        (routes.OPS / 'studio-runtime.json').write_text(json.dumps({'render_prefix': '', 'edit_prefix': ''}))
        routes.catalog = studio.Catalog(routes.VAULT, routes.ARCHIVE)
        routes.workers = {}
        server = types.SimpleNamespace(routes=web.RouteTableDef())
        routes.register(server)
        app = web.Application(client_max_size=studio.MAX_UPLOAD_BYTES)
        app.add_routes(server.routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.task = routes.store.create({'goal': '室内陈设效果图', 'is_demo': True})

    async def upload(self, **kwargs):
        return await self.client.post('/zyb/studio/tasks/' + self.task['id'] + '/upload',
            params={'revision': str(self.task['revision']), 'mode': 'scene', 'name': 'room.png'},
            data=fixture(), headers=kwargs.get('headers', {'X-Zyb-Studio': '1', 'Content-Type': 'application/octet-stream'}))

    async def action(self, action, **data):
        response = await self.client.post('/zyb/studio/tasks/' + self.task['id'] + '/' + action,
            json={'revision': self.task['revision'], **data}, headers={'X-Zyb-Studio': '1'})
        return response

    async def test_upload_endpoint(self):
        response = await self.upload()
        self.assertEqual(response.status, 200)
        task = await response.json()
        self.assertEqual(task['images'][0]['kind'], 'upload')
        self.assertFalse(routes.workers)
        self.assertNotIn('PRIVATE_METADATA_SENTINEL', json.dumps(task))

    async def test_upload_csrf_and_origin(self):
        for headers in ({'Content-Type': 'application/octet-stream'},
                        {'X-Zyb-Studio': '1', 'Content-Type': 'application/octet-stream', 'Origin': 'https://example.invalid'}):
            response = await self.upload(headers=headers)
            self.assertEqual(response.status, 400)
        self.assertFalse(routes.store.get(self.task['id'])['images'])

    async def test_oversize_upload_rejected(self):
        with patch.object(routes, 'MAX_UPLOAD_BYTES', 16):
            response = await self.upload()
        self.assertEqual(response.status, 400)

    async def test_stale_revision_rejected(self):
        routes.store.put(self.task, self.task['revision'])
        response = await self.upload()
        self.assertEqual(response.status, 400)

    async def test_upload_blocked_during_generation(self):
        self.task['job'] = {'status': 'running'}
        self.task = routes.store.put(self.task, self.task['revision'])
        response = await self.upload()
        self.assertEqual(response.status, 400)

    async def test_imported_image_flows_to_generation_and_refinement(self):
        self.task = await (await self.upload()).json()
        source = self.task['images'][0]
        calls = []
        def generate(config, text, refs, size, aspect, destination):
            calls.append((text, refs))
            destination.mkdir(parents=True)
            path = destination / 'original.jpg'
            path.write_bytes(fixture('JPEG'))
            return {'path': path, 'reference_count': len(refs), 'model': 'test-service'}
        for action in ('refine', 'generate'):
            self.task['job'] = {'status': 'running', 'cancel': False}
            self.task = routes.store.put(self.task, self.task['revision'])
            options = dict(quality='精细', size='2K', aspect='4:3', cloud_consent=True,
                           concept_ids=[self.task['selected']], image_id=source['id'], change='修改墙面颜色')
            with patch.object(routes, 'read_config', return_value={'provider': 'ark', 'edit_rules': 'TEST_POLICY'}), patch.object(routes, 'cloud_generate', side_effect=generate):
                await routes.work(self.task['id'], action, options)
            self.task = routes.store.get(self.task['id'])
            self.assertEqual(self.task['job']['status'], 'done', self.task['job'])
            self.assertEqual(self.task['images'][-1]['parent_id'], source['id'])
            self.assertEqual(self.task['review']['image_id'], self.task['images'][-1]['id'])
            self.assertEqual(self.task['review']['design'], '待核验')
            self.assertEqual(len(calls[-1][1]), 1)
            self.assertTrue(calls[-1][1][0].startswith('data:image/jpeg;base64,'))
            self.assertNotIn('TEST_POLICY', json.dumps(self.task))

    async def test_archive_and_library_paths(self):
        self.task = await (await self.upload()).json()
        self.task['review']['reviewer'] = '技术验收'
        self.task = routes.store.put(self.task, self.task['revision'])
        response = await self.action('archive')
        self.assertEqual(response.status, 200)
        self.task = await response.json()
        listing = await (await self.client.get('/zyb/studio/library')).json()
        self.assertEqual(listing['items'][0]['id'], self.task['id'])
        self.assertEqual(listing['items'][0]['archives'][0]['image_ids'], [self.task['images'][0]['id']])
        note = studio.archive_location(self.task, 0, routes.VAULT, routes.ARCHIVE)
        self.assertIn('上传图片为基础', note.read_text())
        self.assertNotIn('PRIVATE_METADATA_SENTINEL', note.read_text())
        for target in ('note', 'images'):
            with patch.object(routes.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as opened:
                response = await self.client.post('/zyb/studio/library/open', json={'task_id': self.task['id'], 'index': 0, 'target': target}, headers={'X-Zyb-Studio': '1'})
                self.assertEqual(response.status, 200)
                self.assertEqual(opened.call_args.args[0][0], '/usr/bin/open')
                self.assertNotIn('shell', opened.call_args.kwargs)
        for index in (-1, 1, True, '0'):
            with self.assertRaises(ValueError):
                studio.archive_location(self.task, index, routes.VAULT, routes.ARCHIVE)
        self.task['archives'][0]['note'] = '../outside.md'
        with self.assertRaises(ValueError):
            studio.archive_location(self.task, 0, routes.VAULT, routes.ARCHIVE)

    async def test_output_root_alias_is_normalized_consistently(self):
        alias = self.root / 'output-alias'
        alias.symlink_to(routes.OUTPUT, target_is_directory=True)
        routes.OUTPUT = alias
        await self.test_imported_image_flows_to_generation_and_refinement()

    async def test_returned_cloud_image_registration_failure_is_uncertain(self):
        self.task = await (await self.upload()).json()
        self.task['job'] = {'status': 'running', 'cancel': False}
        self.task = routes.store.put(self.task, self.task['revision'])
        options = dict(quality='精细', size='2K', aspect='4:3', cloud_consent=True,
                       concept_ids=[self.task['selected']], change='')
        with patch.object(routes, 'read_config', return_value={'provider': 'ark', 'edit_rules': 'TEST_POLICY'}), \
                patch.object(routes, 'cloud_generate', return_value={'path': self.root / 'returned.jpg'}), \
                patch.object(routes, 'save_image', side_effect=ValueError('registration failed')):
            await routes.work(self.task['id'], 'generate', options)
        self.task = routes.store.get(self.task['id'])
        self.assertEqual(self.task['job']['status'], 'uncertain')
        response = await self.action('generate', concept_ids=[self.task['selected']], cloud_consent=True)
        self.assertEqual(response.status, 400)
        self.assertIn('未确认', (await response.json())['error'].replace('状态不确定', '未确认'))

    async def test_local_uploaded_generation_keeps_design_goal(self):
        self.task = await (await self.upload()).json()
        self.task['job'] = {'status': 'running', 'cancel': False}
        self.task = routes.store.put(self.task, self.task['revision'])
        options = dict(quality='精细', size='2K', aspect='4:3', cloud_consent=False,
                       concept_ids=[self.task['selected']], change='')
        with patch.object(routes, 'read_config', return_value={'provider': 'local'}), \
                patch.object(routes, 'local_image') as rendered:
            await routes.work(self.task['id'], 'generate', options)
        self.assertEqual(rendered.call_args.args[-1]['text'], self.task['brief']['goal'])

    async def test_basis_failure_does_not_lose_upload_and_retry_is_deduplicated(self):
        with patch.object(routes, 'write_private', side_effect=OSError('disk error')):
            response = await self.upload()
        self.assertEqual(response.status, 400)
        self.assertIn('图片已保存', (await response.json())['error'])
        self.task = routes.store.get(self.task['id'])
        self.assertEqual(len(self.task['images']), 1)
        response = await self.upload()
        self.assertEqual(response.status, 200)
        self.assertEqual(len((await response.json())['images']), 1)

    async def test_archive_images_open_after_note_move_and_missing_copy_is_restored(self):
        await self.test_archive_and_library_paths()
        self.task = routes.store.get(self.task['id'])
        note = studio.archive_location(self.task, 0, routes.VAULT, routes.ARCHIVE)
        folder = studio.archive_location(self.task, 0, routes.VAULT, routes.ARCHIVE, 'images')
        image = next(folder.iterdir())
        before = image.read_bytes()
        image.unlink()
        self.assertEqual((await self.action('archive')).status, 200)
        self.assertEqual(image.read_bytes(), before)
        note.rename(note.with_name('renamed.md'))
        self.assertEqual(studio.archive_location(self.task, 0, routes.VAULT, routes.ARCHIVE, 'images'), folder)

    async def test_library_search_fields_and_feedback_are_direction_scoped(self):
        self.task = await (await self.upload()).json()
        self.task['feedback'] = [{'concept_id': 'f' * 32, 'decision': '不采用', 'comment': 'UNRELATED_FEEDBACK'},
                                {'concept_id': self.task['selected'], 'decision': '待客户确认', 'comment': 'CURRENT_FEEDBACK'}]
        self.task['review']['reviewer'] = '技术验收'
        self.task = routes.store.put(self.task, self.task['revision'])
        self.task = await (await self.action('archive')).json()
        note = studio.archive_location(self.task, 0, routes.VAULT, routes.ARCHIVE).read_text()
        self.assertNotIn('UNRELATED_FEEDBACK', note)
        self.assertIn('CURRENT_FEEDBACK', note)
        listing = await (await self.client.get('/zyb/studio/library')).json()
        self.assertEqual(listing['items'][0]['brief']['goal'], self.task['brief']['goal'])

    async def test_archive_tracks_reviewed_version_instead_of_latest(self):
        self.task = await (await self.upload()).json()
        source = self.task['images'][0]
        newer = {**source, 'id': 'a' * 32, 'parent_id': source['id'], 'version': 2, 'kind': 'refine'}
        self.task['images'].append(newer)
        self.task = routes.store.put(self.task, self.task['revision'])
        response = await self.action('review', reviewer='技术验收', image_id=source['id'])
        self.assertEqual(response.status, 200)
        self.task = await response.json()
        response = await self.action('archive')
        self.assertEqual(response.status, 200)
        self.task = await response.json()
        saved = self.task['archives'][0]
        self.assertEqual(saved['image_id'], source['id'])
        note = studio.archive_location(self.task, 0, routes.VAULT, routes.ARCHIVE).read_text()
        self.assertIn('本次确认对应版本 1', note)
        listing = await (await self.client.get('/zyb/studio/library')).json()
        self.assertEqual(listing['items'][0]['archives'][0]['image']['id'], source['id'])

    async def test_review_rejects_version_from_another_direction(self):
        self.task = await (await self.upload()).json()
        source_id = self.task['images'][0]['id']
        self.task = routes.store.import_image(self.task, fixture(size=(80, 80)), 'other.png', 'edit', routes.OUTPUT)
        response = await self.action('review', reviewer='技术验收', image_id=source_id)
        self.assertEqual(response.status, 400)
        self.assertNotIn('image_id', routes.store.get(self.task['id'])['review'])

    async def test_review_rejects_nonexistent_version(self):
        self.task = await (await self.upload()).json()
        response = await self.action('review', reviewer='技术验收', image_id='f' * 32)
        self.assertEqual(response.status, 400)


if __name__ == '__main__':
    unittest.main()
