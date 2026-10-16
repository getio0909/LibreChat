import copy
import ast
import hashlib
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
import urllib.error

from PIL import Image

plugin = Path(os.environ.get('ZYB_PLUGIN_PATH', str(Path(__file__).parent / 'zyb_obsidian_design')))
package = types.ModuleType('vision_test_plugin')
package.__path__ = [str(plugin)]
sys.modules[package.__name__] = package
vision = importlib.import_module(package.__name__ + '.studio_vision')
store = importlib.import_module(package.__name__ + '.studio_store')


class VisionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='zyb-vision-test-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.key = self.root / 'key'
        self.key.write_text('unit-test-not-a-secret')
        self.key.chmod(0o600)
        self.config = self.root / 'vision.json'
        self.values = dict(provider='deepseek', model=vision.MODEL, base_url=vision.BASE_URL,
                           key_file=str(self.key), timeout_seconds=30, reasoning_effort='max',
                           max_tokens=16000, rules='PRIVATE_POLICY_SENTINEL')
        self.configure()
        self.task = {'id': 'a'*32, 'selected': 'b'*32, 'brief': {'goal': '摆件', 'materials': '石材'},
                     'concepts': [{'id': 'b'*32, 'brief_revision': 1, 'form': '圆形', 'materials': '石材'}],
                     'images': [{'id': 'c'*32, 'concept_id': 'b'*32, 'parent_id': None,
                                 'file': '产品设计/' + 'a'*32 + '/image.jpg'}]}
        self.output, self.private = self.root / 'output', self.root / 'private'
        self.path = self.output / self.task['images'][0]['file']
        self.path.parent.mkdir(parents=True)
        Image.new('RGB', (80, 60), (150, 130, 90)).save(self.path)
        self.checks = [{'id': name, 'verdict': '无法判断', 'observation': '局部可见，仍需核验。'} for name in vision.LABELS]
        self.body = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'checks': self.checks}),
                      'reasoning_content': 'PRIVATE_REASONING_SENTINEL'}}], 'model': 'deepseek-v4.1-flash',
                     'usage': {'total_tokens': 100, 'private_field': 'private'}}
        self.opener = Mock()
        self.response = self.opener.open.return_value.__enter__ = Mock(return_value=Mock())
        self.opener.open.return_value.__exit__ = Mock(return_value=False)
        self.http = self.response.return_value

    def configure(self):
        self.config.write_text(json.dumps(self.values))
        self.config.chmod(0o600)

    def run_check(self, **kwargs):
        self.http.read.return_value = json.dumps(self.body).encode()
        with patch.object(vision.urllib.request, 'build_opener', return_value=self.opener):
            return vision.inspect(self.config, self.task, 'c'*32, self.output, self.private, **kwargs)

    def response_checks(self, rows):
        self.body['choices'][0]['message']['content'] = json.dumps({'checks': rows})

    def test_status_contains_no_private_config(self):
        status = vision.vision_status(self.config)
        self.assertTrue(status['vision_ready'])
        self.assertNotIn('key', json.dumps(status))
        self.assertNotIn('rules', status)

    def test_unconfigured_keeps_optional_feature_off(self):
        self.assertFalse(vision.vision_status(self.root / 'absent')['vision_ready'])

    def test_disabled_feature_never_submits(self):
        self.values['enabled'] = False
        self.configure()
        self.assertFalse(vision.vision_status(self.config)['vision_ready'])
        with self.assertRaises(ValueError):
            self.run_check()
        self.opener.open.assert_not_called()

    def test_config_permissions(self):
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            self.run_check()

    def test_key_permissions(self):
        self.key.chmod(0o644)
        with self.assertRaises(ValueError):
            self.run_check()

    def test_endpoint_pinned_before_reading_key(self):
        self.values['base_url'] = 'https://example.invalid'
        self.configure()
        with self.assertRaises(ValueError):
            self.run_check()
        self.opener.open.assert_not_called()

    def test_unsupported_model(self):
        self.values['model'] = 'text-only-model'
        self.configure()
        with self.assertRaises(ValueError):
            self.run_check()

    def test_cross_task_path_rejected(self):
        self.task['id'] = 'd'*32
        with self.assertRaises(ValueError):
            self.run_check()
        self.opener.open.assert_not_called()

    def test_other_concept_rejected(self):
        self.task['selected'] = 'd'*32
        with self.assertRaises(ValueError):
            self.run_check()

    def test_symlink_outside_task_rejected(self):
        outside = self.root / 'outside.jpg'
        self.path.rename(outside)
        self.path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.run_check()

    def test_legacy_basis_not_invented(self):
        value = vision.criteria(self.task, self.task['images'][0], self.private)
        self.assertTrue(value['historical_edits_unknown'])
        self.assertEqual(value['changes'], [])

    def test_new_generation_freezes_basis(self):
        value = vision.generation_basis(self.task, 'b'*32, None, 'generate', '', self.private)
        store.write_private(vision.basis_file(self.private, 'c'*32), json.dumps(value))
        self.task['brief']['materials'] = '金属'
        actual = vision.criteria(self.task, self.task['images'][0], self.private)
        self.assertEqual(actual['brief']['materials'], '石材')
        self.assertFalse(actual['historical_edits_unknown'])

    def test_old_brief_restored_from_exact_revision(self):
        self.task['brief_revision'] = 2
        self.task['brief_history'] = [{'brief': {'goal': '原方向'}}]
        value = vision.generation_basis(self.task, 'b'*32, None, 'generate', '', self.private)
        self.assertEqual(value['brief']['goal'], '原方向')

    def test_missing_historical_brief_not_guessed(self):
        self.task['brief_revision'] = 2
        value = vision.generation_basis(self.task, 'b'*32, None, 'generate', '', self.private)
        self.assertEqual(value['brief'], {})
        self.assertTrue(value['historical_edits_unknown'])

    def test_edit_context_chain_is_private(self):
        value = vision.generation_basis(self.task, 'b'*32, 'c'*32, 'refine', '底座改黑', self.private)
        self.assertEqual(value['changes'], ['底座改黑'])
        self.assertTrue(value['historical_edits_unknown'])
        self.assertNotIn('changes', self.task)

    def test_override_replaces_conflicting_old_basis(self):
        result = self.run_check(override='黑色底座')
        request = json.loads(self.opener.open.call_args.args[0].data)
        criteria = json.loads(request['messages'][1]['content'][0]['text'])['criteria']
        self.assertEqual(criteria['standard'], '黑色底座')
        self.assertNotIn('brief', criteria)
        self.assertNotIn('黑色底座', json.dumps(result, ensure_ascii=False))

    def test_original_hash_and_privacy(self):
        before = self.path.read_bytes()
        result = self.run_check()
        self.assertEqual(result['image_sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.assertNotIn('private_field', result['usage'])
        self.assertEqual(result['reported_model'], 'deepseek-v4.1-flash')
        self.assertNotIn('review', result)

    def test_two_images_only_when_parent_exists(self):
        parent = {**self.task['images'][0], 'id': 'd'*32}
        self.task['images'].append(parent)
        self.task['images'][0]['parent_id'] = parent['id']
        result = self.run_check()
        request = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(len(request['messages'][1]['content']), 4)
        self.assertEqual(result['parent_id'], parent['id'])
        self.assertEqual(result['parent_sha256'], result['image_sha256'])
        self.assertNotIn('tools', request)

    def test_max_effort_and_json_contract(self):
        self.run_check()
        request = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(request['reasoning_effort'], 'max')
        self.assertEqual(request['response_format'], {'type': 'json_object'})
        self.assertEqual(request['messages'][1]['content'][1]['image_url']['detail'], 'original')

    def test_missing_check_rejected(self):
        self.response_checks(self.checks[:-1])
        with self.assertRaises(vision.CheckUncertain):
            self.run_check()

    def test_duplicate_check_rejected(self):
        self.response_checks([self.checks[0]] * 5)
        with self.assertRaises(vision.CheckUncertain):
            self.run_check()

    def test_unknown_verdict_rejected(self):
        self.checks[0]['verdict'] = '可量产'
        self.response_checks(self.checks)
        with self.assertRaises(vision.CheckUncertain):
            self.run_check()

    def test_extra_fields_rejected(self):
        self.checks[0]['internal_prompt'] = 'fixture'
        self.response_checks(self.checks)
        with self.assertRaises(vision.CheckUncertain):
            self.run_check()

    def test_private_policy_echo_rejected(self):
        self.checks[0]['observation'] = self.values['rules']
        self.response_checks(self.checks)
        with self.assertRaises(vision.CheckUncertain):
            self.run_check()

    def test_local_path_rejected(self):
        self.checks[0]['observation'] = '/Users/example/private'
        self.response_checks(self.checks)
        with self.assertRaises(vision.CheckUncertain):
            self.run_check()

    def test_truncated_result_rejected(self):
        self.body['choices'][0]['finish_reason'] = 'length'
        with self.assertRaises(vision.CheckUncertain):
            self.run_check()

    def test_http_errors_redacted_no_retry(self):
        self.opener.open.side_effect = urllib.error.HTTPError(vision.BASE_URL, 401, 'PRIVATE', {}, io.BytesIO(b'PRIVATE'))
        with self.assertRaisesRegex(RuntimeError, '密钥无效') as context:
            self.run_check()
        self.assertNotIn('PRIVATE', str(context.exception))
        self.assertEqual(self.opener.open.call_count, 1)

    def test_timeout_is_uncertain_no_retry(self):
        self.opener.open.side_effect = TimeoutError()
        with self.assertRaises(vision.CheckUncertain):
            self.run_check()
        self.assertEqual(self.opener.open.call_count, 1)

    def test_input_changes_during_call_not_registered(self):
        self.http.read.side_effect = lambda _: (self.path.write_bytes(b'changed'), json.dumps(self.body).encode())[1]
        with self.assertRaises(vision.CheckUncertain):
            self.run_check()

    def test_task_and_manual_review_not_mutated(self):
        self.task['review'] = {'design': '待核验', 'culture': '待核验', 'process': '待核验'}
        before = copy.deepcopy(self.task)
        self.run_check()
        self.assertEqual(self.task, before)

    def test_registered_image_records_private_basis_without_public_change_text(self):
        tree = ast.parse((plugin / 'studio_routes.py').read_text())
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'save_image')
        namespace = {'Path': Path, 'OUTPUT': self.output, 'ROOT': self.root, 'Image': Image, 'json': json,
                     'ident': store.ident, 'stamp': store.stamp, 'write_private': store.write_private,
                     'generation_basis': vision.generation_basis, 'basis_file': vision.basis_file}
        database = store.Store(self.root / 'db')
        task = database.create({'goal': '圆形石材摆件'})
        task['concepts'] = [{'id': 'b'*32, 'brief_revision': 1, 'form': '圆形', 'materials': '石材'}]
        task['selected'] = 'b'*32
        database.put(task, task['revision'])
        namespace['store'] = database
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<save_image>', 'exec'), namespace)
        destination = self.output / '产品设计' / task['id'] / ('e'*32) / 'original.jpg'
        destination.parent.mkdir(parents=True)
        Image.new('RGB', (20, 20)).save(destination)
        options = {'quality': '精细', 'size': '1K', 'aspect': '1:1', 'change': 'PRIVATE_CHANGE_SENTINEL'}
        namespace['save_image'](task['id'], 'b'*32, 'e'*32, None, 'refine', options, destination, {})
        current = database.get(task['id'])
        self.assertNotIn('PRIVATE_CHANGE_SENTINEL', json.dumps(current))
        basis = vision.basis_file(self.private, 'e'*32)
        self.assertEqual(basis.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(basis.read_text())['changes'], ['PRIVATE_CHANGE_SENTINEL'])
        self.assertEqual(current['review']['design'], '待核验')


if __name__ == '__main__':
    unittest.main(verbosity=2)
