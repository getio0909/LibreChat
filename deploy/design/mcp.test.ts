import os from 'node:os';
import path from 'node:path';
import sharp from 'sharp';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { test, afterEach } from 'node:test';
import { PDFDocument } from 'pdf-lib';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import type { Server } from 'node:http';
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
  return { root, settings, client, url };
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
