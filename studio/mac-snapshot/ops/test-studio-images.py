import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from PIL import Image

plugin = Path(os.environ.get('ZYB_PLUGIN_PATH', str(Path(__file__).parent / 'zyb_obsidian_design')))
spec = importlib.util.spec_from_file_location('studio_images', plugin / 'studio_images.py')
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='zyb-image-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / 'key'
        self.key.write_text('unit-test-not-a-key')
        self.key.chmod(0o600)
        self.config = self.root / 'image.json'
        self.values = dict(provider='ark', model=images.MODEL, base_url=images.BASE_URL,
                           key_file=str(self.key), timeout_seconds=30)
        self.configure()
        output = io.BytesIO()
        Image.new('RGB', (32, 24), (160, 120, 80)).save(output, format='JPEG')
        self.binary = output.getvalue()
        self.opener = Mock()
        self.response = self.opener.open.return_value.__enter__ = Mock(return_value=Mock())
        self.opener.open.return_value.__exit__ = Mock(return_value=False)
        self.http = self.response.return_value
        self.http.read.return_value = json.dumps({'data': [{'b64_json': base64.b64encode(self.binary).decode()}]}).encode()

    def configure(self):
        self.config.write_text(json.dumps(self.values))
        self.config.chmod(0o600)

    def generate(self, **kwargs):
        options = dict(config_path=self.config, text='unit-test fixture', references=[],
                       size='2K', aspect='1:1', destination=self.root / 'job')
        options.update(kwargs)
        with patch.object(images.urllib.request, 'build_opener', return_value=self.opener):
            return images.generate(**options)

    def test_status_does_not_expose_key(self):
        value = images.image_status(self.config)
        self.assertTrue(value['image_ready'])
        self.assertNotIn('key', json.dumps(value))
        self.assertEqual(value['image_sizes'], ['1K', '2K'])

    def test_private_config_required(self):
        self.config.chmod(0o644)
        self.assertFalse(images.image_status(self.config)['image_ready'])

    def test_private_key_required(self):
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            images.read_config(self.config)

    def test_arbitrary_endpoint_rejected(self):
        self.values['base_url'] = 'https://example.invalid'
        self.configure()
        with self.assertRaises(ValueError):
            self.generate()
        self.opener.open.assert_not_called()

    def test_model_requires_compatible_adapter(self):
        self.values['model'] = 'different-model'
        self.configure()
        with self.assertRaises(ValueError):
            self.generate()

    def test_supported_dimensions_obey_live_area_bound(self):
        for options in images.NATIVE.values():
            for width, height in options.values():
                self.assertGreaterEqual(width * height, 921600)
                self.assertLessEqual(width * height, 4624220)

    def test_original_bytes_and_receipt(self):
        result = self.generate()
        self.assertEqual(result['path'].read_bytes(), self.binary)
        self.assertEqual(result['path'].suffix, '.jpg')
        self.assertEqual(result['path'].stat().st_mode & 0o777, 0o600)
        record = json.loads((result['path'].parent / 'receipt.json').read_text())
        self.assertEqual(record['state'], 'saved')
        self.assertNotIn('prompt', record)
        self.assertNotIn('key', record)
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertFalse(body['watermark'])
        self.assertNotIn('sequential_image_generation', body)
        self.assertNotIn('image', body)

    def test_exact_size_and_reference_forwarding(self):
        self.generate(size='1K', aspect='4:3', references=['data:image/jpeg;base64,fixture'])
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body['size'], '1152x864')
        self.assertIsInstance(body['image'], str)

    def test_multiple_references_forwarded(self):
        self.generate(references=['first', 'second'])
        body = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(body['image'], ['first', 'second'])

    def test_4k_rejected_before_network(self):
        with self.assertRaises(ValueError):
            self.generate(size='4K')
        self.opener.open.assert_not_called()

    def test_reference_limit(self):
        with self.assertRaises(ValueError):
            self.generate(references=['fixture'] * 11)

    def test_no_duplicate_submission_to_same_job(self):
        self.generate()
        with self.assertRaises(ValueError):
            self.generate()
        self.assertEqual(self.opener.open.call_count, 1)

    def test_http_error_redacted_and_not_retried(self):
        self.opener.open.side_effect = urllib.error.HTTPError(images.BASE_URL, 401, 'private-response', {}, io.BytesIO(b'private-content'))
        with self.assertRaisesRegex(RuntimeError, '密钥无效') as error:
            self.generate()
        self.assertNotIn('private', str(error.exception))
        self.assertEqual(self.opener.open.call_count, 1)

    def test_timeout_is_uncertain(self):
        self.opener.open.side_effect = TimeoutError()
        with self.assertRaises(images.GenerationUncertain):
            self.generate()
        record = json.loads((self.root / 'job/receipt.json').read_text())
        self.assertEqual(record['state'], 'pending')
        self.assertEqual(self.opener.open.call_count, 1)

    def test_bad_response_is_uncertain(self):
        self.http.read.return_value = b'{"data": []}'
        with self.assertRaises(images.GenerationUncertain):
            self.generate()

    def test_low_disk_does_not_call_provider(self):
        with patch.object(images.shutil, 'disk_usage', return_value=Mock(free=1)):
            with self.assertRaisesRegex(ValueError, '空间不足'):
                self.generate()
        self.opener.open.assert_not_called()

    def test_reference_cannot_escape(self):
        path = self.root / 'image.jpg'
        path.write_bytes(self.binary)
        with self.assertRaises(ValueError):
            images.reference_data(path, (self.root / 'allowed',))

    def test_reference_thumbnail_is_jpeg_and_has_no_exif(self):
        path = self.root / 'image.jpg'
        path.write_bytes(self.binary)
        value = images.reference_data(path, (self.root,))
        with Image.open(io.BytesIO(base64.b64decode(value.split(',')[1]))) as im:
            self.assertEqual(im.format, 'JPEG')
            self.assertFalse(im.getexif())

    def test_missing_reference_does_not_silently_generate(self):
        catalog = Mock()
        catalog.resolve_snapshot.return_value = None
        with self.assertRaisesRegex(ValueError, '已失效'):
            images.reference_entries(catalog, {'reference_snapshot': [{'id': 'missing'}]})

    def test_references_use_frozen_snapshot_and_deduplicate(self):
        catalog = Mock()
        catalog.resolve_snapshot.return_value = {'kind': 'image', 'source': 'same-image'}
        value = images.reference_entries(catalog, {'reference_snapshot': [{'id': 'frozen', 'role': '形态'}] * 2})
        self.assertEqual(len(value), 1)
        self.assertEqual(value[0]['role'], '形态')

    def test_redirects_disabled(self):
        self.assertIsNone(images.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.invalid'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
