import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { randomBytes } from 'node:crypto';

// 校验必须为单行且不包含单引号、反斜杠或换行的密钥字段。
function requireSafeSingleLineSecret(name) {
  const value = process.env[name];
  if (!value || typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`Missing required environment variable: ${name}`);
  }
  if (/[\r\n'\\]/.test(value)) {
    throw new Error(`${name} must be a single-line value without single quotes or backslashes.`);
  }
  return value;
}

// 校验镜像引用为非空单行字符串且不包含空白或特殊转义字符。
function requireImageReference(name) {
  const value = process.env[name];
  if (!value || typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`Missing required image reference: ${name}`);
  }
  if (/[\r\n'\\\s]/.test(value)) {
    throw new Error(`${name} must be a single-line image reference without whitespace or quotes.`);
  }
  return value;
}

// 校验默认预置账户的邮箱、用户名与密码格式。
function requireAccountConfig(prefix) {
  const email = process.env[`${prefix}_EMAIL`];
  const username = process.env[`${prefix}_USERNAME`];
  const password = process.env[`${prefix}_PASSWORD`];
  if (!email || !email.includes('@') || /[\r\n]/.test(email)) {
    throw new Error(`${prefix}_EMAIL must be a valid single-line email address.`);
  }
  if (!username || username.length < 3 || /[\r\n]/.test(username)) {
    throw new Error(`${prefix}_USERNAME must be a single-line string of at least 3 characters.`);
  }
  if (!password || password.length < 8 || /[\r\n]/.test(password)) {
    throw new Error(`${prefix}_PASSWORD must be a single-line string of at least 8 characters.`);
  }
  return { email, username, password };
}

// 解析或生成至少 32 字节（64 位十六进制）的 Studio 共享认证令牌。
function resolveStudioSharedToken() {
  const provided = process.env.STUDIO_SHARED_TOKEN;
  if (!provided) {
    return randomBytes(32).toString('hex');
  }
  if (/[\r\n'\\]/.test(provided) || provided.length < 64) {
    throw new Error(
      'STUDIO_SHARED_TOKEN must be at least 64 characters without quotes, newlines, or backslashes.',
    );
  }
  return provided;
}

const runnerTemp = process.env.RUNNER_TEMP;
if (!runnerTemp) {
  throw new Error('Missing required environment variable: RUNNER_TEMP');
}

const target = path.join(runnerTemp, 'bundle');
mkdirSync(target, { recursive: true });

const geminiApiKey = requireSafeSingleLineSecret('GEMINI_API_KEY');
const openrouterApiKey = requireSafeSingleLineSecret('OPENROUTER_API_KEY');
const arkApiKey = requireSafeSingleLineSecret('ARK_API_KEY');
const librechatImage = requireImageReference('LIBRECHAT_IMAGE');
const studioImage = requireImageReference('STUDIO_IMAGE');
const sharedToken = resolveStudioSharedToken();
const accounts = {
  admin: requireAccountConfig('ADMIN'),
  user: requireAccountConfig('USER'),
};

// 将基础环境变量与 Google、OpenRouter 及 Studio 共享令牌写入 .env。
const baseEnv = (process.env.LIBRECHAT_ENV || '').replace(/\s+$/, '');
const envLines = [
  ...(baseEnv ? [baseEnv] : []),
  `GOOGLE_KEY='${geminiApiKey}'`,
  `OPENROUTER_API_KEY='${openrouterApiKey}'`,
  `STUDIO_SHARED_TOKEN='${sharedToken}'`,
  '',
];
writeFileSync(path.join(target, '.env'), envLines.join('\n'), { mode: 0o600 });

// 写入不可变镜像引用文件 image.env。
const imageEnvContent = `LIBRECHAT_IMAGE=${librechatImage}\nSTUDIO_IMAGE=${studioImage}\n`;
writeFileSync(path.join(target, 'image.env'), imageEnvContent, { mode: 0o600 });

// 生成权限为 0600 的 studio.json 配置文件。
const studioSettings = {
  shared_token: sharedToken,
  gemini_api_key: geminiApiKey,
  ark_api_key: arkApiKey,
  seedream_model: 'doubao-seedream-5-0-pro-260628',
  planner_model: 'gemini-4-argon',
  planner_thinking_level: 'HIGH',
  max_parallel_per_user: 3,
  seedream_timeout_seconds: 240,
  planner_timeout_seconds: 540,
};
writeFileSync(path.join(target, 'studio.json'), `${JSON.stringify(studioSettings, null, 2)}\n`, {
  mode: 0o600,
});

// 写入默认管理员与普通用户账户配置 accounts.json。
writeFileSync(path.join(target, 'accounts.json'), `${JSON.stringify(accounts)}\n`, {
  mode: 0o600,
});
