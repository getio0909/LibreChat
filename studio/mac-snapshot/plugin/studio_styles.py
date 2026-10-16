"""Curated references are identified by note AND original-file checksum."""
import hashlib
import json
from pathlib import Path

MANIFEST = Path(__file__).with_name('studio-styles.json')
_checks = {}
_manifest_cache = None


def manifest():
    global _manifest_cache
    stat = MANIFEST.stat()
    if MANIFEST.is_symlink() or stat.st_size > 100_000:
        raise ValueError('风格素材索引无效')
    key = (str(MANIFEST), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if _manifest_cache and _manifest_cache[0] == key:
        return _manifest_cache[1]
    value = json.loads(MANIFEST.read_text(encoding='utf-8'))
    if value.get('schema') != 1 or not isinstance(value.get('references'), list) or not isinstance(value.get('packs'), list):
        raise ValueError('风格素材索引格式无效')
    _manifest_cache = (key, value)
    return value


def verified_metadata(note, source):
    try:
        row = next((v for v in manifest()['references'] if v['note'] == note), None)
        if row is None:
            return {}
        path = Path(source)
        stat = path.stat()
        key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        digest = _checks.get(key)
        if digest is None:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if len(_checks) > 256:
                _checks.clear()
            _checks[key] = digest
        if digest != row['sha']:
            return {}
        return dict(origin='AI原创概念' if row.get('type') == 'ai_generated' else '可追溯参考',
                    rights=row['license'], credit=row['credit'], source_url=row.get('url', ''),
                    license_url=row.get('license_url', ''), verified_sha256=digest)
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def definition(note):
    try:
        return next((row for row in manifest()['references'] if row['note'] == note), None)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def library(catalog):
    data = manifest()
    definitions = {row['key']: row for row in data['references']}
    result = []
    for pack in data['packs']:
        references = []
        for key in pack['refs']:
            definition = definitions[key]
            ref_id = hashlib.sha256(definition['note'].encode()).hexdigest()[:24]
            try:
                row = catalog.get(ref_id)
                if not row.get('verified_sha256') or row['verified_sha256'] != definition['sha']:
                    continue
                reference = catalog.public(row)
                if definition.get('style_take'):
                    reference['style_take'] = definition['style_take']
                references.append(reference)
            except (OSError, ValueError):
                continue
        result.append({**{k: v for k, v in pack.items() if k != 'refs'}, 'references': references,
                       'available': len(references) == len(pack['refs'])})
    return {'packs': result, 'verified': data['verified'], 'reference_count': sum(len(v['references']) for v in result)}
