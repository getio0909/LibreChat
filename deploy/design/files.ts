import path from 'node:path';
import { constants } from 'node:fs';
import * as fs from 'node:fs/promises';
import { createHash, createHmac, randomUUID } from 'node:crypto';
import type { Settings } from './config.ts';
import { ToolFailure } from './config.ts';

const types: Record<string, string> = {
  '.pdf': 'application/pdf',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml',
  '.step': 'model/step',
  '.stp': 'model/step',
  '.stl': 'model/stl',
  '.obj': 'model/obj',
  '.ply': 'application/octet-stream',
  '.blend': 'application/octet-stream',
  '.fcstd': 'application/octet-stream',
  '.glb': 'model/gltf-binary',
  '.gltf': 'model/gltf+json',
  '.dxf': 'application/dxf',
  '.igs': 'model/iges',
  '.iges': 'model/iges',
  '.csv': 'text/csv',
};

export function fileType(filename: string): string {
  if (!filename || /[\\/]/.test(filename) || [...filename].some((c) => c.charCodeAt(0) < 32)) {
    throw new ToolFailure('INVALID_FILENAME');
  }
  const type = types[path.extname(filename).toLowerCase()];
  if (!type) throw new ToolFailure('UNSUPPORTED_FILE_TYPE');
  return type;
}

export function fileInfo(settings: Settings, filename: string, bytes: Buffer | number) {
  const fallback = `${settings.workspaceUrl.replace(/\/$/, '')}/${encodeURIComponent(filename)}`;
  let link: {
    download_url: string;
    expires_at?: string;
    download_requires_login?: boolean;
    direct_download_limit_bytes?: number;
  } = {
    download_url: fallback,
    download_requires_login: true,
    direct_download_limit_bytes: settings.maxFileBytes,
  };
  if (
    settings.downloadUrl &&
    settings.downloadSigningKey &&
    settings.userId &&
    Buffer.isBuffer(bytes)
  ) {
    const expires = Math.floor(Date.now() / 1000) + settings.downloadLinkTtlSeconds;
    const ticket = Buffer.from(
      JSON.stringify({
        filename,
        sha256: createHash('sha256').update(bytes).digest('hex'),
        expires,
      }),
    ).toString('base64url');
    const signature = createHmac('sha256', Buffer.from(settings.downloadSigningKey, 'hex'))
      .update(`${settings.userId}\n${ticket}`)
      .digest('base64url');
    const url = new URL(encodeURIComponent(filename), settings.downloadUrl.replace(/\/?$/, '/'));
    url.searchParams.set('ticket', `${ticket}.${signature}`);
    link = { download_url: url.href, expires_at: new Date(expires * 1000).toISOString() };
  }
  return {
    filename,
    bytes: Buffer.isBuffer(bytes) ? bytes.length : bytes,
    path: path.join(settings.workspace, filename),
    ...link,
  };
}

async function workspaceFile(settings: Settings, filename: string) {
  const stat = await fs.lstat(path.join(settings.workspace, filename));
  if (!stat.isFile()) return null;
  if (stat.size > settings.maxFileBytes) return fileInfo(settings, filename, stat.size);
  try {
    return fileInfo(settings, filename, await readWorkspace(settings, filename));
  } catch (error) {
    if (
      error instanceof ToolFailure &&
      ['FILE_IS_CHANGING', 'FILE_TOO_LARGE'].includes(error.code)
    ) {
      // 变动文件仅降级自己的链接，其他稳定文件仍正常列出。
      return { ...fileInfo(settings, filename, stat.size), download_status: error.code };
    }
    throw error;
  }
}

export async function listWorkspace(settings: Settings) {
  const entries = await fs.readdir(settings.workspace, { withFileTypes: true });
  const records = [];
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    try {
      fileType(entry.name);
    } catch {
      continue;
    }
    try {
      const file = await workspaceFile(settings, entry.name);
      if (file) records.push(file);
    } catch (error) {
      if (error instanceof ToolFailure && error.code === 'INVALID_FILE') continue;
      if (
        error instanceof Error &&
        'code' in error &&
        (error.code === 'ENOENT' || error.code === 'ELOOP')
      )
        continue;
      throw error;
    }
  }
  return {
    upload_url: settings.workspaceUrl,
    archive_url: `${settings.workspaceUrl.replace(/\/$/, '')}/?zip`,
    files: records,
  };
}

export async function readWorkspace(settings: Settings, filename: string): Promise<Buffer> {
  fileType(filename);
  const target = path.join(settings.workspace, filename);
  if (!(await fs.lstat(target)).isFile()) throw new ToolFailure('INVALID_FILE');
  // 拒绝符号链接和替换后的特殊文件，目录仅挂载当前用户的工作区。
  const handle = await fs.open(target, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const stat = await handle.stat();
    if (!stat.isFile()) throw new ToolFailure('INVALID_FILE');
    if (stat.size > settings.maxFileBytes) throw new ToolFailure('FILE_TOO_LARGE');
    const buffer = Buffer.alloc(stat.size);
    let position = 0;
    while (position < stat.size) {
      const { bytesRead } = await handle.read(buffer, position, buffer.length - position, position);
      if (!bytesRead) throw new ToolFailure('FILE_IS_CHANGING');
      position += bytesRead;
    }
    const after = await handle.stat();
    if (after.size !== stat.size || after.mtimeMs !== stat.mtimeMs)
      throw new ToolFailure('FILE_IS_CHANGING');
    return buffer;
  } finally {
    await handle.close();
  }
}

export async function writePdf(
  settings: Settings,
  filename: string,
  bytes: Buffer,
  signal: AbortSignal,
  publish: typeof fs.link = fs.link,
): Promise<void> {
  if (fileType(filename) !== 'application/pdf') throw new ToolFailure('PDF_REQUIRED');
  if (bytes.length > settings.maxFileBytes) throw new ToolFailure('FILE_TOO_LARGE');
  signal.throwIfAborted();
  const staged = path.join(settings.workspace, `.${randomUUID()}.tmp`);
  const target = path.join(settings.workspace, filename);
  try {
    await fs.writeFile(staged, bytes, { flag: 'wx', mode: 0o644, signal });
    signal.throwIfAborted();
    // 同目录硬链接原子发布完整文件，并且不会覆盖已有目标。
    await publish(staged, target);
    // 发布后的路径可能已被其他服务替换，取消时保留完整文件而不撤回目标。
    signal.throwIfAborted();
  } catch (error) {
    if (error instanceof Error && 'code' in error && error.code === 'EEXIST')
      throw new ToolFailure('FILE_EXISTS');
    throw error;
  } finally {
    await fs.rm(staged, { force: true });
  }
}
