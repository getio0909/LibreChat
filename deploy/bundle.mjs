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
function resolvePublicBaseUrl(targetDir) {
  let raw = process.env.DEPLOY_URL;
  if (!raw && targetDir) {
    try {
      const existing = JSON.parse(readFileSync(path.join(targetDir, 'studio.json'), 'utf8'));
      if (typeof existing.public_base_url === 'string') {
        raw = existing.public_base_url;
      }
    } catch {
      // 忽略缺失或损坏的旧配置。
    }
  }
  raw ||= 'https://dev.xuqian.io';
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

// 解析 3D 模型生成开关：仅当 STUDIO_ENABLE_3D 显式设为 true 时启用（如 J5005 测试环境），生产环境默认关闭直至具备出网条件；启用 3D 时必须提供 ARK_API_KEY。
function resolveEnable3d(targetDir) {
  const raw = process.env.STUDIO_ENABLE_3D;
  if (raw === undefined || raw === null || raw.trim() === '') {
    if (targetDir) {
      try {
        const existing = JSON.parse(readFileSync(path.join(targetDir, 'studio.json'), 'utf8'));
        if (typeof existing.enable_3d === 'boolean') {
          return existing.enable_3d;
        }
      } catch {
        // 忽略缺失或损坏的旧配置。
      }
    }
    return false;
  }
  const normalized = raw.trim().toLowerCase();
  if (normalized === 'true') {
    return true;
  }
  if (normalized === 'false') {
    return false;
  }
  throw new Error('STUDIO_ENABLE_3D must be "true" or "false" when provided.');
}

// 解析有界整数配置项；优先读取环境变量，未设置时复用已有 studio.json 中的合法值，否则回退默认值。
function resolveBoundedIntegerSetting(envName, fieldName, fallback, min, max, targetDir) {
  const raw = process.env[envName];
  if (raw !== undefined && raw !== null && String(raw).trim() !== '') {
    const cleaned = String(raw).trim();
    if (!/^\d+$/.test(cleaned)) {
      throw new Error(`${envName} must be an integer between ${min} and ${max}.`);
    }
    const parsed = Number.parseInt(cleaned, 10);
    if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
      throw new Error(`${envName} must be an integer between ${min} and ${max}.`);
    }
    return parsed;
  }
  if (targetDir) {
    const existingStudioPath = path.join(targetDir, 'studio.json');
    if (existsSync(existingStudioPath)) {
      try {
        const existing = JSON.parse(readFileSync(existingStudioPath, 'utf8'));
        const candidate = existing?.[fieldName];
        if (Number.isSafeInteger(candidate) && candidate >= min && candidate <= max) {
          return candidate;
        }
      } catch {
        // 忽略损坏的旧配置并回退默认值。
      }
    }
  }
  return fallback;
}

// 解析有界数值配置项，保留合法的已有部署值。
function resolveBoundedNumberSetting(envName, fieldName, fallback, min, max, targetDir) {
  const raw = process.env[envName];
  if (raw !== undefined && raw !== null && String(raw).trim() !== '') {
    const cleaned = String(raw).trim();
    const parsed = Number(cleaned);
    if (
      !/^\d+(?:\.\d+)?$/.test(cleaned) ||
      !Number.isFinite(parsed) ||
      parsed <= min ||
      parsed > max
    ) {
      throw new Error(`${envName} must be a number greater than ${min} and at most ${max}.`);
    }
    return parsed;
  }
  if (targetDir) {
    const existingStudioPath = path.join(targetDir, 'studio.json');
    if (existsSync(existingStudioPath)) {
      try {
        const existing = JSON.parse(readFileSync(existingStudioPath, 'utf8'));
        const candidate = existing?.[fieldName];
        if (
          typeof candidate === 'number' &&
          Number.isFinite(candidate) &&
          candidate > min &&
          candidate <= max
        ) {
          return candidate;
        }
      } catch {
        // 忽略损坏的旧配置并回退默认值。
      }
    }
  }
  return fallback;
}

const geminiApiKey = requireSafeSingleLineSecret('GEMINI_API_KEY');
const openrouterApiKey = requireSafeSingleLineSecret('OPENROUTER_API_KEY');
const enable3d = resolveEnable3d(target);
const arkApiKey = enable3d
  ? requireSafeSingleLineSecret('ARK_API_KEY')
  : optionalSafeSingleLineSecret('ARK_API_KEY');
const librechatImage = requireImageReference('LIBRECHAT_IMAGE');
const studioImage = requireImageReference('STUDIO_IMAGE');
const sharedToken = resolveStudioSharedToken();
const fileSigningKey = resolveFileSigningKey(target);
const publicBaseUrl = resolvePublicBaseUrl(target);
const fileTokenTtlSeconds = resolveBoundedIntegerSetting(
  'STUDIO_FILE_TOKEN_TTL_SECONDS',
  'file_token_ttl_seconds',
  86400,
  60,
  604800,
  target,
);
const previewTokenTtlSeconds = resolveBoundedIntegerSetting(
  'STUDIO_PREVIEW_TOKEN_TTL_SECONDS',
  'preview_token_ttl_seconds',
  604800,
  300,
  2592000,
  target,
);
const editorTokenTtlSeconds = resolveBoundedIntegerSetting(
  'STUDIO_EDITOR_TOKEN_TTL_SECONDS',
  'editor_token_ttl_seconds',
  900,
  60,
  900,
  target,
);
const editorCapabilityMaxLifetimeSeconds = resolveBoundedIntegerSetting(
  'STUDIO_EDITOR_CAPABILITY_MAX_LIFETIME_SECONDS',
  'editor_capability_max_lifetime_seconds',
  86400,
  900,
  604800,
  target,
);
const maxParallelPerUser = resolveBoundedIntegerSetting(
  'STUDIO_MAX_PARALLEL_PER_USER',
  'max_parallel_per_user',
  3,
  1,
  10,
  target,
);
const globalMaxParallel = resolveBoundedIntegerSetting(
  'STUDIO_GLOBAL_MAX_PARALLEL',
  'global_max_parallel',
  12,
  1,
  64,
  target,
);
const comparisonPreviewMaxBytes = resolveBoundedIntegerSetting(
  'STUDIO_COMPARISON_PREVIEW_MAX_BYTES',
  'comparison_preview_max_bytes',
  48 * 1024,
  16 * 1024,
  48 * 1024,
  target,
);
const segmentMaxPendingRequests = resolveBoundedIntegerSetting(
  'STUDIO_SEGMENT_MAX_PENDING_REQUESTS',
  'segment_max_pending_requests',
  2,
  1,
  32,
  target,
);
const segmentRequestTimeoutSeconds = resolveBoundedNumberSetting(
  'STUDIO_SEGMENT_REQUEST_TIMEOUT_SECONDS',
  'segment_request_timeout_seconds',
  90,
  0,
  300,
  target,
);
const seed3dMaxParallel = resolveBoundedIntegerSetting(
  'STUDIO_SEED3D_MAX_PARALLEL',
  'seed3d_max_parallel',
  5,
  1,
  32,
  target,
);
const seedreamTimeoutSeconds = resolveBoundedIntegerSetting(
  'STUDIO_SEEDREAM_TIMEOUT_SECONDS',
  'seedream_timeout_seconds',
  240,
  5,
  600,
  target,
);
const seed3dPollTimeoutSeconds = resolveBoundedNumberSetting(
  'STUDIO_SEED3D_POLL_TIMEOUT_SECONDS',
  'seed3d_poll_timeout_seconds',
  480,
  0,
  600,
  target,
);
const seed3dPollInitialSeconds = resolveBoundedNumberSetting(
  'STUDIO_SEED3D_POLL_INITIAL_SECONDS',
  'seed3d_poll_initial_seconds',
  2,
  0,
  60,
  target,
);
const seed3dPollMaxIntervalSeconds = resolveBoundedNumberSetting(
  'STUDIO_SEED3D_POLL_MAX_INTERVAL_SECONDS',
  'seed3d_poll_max_interval_seconds',
  15,
  0,
  60,
  target,
);
const seed3dBackgroundTimeoutSeconds = resolveBoundedNumberSetting(
  'STUDIO_SEED3D_BACKGROUND_TIMEOUT_SECONDS',
  'seed3d_background_timeout_seconds',
  1800,
  0,
  3600,
  target,
);
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

// 生成权限为 0600 的 studio.json 配置文件。
const studioSettings = {
  shared_token: sharedToken,
  file_signing_key: fileSigningKey,
  public_base_url: publicBaseUrl,
  file_token_ttl_seconds: fileTokenTtlSeconds,
  preview_token_ttl_seconds: previewTokenTtlSeconds,
  editor_token_ttl_seconds: editorTokenTtlSeconds,
  editor_capability_max_lifetime_seconds: editorCapabilityMaxLifetimeSeconds,
  gemini_api_key: geminiApiKey,
  ark_api_key: arkApiKey,
  image_provider: 'gemini',
  gemini_image_model: 'gemini-nano-banana-2.1',
  enable_3d: enable3d,
  seedream_model: 'doubao-seedream-5-0-pro-260628',
  seed3d_model: 'doubao-seed3d-2-0-260328',
  max_parallel_per_user: maxParallelPerUser,
  global_max_parallel: globalMaxParallel,
  comparison_preview_max_bytes: comparisonPreviewMaxBytes,
  segment_max_pending_requests: segmentMaxPendingRequests,
  segment_request_timeout_seconds: segmentRequestTimeoutSeconds,
  seed3d_max_parallel: seed3dMaxParallel,
  seedream_timeout_seconds: seedreamTimeoutSeconds,
  seed3d_poll_timeout_seconds: seed3dPollTimeoutSeconds,
  seed3d_poll_initial_seconds: seed3dPollInitialSeconds,
  seed3d_poll_max_interval_seconds: seed3dPollMaxIntervalSeconds,
  seed3d_background_timeout_seconds: seed3dBackgroundTimeoutSeconds,
};
writeFileSync(path.join(target, 'studio.json'), `${JSON.stringify(studioSettings, null, 2)}\n`, {
  mode: 0o600,
});

// 写入默认管理员与普通用户账户配置 accounts.json。
writeFileSync(path.join(target, 'accounts.json'), `${JSON.stringify(accounts)}\n`, {
  mode: 0o600,
});
