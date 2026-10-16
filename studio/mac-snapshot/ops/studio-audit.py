import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image

OPS = Path('/Users/ipd/workspace/comfyui-ops')
PLUGIN = Path('/Users/ipd/workspace/ComfyUI/custom_nodes/zyb_obsidian_design')
VAULT = Path('/Users/ipd/obsidian/WIKI')
ARCHIVE = Path('/Users/ipd/Archive')
OUTPUT = Path('/Users/ipd/workspace/ComfyUI/output')
spec = importlib.util.spec_from_file_location('audit_store', PLUGIN / 'studio_store.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
store = module.Store(OPS / 'studio-data')
task = store.get(sys.argv[1])
catalog = module.Catalog(VAULT, ARCHIVE)
result = {'task_id': task['id'], 'is_demo': task.get('is_demo', False), 'images': [], 'references': [], 'notes': []}
private_values = []
runtime = json.loads((OPS / 'studio-runtime.json').read_text())
private_values.extend(value for value in runtime.values() if isinstance(value, str) and value)
for concept in task['concepts']:
    value = json.loads((OPS / 'studio-data/private' / (concept['id'] + '.json')).read_text())
    private_values.append(value['text'])
keys = [Path('/Users/ipd/.config/zyb-design/' + name + '.api').read_text().strip()
        for name in ('deepseek', 'doubao') if Path('/Users/ipd/.config/zyb-design/' + name + '.api').is_file()]
image_config = json.loads((OPS / 'studio-image.json').read_text())
private_values.extend(image_config.get(k, '') for k in ('reference_rules', 'edit_rules') if image_config.get(k))
vision_config = OPS / 'studio-vision.json'
if vision_config.is_file():
    private_values.append(json.loads(vision_config.read_text())['rules'])
public_text = json.dumps(task, ensure_ascii=False)
result['task_private_content_absent'] = all(v not in public_text for v in [*keys, *private_values])
for row in task['references']:
    entry = catalog.get(row['id'])
    result['references'].append({'id': row['id'], 'note_sha256': hashlib.sha256((VAULT / entry['note']).read_bytes()).hexdigest(),
        'source_sha256': hashlib.sha256(Path(entry['source']).read_bytes()).hexdigest()})
for row in task['images']:
    path = OUTPUT / row['file']
    with Image.open(path) as image:
        image.verify()
        receipt = path.parent / 'receipt.json'
        record = json.loads(receipt.read_text()) if receipt.is_file() else {}
        result['images'].append({'id': row['id'], 'size': list(image.size), 'metadata_empty': not image.info,
            'internal_metadata_absent': not any(k.casefold() in ('prompt', 'workflow', 'parameters', 'description') for k in image.info),
            'private_content_absent': all(v.encode() not in path.read_bytes() for v in [*keys, *private_values]),
            'requested_size': record.get('requested_size'), 'receipt_state': record.get('state'),
            'size_matches_request': record.get('requested_size') == f'{image.width}x{image.height}' if record else None,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
for entry in task['archives']:
    note = VAULT / entry['note']
    text = note.read_text()
    links = re.findall(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]', text)
    external = re.findall(r'\]\((file://[^)]+)\)', text)
    files = [Path(unquote(urlparse(link).path)) for link in external]
    result['notes'].append({'file': entry['note'], 'wikilinks_resolve': all((VAULT / (link + '.md')).is_file() for link in links),
         'artifact_links_resolve': all(path.is_file() for path in files),
         'archive_hashes_match': all(path.read_bytes() == (OUTPUT / row['file']).read_bytes()
             for path in files for row in task['images'] if path.stem == row['id']),
         'private_content_absent': all(v not in text for v in [*keys, *private_values]),
         'reference_count': len(links), 'artifact_count': len(files)})
print(json.dumps(result, ensure_ascii=False, indent=2))
