import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { parseEnv } from 'node:util';
import { randomBytes } from 'node:crypto';

const deploymentUrl = new URL(process.env.DEPLOY_URL);
if (deploymentUrl.protocol !== 'https:' || deploymentUrl.username || deploymentUrl.password) {
  throw new Error('DEPLOY_URL must be an HTTPS URL without credentials.');
}
const downloadLinkTtlSeconds = Number(process.env.CAD_DOWNLOAD_LINK_TTL_SECONDS || 86400);
if (
  !Number.isInteger(downloadLinkTtlSeconds) ||
  downloadLinkTtlSeconds < 60 ||
  downloadLinkTtlSeconds > 7 * 86400
) {
  throw new Error('CAD_DOWNLOAD_LINK_TTL_SECONDS must be an integer between 60 and 604800.');
}
const target = path.join(process.env.RUNNER_TEMP, 'bundle');
const maxPdfPages = Number(process.env.CAD_MAX_PDF_PAGES || 32);
if (!Number.isInteger(maxPdfPages) || maxPdfPages < 1 || maxPdfPages > 128) {
  throw new Error('CAD_MAX_PDF_PAGES must be an integer between 1 and 128.');
}
const hostImageLimit = Number(parseEnv(process.env.LIBRECHAT_ENV || '').MCP_IMAGE_DATA_MAX_BYTES);
const maxImageBytes =
  Number.isSafeInteger(hostImageLimit) && hostImageLimit > 0
    ? Math.min(10 * 1024 * 1024, hostImageLimit)
    : 10 * 1024 * 1024;
const accounts = {};
for (const account of ['admin', 'user']) {
  const prefix = account.toUpperCase();
  const values = {
    email: process.env[`${prefix}_EMAIL`],
    username: process.env[`${prefix}_USERNAME`],
    password: process.env[`${prefix}_PASSWORD`],
  };
  if (
    !values.username ||
    !values.password ||
    /[:@|\r\n]/.test(values.username) ||
    /[@|\r\n]/.test(values.password)
  ) {
    throw new Error('CAD file credentials contain unsupported delimiter characters.');
  }
  accounts[account] = values;
  writeFileSync(
    path.join(target, `design-${account}.json`),
    JSON.stringify({
      workspaceUrl: `${process.env.DEPLOY_URL.replace(/\/$/, '')}/cad-files/${account}/`,
      downloadUrl: `${process.env.DEPLOY_URL.replace(/\/$/, '')}/cad-downloads/${account}/`,
      downloadSigningKey: randomBytes(32).toString('hex'),
      downloadLinkTtlSeconds,
      maxImageBytes,
      maxPdfPages,
      arkApiKey: process.env.ARK_API_KEY,
      geminiApiKey: process.env.GEMINI_API_KEY,
    }),
    { mode: 0o600 },
  );
  writeFileSync(
    path.join(target, `files-${account}.yaml`),
    JSON.stringify({
      'serve-path': '/data',
      bind: '0.0.0.0',
      port: 5000,
      'path-prefix': `/cad-files/${account}`,
      auth: [`${values.username}:${values.password}@/:rw`],
      'allow-upload': true,
      'allow-delete': true,
      'allow-search': true,
      'allow-archive': true,
      'allow-symlink': false,
      'log-format': '$remote_addr "$request" $status',
    }),
    { mode: 0o600 },
  );
}
writeFileSync(path.join(target, 'accounts.json'), JSON.stringify(accounts), { mode: 0o600 });
