import { z } from 'zod';
import { createHash, createHmac, timingSafeEqual } from 'node:crypto';
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Settings } from './config.ts';
import { fileType, readWorkspace } from './files.ts';

const ticketSchema = z
  .object({
    filename: z.string(),
    sha256: z.string().regex(/^[a-f0-9]{64}$/),
    expires: z.number().int().positive(),
  })
  .strict();

function signature(settings: Settings, encoded: string): Buffer {
  return createHmac('sha256', Buffer.from(settings.downloadSigningKey, 'hex'))
    .update(`${settings.userId}\n${encoded}`)
    .digest();
}

export async function serveDownload(settings: Settings, req: IncomingMessage, res: ServerResponse) {
  res.setHeader('Cache-Control', 'private, no-store');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader('Content-Security-Policy', "sandbox; default-src 'none'");
  res.setHeader('X-Content-Type-Options', 'nosniff');
  if (!['GET', 'HEAD'].includes(req.method ?? '')) {
    res.writeHead(405, { Allow: 'GET, HEAD' });
    res.end();
    return;
  }
  let ticket;
  try {
    if (!settings.downloadSigningKey || !settings.downloadUrl || !settings.userId)
      throw new Error();
    const url = new URL(req.url ?? '', 'http://localhost');
    if (url.searchParams.size !== 1 || url.searchParams.getAll('ticket').length !== 1)
      throw new Error();
    const raw = url.searchParams.get('ticket') ?? '';
    if (raw.length > 2048) throw new Error();
    const parts = raw.split('.');
    if (parts.length !== 2 || !/^[A-Za-z0-9_-]{43}$/.test(parts[1])) throw new Error();
    if (!timingSafeEqual(signature(settings, parts[0]), Buffer.from(parts[1], 'base64url')))
      throw new Error();
    ticket = ticketSchema.parse(JSON.parse(Buffer.from(parts[0], 'base64url').toString('utf8')));
    if (
      ticket.expires <= Math.floor(Date.now() / 1000) ||
      ticket.expires > Math.floor(Date.now() / 1000) + settings.downloadLinkTtlSeconds ||
      decodeURIComponent(url.pathname.slice('/download/'.length)) !== ticket.filename
    )
      throw new Error();
    fileType(ticket.filename);
  } catch {
    res.writeHead(403);
    res.end('Download link is invalid or expired. Request a new link in the chat.');
    return;
  }
  try {
    // 签名同时绑定用户、文件名和内容；修改文件后旧链接不能下载新版本。
    const bytes = await readWorkspace(settings, ticket.filename);
    if (createHash('sha256').update(bytes).digest('hex') !== ticket.sha256) {
      res.writeHead(409);
      res.end('The file has changed. Request a new link in the chat.');
      return;
    }
    res.writeHead(200, {
      'Content-Type': fileType(ticket.filename),
      'Content-Length': bytes.length,
      'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(ticket.filename)}`,
    });
    res.end(req.method === 'HEAD' ? undefined : bytes);
  } catch {
    res.writeHead(404);
    res.end('The file is unavailable. Request a new export in the chat.');
  }
}
