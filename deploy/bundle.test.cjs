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
  assert.match(dotEnv, /^OPENROUTER_API_KEY='fixture-openrouter-key'$/m);
  assert.ok(dotEnv.includes(`STUDIO_SHARED_TOKEN='${studioJson.shared_token}'`));

  // 验证 studio.json 包含所有契约规定的配置项。
  assert.deepEqual(studioJson, {
    shared_token: studioJson.shared_token,
    file_signing_key: studioJson.file_signing_key,
    public_base_url: 'https://dev.xuqian.io',
    gemini_api_key: 'fixture-gemini-key',
    ark_api_key: 'fixture-ark-key',
    seedream_model: 'doubao-seedream-5-0-pro-260628',
    seed3d_model: 'doubao-seed3d-2-0-260328',
    planner_model: 'gemini-4-argon',
    planner_thinking_level: 'HIGH',
    max_parallel_per_user: 3,
    seed3d_max_parallel: 5,
    seedream_timeout_seconds: 240,
    planner_timeout_seconds: 540,
  });

  // 验证 DEPLOY_URL 与显式 STUDIO_FILE_SIGNING_KEY 会写入 studio.json。
  const explicitSigningKey = 'c'.repeat(64);
  const customBundle = runBundle({
    DEPLOY_URL: 'https://studio.example.com/',
    STUDIO_FILE_SIGNING_KEY: explicitSigningKey,
  });
  assert.equal(customBundle.result.status, 0, customBundle.result.stderr);
  assert.equal(customBundle.studioJson.public_base_url, 'https://studio.example.com');
  assert.equal(customBundle.studioJson.file_signing_key, explicitSigningKey);

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
  for (const keyName of ['GEMINI_API_KEY', 'OPENROUTER_API_KEY', 'ARK_API_KEY']) {
    for (const invalid of ['', "bad'quote", 'bad\\slash', 'line1\nline2', 'line1\rline2']) {
      const { result } = runBundle({ [keyName]: invalid });
      assert.notEqual(result.status, 0, `${keyName} accepted invalid value: ${invalid}`);
      assert.doesNotMatch(result.stderr, /fixture-.*-key/);
    }
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

test('bundle reuses existing file_signing_key when studio.json already exists in target directory', () => {
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
      env: baseEnv,
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
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

