import path from 'node:path';
import { constants } from 'node:fs';
import * as fs from 'node:fs/promises';
import { createHash, createHmac } from 'node:crypto';
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

export function fileInfo(settings: Settings, filename: string, bytes: Buffer) {
  const fallback = `${settings.workspaceUrl.replace(/\/$/, '')}/${encodeURIComponent(filename)}`;
  let link: { download_url: string; expires_at?: string } = { download_url: fallback };
  if (settings.downloadUrl && settings.downloadSigningKey && settings.userId) {
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
    bytes: bytes.length,
    path: path.join(settings.workspace, filename),
    ...link,
  };
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
    const stat = await fs.lstat(path.join(settings.workspace, entry.name));
    if (stat.isFile() && stat.size <= settings.maxFileBytes)
      records.push(fileInfo(settings, entry.name, await readWorkspace(settings, entry.name)));
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
