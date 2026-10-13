import { writeFileSync } from 'node:fs';
import path from 'node:path';
import { randomBytes } from 'node:crypto';

const target = path.join(process.env.RUNNER_TEMP, 'bundle');
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
