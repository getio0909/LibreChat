import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
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

// 校验可选的单行密钥字段；未设置或为空字符串时返回空字符串，非空时禁止空白或非法转义字符。
function optionalSafeSingleLineSecret(name) {
  const value = process.env[name];
  if (value === undefined || value === null || value === '') {
    return '';
  }
  if (typeof value !== 'string' || value.trim().length === 0) {
    throw new Error(`${name} must not be whitespace-only when provided.`);
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

// 解析或生成 32 字节（64 位十六进制）的三维模型文件 HMAC 签名密钥；若目标目录已存在有效密钥则优先复用。
function resolveFileSigningKey(targetDir) {
  const provided = process.env.STUDIO_FILE_SIGNING_KEY;
  if (!provided) {
    if (targetDir) {
      const existingStudioPath = path.join(targetDir, 'studio.json');
      if (existsSync(existingStudioPath)) {
        try {
          const existing = JSON.parse(readFileSync(existingStudioPath, 'utf8'));
          if (
            typeof existing.file_signing_key === 'string' &&
            /^[0-9a-fA-F]{64}$/.test(existing.file_signing_key)
          ) {
            return existing.file_signing_key;
          }
        } catch {
          // 忽略损坏的旧配置并生成新密钥。
        }
      }
    }
    return randomBytes(32).toString('hex');
  }
  if (!/^[0-9a-fA-F]{64}$/.test(provided)) {
    throw new Error('STUDIO_FILE_SIGNING_KEY must be an exact 64-character hexadecimal string.');
  }
  return provided;
}

// 解析公开部署基础 URL（来自 DEPLOY_URL，用于拼接签名下载链接）。
function resolvePublicBaseUrl() {
  const raw = process.env.DEPLOY_URL || 'https://dev.xuqian.io';
  const cleaned = raw.trim().replace(/\/+$/, '');
  if (!/^https?:\/\/[^\s'\\]+$/.test(cleaned)) {
    throw new Error('DEPLOY_URL must be a valid single-line HTTP(S) URL.');
  }
  return cleaned;
}

const runnerTemp = process.env.RUNNER_TEMP;
if (!runnerTemp) {
  throw new Error('Missing required environment variable: RUNNER_TEMP');
}

const target = path.join(runnerTemp, 'bundle');
mkdirSync(target, { recursive: true });

const geminiApiKey = requireSafeSingleLineSecret('GEMINI_API_KEY');
const openrouterApiKey = requireSafeSingleLineSecret('OPENROUTER_API_KEY');
const arkApiKey = optionalSafeSingleLineSecret('ARK_API_KEY');
const librechatImage = requireImageReference('LIBRECHAT_IMAGE');
const studioImage = requireImageReference('STUDIO_IMAGE');
const sharedToken = resolveStudioSharedToken();
const fileSigningKey = resolveFileSigningKey(target);
const publicBaseUrl = resolvePublicBaseUrl();
const accounts = {
  admin: requireAccountConfig('ADMIN'),
  user: requireAccountConfig('USER'),
};

// 将基础环境变量与 Google、OpenRouter、内置生图模型及 Studio 共享令牌写入 .env。
const baseEnv = (process.env.LIBRECHAT_ENV || '').replace(/\s+$/, '');
const envLines = [
  ...(baseEnv ? [baseEnv] : []),
  `GOOGLE_KEY='${geminiApiKey}'`,
  `GEMINI_IMAGE_MODEL='gemini-nano-banana-2.1'`,
  `OPENROUTER_API_KEY='${openrouterApiKey}'`,
  `STUDIO_SHARED_TOKEN='${sharedToken}'`,
  '',
];
writeFileSync(path.join(target, '.env'), envLines.join('\n'), { mode: 0o600 });

// 写入不可变镜像引用文件 image.env。
const imageEnvContent = `LIBRECHAT_IMAGE=${librechatImage}\nSTUDIO_IMAGE=${studioImage}\n`;
writeFileSync(path.join(target, 'image.env'), imageEnvContent, { mode: 0o600 });

// 当配置了火山方舟密钥（如 J5005 测试环境）时默认启用 3D 模型生成，也可通过 STUDIO_ENABLE_3D 显式覆盖。
const enable3d =
  process.env.STUDIO_ENABLE_3D !== undefined
    ? process.env.STUDIO_ENABLE_3D === 'true'
    : Boolean(arkApiKey);

// 生成权限为 0600 的 studio.json 配置文件。
const studioSettings = {
  shared_token: sharedToken,
  file_signing_key: fileSigningKey,
  public_base_url: publicBaseUrl,
  gemini_api_key: geminiApiKey,
  ark_api_key: arkApiKey,
  image_provider: 'gemini',
  gemini_image_model: 'gemini-nano-banana-2.1',
  enable_3d: enable3d,
  seedream_model: 'doubao-seedream-5-0-pro-260628',
  seed3d_model: 'doubao-seed3d-2-0-260328',
  max_parallel_per_user: 3,
  seed3d_max_parallel: 5,
  seedream_timeout_seconds: 240,
};
writeFileSync(path.join(target, 'studio.json'), `${JSON.stringify(studioSettings, null, 2)}\n`, {
  mode: 0o600,
});

// 写入默认管理员与普通用户账户配置 accounts.json。
writeFileSync(path.join(target, 'accounts.json'), `${JSON.stringify(accounts)}\n`, {
  mode: 0o600,
});
