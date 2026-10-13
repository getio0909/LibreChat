import os from 'node:os';
import path from 'node:path';
import sharp from 'sharp';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { connect } from 'node:net';
import { test, afterEach } from 'node:test';
import { inflateSync } from 'node:zlib';
import { PDFArray, PDFDocument, PDFRawStream } from 'pdf-lib';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { CallToolResultSchema } from '@modelcontextprotocol/sdk/types.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import type { Server } from 'node:http';
import type { FileHandle } from 'node:fs/promises';
import type { Settings, Request } from './config.ts';
import { settingsSchema, ToolFailure, boundedBody } from './config.ts';
import { fileType, readWorkspace } from './files.ts';
import { createHandler } from './mcp.ts';
import { imageProviders, compactImage } from './images.ts';

const fixtures: { server: Server; root: string; client: Client }[] = [];
afterEach(async () => {
  for (const fixture of fixtures.splice(0)) {
    await fixture.client.close();
    fixture.server.closeAllConnections();
    await new Promise<void>((resolve) => fixture.server.close(() => resolve()));
    await fs.rm(fixture.root, { recursive: true, force: true });
  }
});

async function fixture(overrides: Partial<Settings> = {}, request?: Request) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'design-mcp-'));
  const settings = settingsSchema.parse({
    userId: 'a'.repeat(24),
    workspace: root,
    workspaceUrl: 'https://example.test/cad-files/admin/',
    ...overrides,
  });
  const server = createServer(createHandler(settings, imageProviders(settings, request)));
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert.ok(address && typeof address === 'object');
  const url = new URL(`http://127.0.0.1:${address.port}/mcp`);
  const client = new Client({ name: 'design-test', version: '1.0.0' });
  fixtures.push({ server, root, client });
  await client.connect(
    new StreamableHTTPClientTransport(url, {
      requestInit: { headers: { 'X-LibreChat-User-ID': settings.userId } },
    }),
  );
  return { root, settings, client, url, server };
}

test('private workspace rejects other identities and lists only usable filenames', async () => {
  const a = await fixture();
  const b = await fixture({
    userId: 'b'.repeat(24),
    workspaceUrl: 'https://example.test/cad-files/user/',
  });
  await fs.writeFile(path.join(a.root, 'part.step'), 'admin geometry');
  await fs.writeFile(path.join(b.root, 'part.step'), 'user geometry');
  await fs.writeFile(path.join(a.root, 'private.env'), 'never published');
  if (process.platform !== 'win32')
    await fs.writeFile(path.join(a.root, 'bad\\name.step'), 'hidden');
  const forbidden = await fetch(a.url, {
    method: 'POST',
    headers: { 'X-LibreChat-User-ID': b.settings.userId },
  });
  assert.equal(forbidden.status, 403);
  const result = await a.client.callTool({ name: 'workspace_files', arguments: {} });
  assert.match(JSON.stringify(result), /part\.step/);
  assert.doesNotMatch(JSON.stringify(result), /private|bad/);
  assert.equal((await readWorkspace(a.settings, 'part.step')).toString(), 'admin geometry');
  assert.equal((await readWorkspace(b.settings, 'part.step')).toString(), 'user geometry');
  assert.throws(() => fileType('../part.step'), ToolFailure);
  await fs.symlink(
    process.platform === 'win32' ? b.root : path.join(b.root, 'part.step'),
    path.join(a.root, 'link.step'),
    process.platform === 'win32' ? 'junction' : 'file',
  );
  await assert.rejects(readWorkspace(a.settings, 'link.step'));
});

test('exports a real PDF with a download URL and preserves an existing destination', async () => {
  const f = await fixture();
  await fs.writeFile(
    path.join(f.root, 'drawing.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="800"><rect x="200" y="200" width="800" height="400" fill="none" stroke="black" stroke-width="4"/><text x="600" y="160" font-size="40" text-anchor="middle">CAD export verification</text></svg>',
  );
  const result = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: { source_filename: 'drawing.svg', pdf_filename: 'drawing.pdf' },
  });
  assert.equal(result.isError, undefined);
  assert.match(JSON.stringify(result), /https:\/\/example.test\/cad-files\/admin\/drawing.pdf/);
  const bytes = await fs.readFile(path.join(f.root, 'drawing.pdf'));
  const pdf = await PDFDocument.load(bytes);
  assert.equal(pdf.getPageCount(), 1);
  assert.ok(pdf.getPage(0).getWidth() > pdf.getPage(0).getHeight());
  const repeated = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: { source_filename: 'drawing.svg', pdf_filename: 'drawing.pdf' },
  });
  assert.equal(repeated.isError, true);
  assert.deepEqual(await fs.readFile(path.join(f.root, 'drawing.pdf')), bytes);
});

test('scaled SVG sheets preserve A3 paper and reject implicit resizing or incomplete dimensions', async () => {
  const f = await fixture();
  await fs.writeFile(
    path.join(f.root, 'sheet.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm" viewBox="0 0 420 297"><rect x="10" y="10" width="400" height="277" fill="white" stroke="black"/><path d="M20 50h100" stroke="black" stroke-width="1"/><text x="20" y="40" font-size="5">100 mm - Scale 1:1</text></svg>',
  );
  const result = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: {
      source_filename: 'sheet.svg',
      pdf_filename: 'sheet.pdf',
      page_width_mm: 420,
      page_height_mm: 297,
    },
  });
  assert.equal(result.isError, undefined);
  const resultText = CallToolResultSchema.parse(result).content.find(
    (item) => item.type === 'text',
  );
  assert.ok(resultText?.type === 'text');
  assert.equal(JSON.parse(resultText.text).layout, 'sheet');
  const pdf = await PDFDocument.load(await fs.readFile(path.join(f.root, 'sheet.pdf')));
  const page = pdf.getPage(0);
  assert.ok(Math.abs(page.getWidth() - (420 * 72) / 25.4) < 0.001);
  assert.ok(Math.abs(page.getHeight() - (297 * 72) / 25.4) < 0.001);
  const contents = page.node.Contents();
  assert.ok(contents instanceof PDFArray);
  const stream = pdf.context.lookup(contents.get(0));
  assert.ok(stream instanceof PDFRawStream);
  const operators = inflateSync(stream.getContents()).toString();
  assert.ok(operators.includes(`${page.getWidth()} 0 0 ${page.getHeight()} 0 0 cm`));
  for (const dimensions of [
    { page_width_mm: 297, page_height_mm: 210 },
    { page_width_mm: 420 },
    { page_height_mm: 297 },
    { page_width_mm: 0, page_height_mm: 297 },
  ]) {
    const rejected = await f.client.callTool({
      name: 'export_drawing_pdf',
      arguments: {
        source_filename: 'sheet.svg',
        pdf_filename: 'invalid.pdf',
        ...dimensions,
      },
    });
    assert.equal(rejected.isError, true);
  }
  await assert.rejects(fs.stat(path.join(f.root, 'invalid.pdf')), { code: 'ENOENT' });
  await fs.writeFile(
    path.join(f.root, 'preview.png'),
    await sharp({ create: { width: 120, height: 80, channels: 3, background: 'white' } })
      .png()
      .toBuffer(),
  );
  const raster = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: {
      source_filename: 'preview.png',
      pdf_filename: 'raster.pdf',
      page_width_mm: 420,
      page_height_mm: 297,
    },
  });
  assert.equal(raster.isError, true);
  assert.match(JSON.stringify(raster), /SVG_SHEET_REQUIRED/);
});

function localDownload(f: { url: URL }, remote: string): URL {
  const target = new URL(remote);
  target.host = f.url.host;
  target.protocol = f.url.protocol;
  return target;
}

test('chat download links deliver exact files without login and reject other users or files', async () => {
  const sharedKey = 'c'.repeat(64);
  const a = await fixture({
    downloadUrl: 'https://example.test/download/',
    downloadSigningKey: sharedKey,
  });
  const b = await fixture({
    userId: 'b'.repeat(24),
    downloadUrl: 'https://example.test/download/',
    downloadSigningKey: sharedKey,
  });
  await fs.writeFile(path.join(a.root, 'drawing.pdf'), '%PDF-admin');
  await fs.writeFile(path.join(a.root, 'other.pdf'), '%PDF-other');
  await fs.writeFile(path.join(a.root, 'cutlist.csv'), 'Part,Qty\nDoor,4\n');
  await fs.writeFile(path.join(b.root, 'drawing.pdf'), '%PDF-user');
  const result = await a.client.callTool({ name: 'workspace_files', arguments: {} });
  assert.ok(Array.isArray(result.content));
  const block = result.content.find((item) => item.type === 'text');
  assert.ok(block && block.type === 'text');
  const files = JSON.parse(block.text).files as {
    filename: string;
    download_url: string;
    expires_at: string;
  }[];
  assert.ok(files.some((file) => file.filename === 'cutlist.csv'));
  const file = files.find((file) => file.filename === 'drawing.pdf');
  assert.ok(file);
  assert.ok(Date.parse(file.expires_at) > Date.now());
  const url = localDownload(a, file.download_url);
  const response = await fetch(url);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), '%PDF-admin');
  assert.equal(response.headers.get('cache-control'), 'private, no-store');
  assert.equal(response.headers.get('referrer-policy'), 'no-referrer');
  assert.match(response.headers.get('content-disposition') ?? '', /attachment/);
  const head = await fetch(url, { method: 'HEAD' });
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
  assert.equal((await fetch(url, { method: 'POST' })).status, 405);
  assert.equal((await fetch(new URL('/download/drawing.pdf', a.url))).status, 403);
  assert.equal((await fetch(localDownload(b, file.download_url))).status, 403);
  const differentFile = new URL(url);
  differentFile.pathname = '/download/other.pdf';
  assert.equal((await fetch(differentFile)).status, 403);
  const forged = new URL(url);
  const ticket = forged.searchParams.get('ticket')!;
  forged.searchParams.set('ticket', ticket.slice(0, -1) + (ticket.endsWith('A') ? 'Q' : 'A'));
  assert.equal((await fetch(forged)).status, 403);
  const traversal = new URL(url);
  traversal.pathname = '/download/%2e%2e%2fsecret.pdf';
  assert.equal((await fetch(traversal)).status, 403);
});

test('download links expire and do not silently deliver changed files', async (context) => {
  const f = await fixture({
    downloadUrl: 'https://example.test/download/',
    downloadSigningKey: 'a'.repeat(64),
    downloadLinkTtlSeconds: 60,
  });
  await fs.writeFile(
    path.join(f.root, 'drawing.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20" fill="black"/></svg>',
  );
  const result = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: { source_filename: 'drawing.svg', pdf_filename: 'drawing.pdf' },
  });
  assert.ok(Array.isArray(result.content));
  const block = result.content.find((item) => item.type === 'text');
  assert.ok(block && block.type === 'text');
  const file = JSON.parse(block.text) as { download_url: string; expires_at: string };
  const url = localDownload(f, file.download_url);
  assert.equal((await fetch(url)).status, 200);
  const original = await fs.readFile(path.join(f.root, 'drawing.pdf'));
  await fs.writeFile(path.join(f.root, 'drawing.pdf'), '%PDF-replaced');
  assert.equal((await fetch(url)).status, 409);
  await fs.writeFile(path.join(f.root, 'drawing.pdf'), original);
  context.mock.method(Date, 'now', () => Date.parse(file.expires_at) + 1000);
  assert.equal((await fetch(url)).status, 403);
});

test('large exports remain visible with authenticated fallback links', async () => {
  const f = await fixture({
    maxFileBytes: 4,
    downloadUrl: 'https://example.test/download/',
    downloadSigningKey: 'a'.repeat(64),
  });
  await fs.writeFile(path.join(f.root, 'large.blend'), '12345');
  const result = await f.client.callTool({ name: 'workspace_files', arguments: {} });
  assert.ok(Array.isArray(result.content));
  const block = result.content.find((item) => item.type === 'text');
  assert.ok(block && block.type === 'text');
  const file = JSON.parse(block.text).files[0];
  assert.equal(file.filename, 'large.blend');
  assert.equal(file.bytes, 5);
  assert.equal(file.download_url, 'https://example.test/cad-files/admin/large.blend');
  assert.equal(file.download_requires_login, true);
  assert.equal(file.direct_download_limit_bytes, 4);
});

test('a real file mutation during reading does not hide other stable exports', async (context) => {
  const f = await fixture({
    downloadUrl: 'https://example.test/download/',
    downloadSigningKey: 'a'.repeat(64),
  });
  const changing = path.join(f.root, 'changing.step');
  await fs.writeFile(changing, 'changing');
  await fs.writeFile(path.join(f.root, 'stable.step'), 'stable');
  const handle = await fs.open(changing, 'r');
  const prototype: FileHandle = Object.getPrototypeOf(handle);
  const original = prototype.read;
  await handle.close();
  let mutated = false;
  context.mock.method(
    prototype,
    'read',
    async function (
      this: FileHandle,
      buffer: Buffer,
      offset: number,
      length: number,
      position: number,
    ) {
      const result = await original.bind(this)(buffer, offset, length, position);
      if (!mutated && buffer.subarray(0, 8).toString() === 'changing') {
        mutated = true;
        await fs.appendFile(changing, '-in-progress');
      }
      return result;
    },
  );
  const result = await f.client.callTool({ name: 'workspace_files', arguments: {} });
  assert.notEqual(result.isError, true);
  assert.equal(mutated, true);
  assert.ok(Array.isArray(result.content));
  const block = result.content.find((item) => item.type === 'text');
  assert.ok(block && block.type === 'text');
  const files = JSON.parse(block.text).files as {
    filename: string;
    download_url: string;
    download_status?: string;
    download_requires_login?: boolean;
  }[];
  const transient = files.find((file) => file.filename === 'changing.step');
  assert.equal(transient?.download_status, 'FILE_IS_CHANGING');
  assert.equal(transient?.download_requires_login, true);
  const stable = files.find((file) => file.filename === 'stable.step');
  assert.ok(stable);
  assert.equal((await fetch(localDownload(f, stable.download_url))).status, 200);
});

test('downloads retain their slot until the response finishes or closes', async (context) => {
  const f = await fixture({
    downloadUrl: 'https://example.test/download/',
    downloadSigningKey: 'a'.repeat(64),
  });
  await fs.writeFile(path.join(f.root, 'large.blend'), Buffer.alloc(16 * 1024 * 1024, 1));
  const result = await f.client.callTool({ name: 'workspace_files', arguments: {} });
  assert.ok(Array.isArray(result.content));
  const block = result.content.find((item) => item.type === 'text');
  assert.ok(block && block.type === 'text');
  const url = localDownload(f, JSON.parse(block.text).files[0].download_url);
  const closed = Promise.withResolvers<void>();
  let finished = false;
  f.server.on('request', (req, res) => {
    if (req.method !== 'GET' || !req.url?.startsWith('/download/')) return;
    res.once('finish', () => {
      finished = true;
    });
    res.once('close', () => closed.resolve());
  });
  const socket = connect({ host: url.hostname, port: Number(url.port) });
  const received = Promise.withResolvers<Buffer>();
  socket.once('data', (bytes) => {
    socket.pause();
    received.resolve(bytes);
  });
  socket.once('error', received.reject);
  socket.write(
    `GET ${url.pathname}${url.search} HTTP/1.1\r\nHost: ${url.host}\r\nConnection: close\r\n\r\n`,
  );
  try {
    assert.match((await received.promise).toString('latin1'), /^HTTP\/1.1 200/);
    // 某些系统的回环套接字可先缓冲整个文件；按真实响应状态验证名额。
    const expected = finished ? 200 : 503;
    context.diagnostic(
      finished
        ? 'Loopback transfer finished before the client paused.'
        : 'Paused client kept the download response pending.',
    );
    assert.equal((await fetch(url, { method: 'HEAD' })).status, expected);
    socket.destroy();
    await closed.promise;
    const retry = await fetch(url, { method: 'HEAD' });
    assert.equal(retry.status, 200);
  } finally {
    socket.destroy();
  }
});

test('uses supported Seedream size and does not send credentials to another destination', async () => {
  const png = await sharp({ create: { width: 16, height: 16, channels: 3, background: 'white' } })
    .png()
    .toBuffer();
  const requests: { url: string; options?: RequestInit }[] = [];
  const request: Request = async (url, options) => {
    requests.push({ url: String(url), options });
    return Response.json({ data: [{ b64_json: png.toString('base64') }] });
  };
  const f = await fixture({ arkApiKey: 'provider-secret' }, request);
  const output = await f.client.callTool({
    name: 'seedream_image',
    arguments: { prompt: 'A cube', size: '2K' },
  });
  assert.equal(output.isError, undefined);
  assert.equal(requests[0].url, 'https://ark.cn-beijing.volces.com/api/v3/images/generations');
  assert.equal(JSON.parse(String(requests[0].options?.body)).size, '2048x2048');
  assert.doesNotMatch(JSON.stringify(output), /provider-secret/);
  const invalid = await f.client.callTool({
    name: 'seedream_image',
    arguments: { prompt: 'A cube', size: '4K' },
  });
  assert.equal(invalid.isError, true);
  assert.equal(requests.length, 1);
});

test('returns a stable quota error without provider diagnostics', async () => {
  const request: Request = async () => new Response('private provider token', { status: 429 });
  const f = await fixture({ geminiApiKey: 'secret' }, request);
  const result = await f.client.callTool({ name: 'gemini_image', arguments: { prompt: 'A cube' } });
  assert.equal(result.isError, true);
  assert.match(JSON.stringify(result), /IMAGE_PROVIDER_QUOTA/);
  assert.doesNotMatch(JSON.stringify(result), /private provider token|secret/);
});

test('caps file reads, provider responses, and inline images', async () => {
  const f = await fixture({ maxFileBytes: 4 });
  await fs.writeFile(path.join(f.root, 'large.step'), '12345');
  await assert.rejects(readWorkspace(f.settings, 'large.step'), { code: 'FILE_TOO_LARGE' });
  await assert.rejects(boundedBody(new Response('12345'), 4), { code: 'FILE_TOO_LARGE' });
  const png = await sharp(randomBytes(128 * 128 * 3), {
    raw: { width: 128, height: 128, channels: 3 },
  })
    .png()
    .toBuffer();
  const image = await compactImage(
    { bytes: png, mimeType: 'image/png' },
    20000,
    f.settings.maxImagePixels,
  );
  assert.equal(image.mimeType, 'image/jpeg');
  assert.ok(image.bytes.length <= 20000);
  assert.equal((await sharp(image.bytes).metadata()).width, 128);
});

test('bounds concurrent jobs and releases capacity after provider timeout', async () => {
  const entered = Promise.withResolvers<void>();
  const request: Request = async (_url, options) => {
    entered.resolve();
    return new Promise<Response>((_resolve, reject) => {
      const signal = options?.signal;
      assert.ok(signal);
      signal.addEventListener('abort', () => reject(signal.reason), { once: true });
    });
  };
  const f = await fixture({ geminiApiKey: 'secret', requestTimeoutMs: 200 }, request);
  const pending = f.client.callTool({ name: 'gemini_image', arguments: { prompt: 'A cube' } });
  await entered.promise;
  const busy = await f.client.callTool({ name: 'workspace_files', arguments: {} });
  assert.equal(busy.isError, true);
  assert.match(JSON.stringify(busy), /DESIGN_BUSY/);
  const cancelled = await pending;
  assert.equal(cancelled.isError, true);
  assert.match(JSON.stringify(cancelled), /REQUEST_CANCELLED/);
  const retry = await f.client.callTool({ name: 'workspace_files', arguments: {} });
  assert.equal(retry.isError, undefined);
});
