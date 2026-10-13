const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');

function bundle(overrides) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cad-bundle-'));
  fs.mkdirSync(path.join(root, 'bundle'));
  const result = spawnSync(process.execPath, ['deploy/bundle.mjs'], {
    encoding: 'utf8',
    env: {
      ...process.env,
      RUNNER_TEMP: root,
      DEPLOY_URL: 'https://example.test',
      CAD_DOWNLOAD_LINK_TTL_SECONDS: '',
      LIBRECHAT_ENV: '',
      ADMIN_EMAIL: 'admin@example.test',
      ADMIN_USERNAME: 'admin',
      ADMIN_PASSWORD: 'fixture-admin-password',
      USER_EMAIL: 'user@example.test',
      USER_USERNAME: 'user',
      USER_PASSWORD: 'fixture-user-password',
      ARK_API_KEY: '',
      GEMINI_API_KEY: '',
      ...overrides,
    },
  });
  const settings =
    result.status === 0
      ? JSON.parse(fs.readFileSync(path.join(root, 'bundle', 'design-user.json'), 'utf8'))
      : undefined;
  fs.rmSync(root, { recursive: true, force: true });
  return { result, settings };
}

test('public downloads require HTTPS before any credentials are bundled', () => {
  const { result } = bundle({ DEPLOY_URL: 'http://example.test' });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must be an HTTPS URL/);
  assert.doesNotMatch(result.stderr, /fixture-.*-password/);
});

test('the development environment can configure download expiry with a 24-hour default', () => {
  assert.equal(bundle({}).settings.downloadLinkTtlSeconds, 86400);
  const custom = bundle({ CAD_DOWNLOAD_LINK_TTL_SECONDS: '3600' });
  assert.equal(custom.result.status, 0);
  assert.equal(custom.settings.downloadLinkTtlSeconds, 3600);
  assert.equal(custom.settings.downloadUrl, 'https://example.test/cad-downloads/user/');
  assert.match(custom.settings.downloadSigningKey, /^[a-f0-9]{64}$/);
  for (const value of ['59', '604801', 'invalid', '60.5']) {
    assert.notEqual(bundle({ CAD_DOWNLOAD_LINK_TTL_SECONDS: value }).result.status, 0);
  }
});

test('design images share the host MCP byte limit before PDFs are committed', () => {
  assert.equal(bundle({}).settings.maxImageBytes, 10 * 1024 * 1024);
  for (const value of ['8192', '"8192"']) {
    assert.equal(
      bundle({ LIBRECHAT_ENV: `MCP_IMAGE_DATA_MAX_BYTES=${value}\n` }).settings.maxImageBytes,
      8192,
    );
  }
  for (const value of ['0', 'invalid', '1.5', '999999999']) {
    assert.equal(
      bundle({ LIBRECHAT_ENV: `MCP_IMAGE_DATA_MAX_BYTES=${value}\n` }).settings.maxImageBytes,
      10 * 1024 * 1024,
    );
  }
});
