import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path('/Users/ipd/workspace/comfyui-ops')


def main():
    raw = sys.stdin.buffer.read(196609)
    if len(raw) > 196608:
        raise ValueError('input_limit')
    request = json.loads(raw)
    runtime = json.loads((ROOT / 'studio-runtime.json').read_text())
    config = json.loads((ROOT / 'studio-llm.json').read_text())
    messages = [{'role': 'system', 'content': runtime['planner_system'] + '\n' + runtime.get('planner_rules', '')},
                {'role': 'user', 'content': json.dumps(request, ensure_ascii=False)}]
    if not request.get('references'):
        policy_path = ROOT / 'studio-creation-policy.json'
        if policy_path.is_symlink() or stat.S_IMODE(policy_path.stat().st_mode) != 0o600:
            raise ValueError('creation_policy_permissions')
        policy = json.loads(policy_path.read_text())
        messages[0]['content'] += '\n' + policy['no_reference_rules']
    if config['provider'] == 'deepseek':
        if config['base_url'] != 'https://api.deepseek.com':
            raise ValueError('endpoint_not_allowed')
        key_path = Path(config['key_file'])
        if key_path.is_symlink() or stat.S_IMODE(key_path.stat().st_mode) != 0o600:
            raise ValueError('key_permissions')
        key = key_path.read_text().strip()
        if not key or any(c.isspace() for c in key):
            raise ValueError('key_format')
        body = {'model': config['model'], 'messages': messages,
                'thinking': {'type': 'enabled'}, 'reasoning_effort': config['reasoning_effort'],
                'max_tokens': min(int(config['max_tokens']), 24000),
                'response_format': {'type': 'json_object'}, 'stream': False}
        req = urllib.request.Request(config['base_url'] + '/chat/completions',
              data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
              'Authorization': 'Bearer ' + key}, method='POST')
        try:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            with urllib.request.build_opener(NoRedirect).open(req, timeout=min(int(config['timeout_seconds']), 600)) as response:
                raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError('response_limit')
            result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise ValueError('provider_http_' + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError):
            raise ValueError('provider_unavailable') from None
        choice = result['choices'][0]
        if choice.get('finish_reason') != 'stop':
            raise ValueError('incomplete_response')
        text = choice['message']['content']
        result.clear()
    elif config['provider'] == 'local':
        os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
        import mlx.core as mx
        from mlx_lm import generate, load
        from mlx_lm.sample_utils import make_sampler
        mx.set_memory_limit(12 * 1024**3)
        mx.set_cache_limit(512 * 1024**2)
        mx.random.seed(91415)
        model, tokenizer = load(str(ROOT / 'studio-model/qwen35-9b'), tokenizer_config={'trust_remote_code': False})
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
        text = generate(model, tokenizer, prompt=prompt, max_tokens=4000,
                        sampler=make_sampler(temp=0.55, top_p=0.9), verbose=False)
        if '</think>' in text:
            text = text.split('</think>', 1)[1]
    else:
        raise ValueError('provider_not_allowed')
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        raise ValueError('invalid_json')
    print(json.dumps(json.loads(text[start:end+1]), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        safe = str(exc) if isinstance(exc, ValueError) else ''
        allowed = {'input_limit', 'endpoint_not_allowed', 'key_permissions', 'key_format',
                   'response_limit', 'provider_unavailable', 'incomplete_response',
                   'provider_not_allowed', 'invalid_json', 'creation_policy_permissions'}
        if safe not in allowed and safe not in {'provider_http_' + str(c) for c in (400, 401, 402, 403, 422, 429, 500, 502, 503, 504)}:
            safe = 'planner_failed'
        sys.stderr.write(safe + '\n')
        sys.exit(1)
