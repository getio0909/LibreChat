const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');

// 在临时目录运行 bundle.mjs 并返回退出状态与产物内容。
function runBundle(overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-bundle-'));
  const bundleDir = path.join(root, 'bundle');
  fs.mkdirSync(bundleDir, { recursive: true });
  const env = {
    PATH: process.env.PATH,
    RUNNER_TEMP: root,
    LIBRECHAT_ENV: 'APP_TITLE=LibreChat\nALLOW_REGISTRATION=false',
    GEMINI_API_KEY: 'fixture-gemini-key',
    OPENROUTER_API_KEY: 'fixture-openrouter-key',
    ARK_API_KEY: 'fixture-ark-key',
    LIBRECHAT_IMAGE:
      'ghcr.io/getio0909/librechat-dev@sha256:1111111111111111111111111111111111111111111111111111111111111111',
    STUDIO_IMAGE:
      'ghcr.io/getio0909/librechat-studio:sha-abc123@sha256:2222222222222222222222222222222222222222222222222222222222222222',
    ADMIN_EMAIL: 'admin@example.test',
    ADMIN_USERNAME: 'admin',
    ADMIN_PASSWORD: 'fixture-admin-password',
    USER_EMAIL: 'user@example.test',
    USER_USERNAME: 'user',
    USER_PASSWORD: 'fixture-user-password',
    ...overrides,
  };
  const result = spawnSync(process.execPath, ['deploy/bundle.mjs'], {
    encoding: 'utf8',
    env,
  });

  let dotEnv;
  let imageEnv;
  let studioJson;
  let studioMode;
  let accountsJson;

  if (result.status === 0) {
    dotEnv = fs.readFileSync(path.join(bundleDir, '.env'), 'utf8');
    imageEnv = fs.readFileSync(path.join(bundleDir, 'image.env'), 'utf8');
    const studioPath = path.join(bundleDir, 'studio.json');
    studioJson = JSON.parse(fs.readFileSync(studioPath, 'utf8'));
    studioMode = fs.statSync(studioPath).mode & 0o777;
    accountsJson = JSON.parse(fs.readFileSync(path.join(bundleDir, 'accounts.json'), 'utf8'));
  }

  fs.rmSync(root, { recursive: true, force: true });
  return { result, dotEnv, imageEnv, studioJson, studioMode, accountsJson };
}

test('bundle writes .env additions, 0600 studio.json, image.env, and accounts.json', () => {
  const { result, dotEnv, imageEnv, studioJson, studioMode, accountsJson } = runBundle();
  assert.equal(result.status, 0, result.stderr);
  assert.equal(studioMode, 0o600);

  // 验证生成的共享令牌与文件签名密钥均为 64 位小写十六进制，且共享令牌在 .env 与 studio.json 之间保持一致。
  assert.match(studioJson.shared_token, /^[a-f0-9]{64}$/);
  assert.match(studioJson.file_signing_key, /^[a-f0-9]{64}$/);
  assert.notEqual(studioJson.shared_token, studioJson.file_signing_key);
  assert.match(dotEnv, /^APP_TITLE=LibreChat$/m);
  assert.match(dotEnv, /^GOOGLE_KEY='fixture-gemini-key'$/m);
  assert.match(dotEnv, /^GEMINI_IMAGE_MODEL='gemini-nano-banana-2.1'$/m);
  assert.match(dotEnv, /^OPENROUTER_API_KEY='fixture-openrouter-key'$/m);
  assert.ok(dotEnv.includes(`STUDIO_SHARED_TOKEN='${studioJson.shared_token}'`));

  // 验证 studio.json 包含所有契约规定的配置项（未显式开启 STUDIO_ENABLE_3D 时默认关闭 3D，即便提供了 ARK_API_KEY）。
  assert.deepEqual(studioJson, {
    shared_token: studioJson.shared_token,
    file_signing_key: studioJson.file_signing_key,
    public_base_url: 'https://dev.xuqian.io',
    file_token_ttl_seconds: 86400,
    preview_token_ttl_seconds: 604800,
    editor_token_ttl_seconds: 900,
    editor_capability_max_lifetime_seconds: 86400,
    gemini_api_key: 'fixture-gemini-key',
    ark_api_key: 'fixture-ark-key',
    image_provider: 'gemini',
    gemini_image_model: 'gemini-nano-banana-2.1',
    enable_3d: false,
    seedream_model: 'doubao-seedream-5-0-pro-260628',
    seed3d_model: 'doubao-seed3d-2-0-260328',
    max_parallel_per_user: 3,
    global_max_parallel: 12,
    comparison_preview_max_bytes: 49152,
    segment_max_pending_requests: 2,
    segment_request_timeout_seconds: 90,
    seed3d_max_parallel: 5,
    seedream_timeout_seconds: 240,
    seed3d_poll_timeout_seconds: 480,
    seed3d_poll_initial_seconds: 2,
    seed3d_poll_max_interval_seconds: 15,
    seed3d_background_timeout_seconds: 1800,
  });

  // 验证 3D 关闭时 ARK_API_KEY 为可选项，未提供或为空字符串时 studio.json 中 ark_api_key 为空且 enable_3d 为 false。
  const withoutArk = runBundle({ ARK_API_KEY: '', STUDIO_ENABLE_3D: 'false' });
  assert.equal(withoutArk.result.status, 0, withoutArk.result.stderr);
  assert.equal(withoutArk.studioJson.ark_api_key, '');
  assert.equal(withoutArk.studioJson.image_provider, 'gemini');
  assert.equal(withoutArk.studioJson.enable_3d, false);

  // 验证显式设置 STUDIO_ENABLE_3D=true（如 J5005 测试环境）时开启 3D，且此时缺少 ARK_API_KEY 会被拒绝。
  const with3dEnabled = runBundle({ STUDIO_ENABLE_3D: 'true' });
  assert.equal(with3dEnabled.result.status, 0, with3dEnabled.result.stderr);
  assert.equal(with3dEnabled.studioJson.enable_3d, true);
  assert.equal(with3dEnabled.studioJson.ark_api_key, 'fixture-ark-key');

  const missingArkWhen3dEnabled = runBundle({ STUDIO_ENABLE_3D: 'true', ARK_API_KEY: '' });
  assert.notEqual(missingArkWhen3dEnabled.result.status, 0);
  assert.match(missingArkWhen3dEnabled.result.stderr, /ARK_API_KEY/);

  const invalidEnable3d = runBundle({ STUDIO_ENABLE_3D: 'maybe' });
  assert.notEqual(invalidEnable3d.result.status, 0);

  // 验证 DEPLOY_URL、显式 STUDIO_FILE_SIGNING_KEY 以及三项签名令牌 TTL 覆盖会写入 studio.json。
  const explicitSigningKey = 'c'.repeat(64);
  const customBundle = runBundle({
    DEPLOY_URL: 'https://studio.example.com/',
    STUDIO_FILE_SIGNING_KEY: explicitSigningKey,
    STUDIO_FILE_TOKEN_TTL_SECONDS: '172800',
    STUDIO_PREVIEW_TOKEN_TTL_SECONDS: '1209600',
    STUDIO_EDITOR_TOKEN_TTL_SECONDS: '300',
    STUDIO_EDITOR_CAPABILITY_MAX_LIFETIME_SECONDS: '3600',
    STUDIO_COMPARISON_PREVIEW_MAX_BYTES: '32768',
    STUDIO_SEGMENT_MAX_PENDING_REQUESTS: '3',
    STUDIO_SEGMENT_REQUEST_TIMEOUT_SECONDS: '45.5',
    STUDIO_GLOBAL_MAX_PARALLEL: '16',
    STUDIO_SEED3D_POLL_TIMEOUT_SECONDS: '400.5',
  });
  assert.equal(customBundle.result.status, 0, customBundle.result.stderr);
  assert.equal(customBundle.studioJson.public_base_url, 'https://studio.example.com');
  assert.equal(customBundle.studioJson.file_signing_key, explicitSigningKey);
  assert.equal(customBundle.studioJson.file_token_ttl_seconds, 172800);
  assert.equal(customBundle.studioJson.preview_token_ttl_seconds, 1209600);
  assert.equal(customBundle.studioJson.editor_token_ttl_seconds, 300);
  assert.equal(customBundle.studioJson.editor_capability_max_lifetime_seconds, 3600);
  assert.equal(customBundle.studioJson.comparison_preview_max_bytes, 32768);
  assert.equal(customBundle.studioJson.segment_max_pending_requests, 3);
  assert.equal(customBundle.studioJson.segment_request_timeout_seconds, 45.5);
  assert.equal(customBundle.studioJson.global_max_parallel, 16);
  assert.equal(customBundle.studioJson.seed3d_poll_timeout_seconds, 400.5);

  // 验证越界或非整数 TTL 配置被 bundle.mjs 拒绝。
  for (const badOverride of [
    { STUDIO_FILE_TOKEN_TTL_SECONDS: '59' },
    { STUDIO_FILE_TOKEN_TTL_SECONDS: '604801' },
    { STUDIO_PREVIEW_TOKEN_TTL_SECONDS: '299' },
    { STUDIO_PREVIEW_TOKEN_TTL_SECONDS: '2592001' },
    { STUDIO_EDITOR_TOKEN_TTL_SECONDS: '59' },
    { STUDIO_EDITOR_TOKEN_TTL_SECONDS: '901' },
    { STUDIO_PREVIEW_TOKEN_TTL_SECONDS: 'not-a-number' },
    { STUDIO_COMPARISON_PREVIEW_MAX_BYTES: '16383' },
    { STUDIO_COMPARISON_PREVIEW_MAX_BYTES: '49153' },
    { STUDIO_SEGMENT_MAX_PENDING_REQUESTS: '0' },
    { STUDIO_SEGMENT_REQUEST_TIMEOUT_SECONDS: '0' },
    { STUDIO_EDITOR_CAPABILITY_MAX_LIFETIME_SECONDS: '899' },
  ]) {
    assert.notEqual(runBundle(badOverride).result.status, 0);
  }

  // 验证 image.env 同时包含 LIBRECHAT_IMAGE 和 STUDIO_IMAGE。
  assert.match(imageEnv, /^LIBRECHAT_IMAGE=ghcr\.io\/getio0909\/librechat-dev@sha256:1{64}$/m);
  assert.match(
    imageEnv,
    /^STUDIO_IMAGE=ghcr\.io\/getio0909\/librechat-studio:sha-abc123@sha256:2{64}$/m,
  );

  // 验证预置账户结构。
  assert.equal(accountsJson.admin.email, 'admin@example.test');
  assert.equal(accountsJson.user.email, 'user@example.test');
});

test('bundle rejects multi-line, single-quoted, or backslash secrets without leaking values', () => {
  for (const keyName of ['GEMINI_API_KEY', 'OPENROUTER_API_KEY']) {
    for (const invalid of ['', "bad'quote", 'bad\\slash', 'line1\nline2', 'line1\rline2']) {
      const { result } = runBundle({ [keyName]: invalid });
      assert.notEqual(result.status, 0, `${keyName} accepted invalid value: ${invalid}`);
      assert.doesNotMatch(result.stderr, /fixture-.*-key/);
    }
  }
  for (const invalidArk of ['   ', "bad'quote", 'bad\\slash', 'line1\nline2', 'line1\rline2']) {
    const { result } = runBundle({ ARK_API_KEY: invalidArk });
    assert.notEqual(result.status, 0, `ARK_API_KEY accepted invalid value: ${invalidArk}`);
    assert.doesNotMatch(result.stderr, /fixture-.*-key/);
  }
});

test('bundle rejects missing or malformed image references, URLs, signing keys, and account credentials', () => {
  for (const field of ['LIBRECHAT_IMAGE', 'STUDIO_IMAGE']) {
    assert.notEqual(runBundle({ [field]: '' }).result.status, 0);
    assert.notEqual(runBundle({ [field]: 'image with spaces' }).result.status, 0);
  }
  assert.notEqual(runBundle({ STUDIO_FILE_SIGNING_KEY: 'not-64-hex' }).result.status, 0);
  assert.notEqual(runBundle({ DEPLOY_URL: 'ftp://invalid.example.com' }).result.status, 0);
  assert.notEqual(runBundle({ ADMIN_EMAIL: 'invalid-email' }).result.status, 0);
  assert.notEqual(runBundle({ USER_PASSWORD: 'short' }).result.status, 0);
});

test('bundle preserves validated Studio settings when studio.json already exists in target directory', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-bundle-reuse-'));
  try {
    const baseEnv = {
      PATH: process.env.PATH,
      RUNNER_TEMP: root,
      LIBRECHAT_ENV: 'APP_TITLE=LibreChat',
      GEMINI_API_KEY: 'fixture-gemini-key',
      OPENROUTER_API_KEY: 'fixture-openrouter-key',
      ARK_API_KEY: 'fixture-ark-key',
      LIBRECHAT_IMAGE:
        'ghcr.io/getio0909/librechat-dev@sha256:1111111111111111111111111111111111111111111111111111111111111111',
      STUDIO_IMAGE:
        'ghcr.io/getio0909/librechat-studio:sha-abc123@sha256:2222222222222222222222222222222222222222222222222222222222222222',
      ADMIN_EMAIL: 'admin@example.test',
      ADMIN_USERNAME: 'admin',
      ADMIN_PASSWORD: 'fixture-admin-password',
      USER_EMAIL: 'user@example.test',
      USER_USERNAME: 'user',
      USER_PASSWORD: 'fixture-user-password',
    };
    const first = spawnSync(process.execPath, ['deploy/bundle.mjs'], {
      encoding: 'utf8',
      env: {
        ...baseEnv,
        STUDIO_FILE_TOKEN_TTL_SECONDS: '172800',
        STUDIO_PREVIEW_TOKEN_TTL_SECONDS: '1209600',
        STUDIO_EDITOR_TOKEN_TTL_SECONDS: '450',
        STUDIO_ENABLE_3D: 'true',
        DEPLOY_URL: 'https://studio.example.test/',
        STUDIO_COMPARISON_PREVIEW_MAX_BYTES: '32768',
        STUDIO_SEGMENT_MAX_PENDING_REQUESTS: '4',
        STUDIO_SEED3D_POLL_INITIAL_SECONDS: '3.5',
      },
    });
    assert.equal(first.status, 0, first.stderr);
    const studioPath = path.join(root, 'bundle', 'studio.json');
    const firstStudio = JSON.parse(fs.readFileSync(studioPath, 'utf8'));

    const second = spawnSync(process.execPath, ['deploy/bundle.mjs'], {
      encoding: 'utf8',
      env: baseEnv,
    });
    assert.equal(second.status, 0, second.stderr);
    const secondStudio = JSON.parse(fs.readFileSync(studioPath, 'utf8'));

    assert.equal(secondStudio.file_signing_key, firstStudio.file_signing_key);
    assert.notEqual(secondStudio.shared_token, firstStudio.shared_token);
    assert.equal(secondStudio.enable_3d, true);
    assert.equal(secondStudio.public_base_url, 'https://studio.example.test');
    assert.equal(secondStudio.file_token_ttl_seconds, 172800);
    assert.equal(secondStudio.preview_token_ttl_seconds, 1209600);
    assert.equal(secondStudio.editor_token_ttl_seconds, 450);
    assert.equal(secondStudio.comparison_preview_max_bytes, 32768);
    assert.equal(secondStudio.segment_max_pending_requests, 4);
    assert.equal(secondStudio.seed3d_poll_initial_seconds, 3.5);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
