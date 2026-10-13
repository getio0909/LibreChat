import os from 'node:os';
import path from 'node:path';
import sharp from 'sharp';
import assert from 'node:assert/strict';
import * as fs from 'node:fs/promises';
import { randomBytes } from 'node:crypto';
import { createServer } from 'node:http';
import { connect } from 'node:net';
import { watch } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { test, afterEach } from 'node:test';
import { inflateSync, deflateSync } from 'node:zlib';
import {
  PDFArray,
  PDFDict,
  PDFDocument,
  PDFName,
  PDFNull,
  PDFRawStream,
  PDFString,
  PrintScaling,
  degrees,
  drawObject,
} from 'pdf-lib';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { CallToolResultSchema } from '@modelcontextprotocol/sdk/types.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import type { Server } from 'node:http';
import type { PDFObject } from 'pdf-lib';
import type { FileHandle } from 'node:fs/promises';
import type { Settings, Request } from './config.ts';
import { settingsSchema, ToolFailure, boundedBody } from './config.ts';
import { fileType, readWorkspace, writePdf } from './files.ts';
import { createHandler } from './mcp.ts';
import { imageProviders, compactImage } from './images.ts';
import { render } from './render.ts';
import { pdfTask } from './parse.ts';

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
  assert.deepEqual(CallToolResultSchema.parse(repeated).content, [
    { type: 'text', text: JSON.stringify({ ok: false, error: { code: 'FILE_EXISTS' } }) },
  ]);
  assert.deepEqual(await fs.readFile(path.join(f.root, 'drawing.pdf')), bytes);
  const revision = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: { source_filename: 'drawing.svg', pdf_filename: 'drawing-r02.pdf' },
  });
  assert.equal(revision.isError, undefined);
  assert.equal(
    (
      await PDFDocument.load(await fs.readFile(path.join(f.root, 'drawing-r02.pdf')))
    ).getPageCount(),
    1,
  );
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
  const preview = CallToolResultSchema.parse(result).content.find((item) => item.type === 'image');
  assert.ok(preview?.type === 'image');
  const previewBytes = Buffer.from(preview.data, 'base64');
  const previewMetadata = await sharp(previewBytes).metadata();
  assert.ok(previewMetadata.width != null && previewMetadata.width <= 1600);
  assert.ok(previewMetadata.height != null && previewMetadata.height <= 1600);
  assert.ok(previewBytes.length <= f.settings.maxImageBytes);
  const pdf = await PDFDocument.load(await fs.readFile(path.join(f.root, 'sheet.pdf')));
  assert.equal(pdf.catalog.getOrCreateViewerPreferences().getPrintScaling(), PrintScaling.None);
  const page = pdf.getPage(0);
  assert.ok(Math.abs(page.getWidth() - (420 * 72) / 25.4) < 0.001);
  assert.ok(Math.abs(page.getHeight() - (297 * 72) / 25.4) < 0.001);
  const contents = page.node.Contents();
  const streams =
    contents instanceof PDFArray
      ? contents.asArray().map((item) => pdf.context.lookup(item))
      : [contents];
  const operators = streams
    .map((stream) => {
      assert.ok(stream instanceof PDFRawStream);
      return inflateSync(stream.getContents()).toString();
    })
    .join('\n');
  const lines = [...operators.matchAll(/([\d.]+) ([\d.]+) m ([\d.]+) ([\d.]+) l/g)];
  assert.ok(
    lines.some(
      (line) =>
        Math.abs(Number(line[3]) - Number(line[1]) - (100 * 72) / 25.4) < 0.01 &&
        Number(line[2]) === Number(line[4]),
    ),
    'The dimensioned 100 mm line must remain 100 mm on paper',
  );
  assert.doesNotMatch(operators, /\/Image/);
  assert.equal(imageCount(pdf), 0);
  await fs.writeFile(
    path.join(f.root, 'exponent.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="+4.2e2mm" height="2.97E+2mm" viewBox="0 0 420 297"><path d="M20 50h100" stroke="black"/></svg>',
  );
  const exponent = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: {
      source_filename: 'exponent.svg',
      pdf_filename: 'exponent.pdf',
      page_width_mm: 420,
      page_height_mm: 297,
    },
  });
  assert.equal(exponent.isError, undefined);
  const exponentPdf = await PDFDocument.load(await fs.readFile(path.join(f.root, 'exponent.pdf')));
  assert.ok(Math.abs(exponentPdf.getPage(0).getWidth() - (420 * 72) / 25.4) < 0.001);
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

test('physical SVG dimensions are compared before integer pixel rounding can hide a resize', async () => {
  const f = await fixture();
  for (const [index, width] of [
    '49.7mm',
    '50mm',
    '5cm',
    '1.968503937007874in',
    '50MM',
    '200q',
    '200Q',
  ].entries()) {
    const filename = 'physical-' + index + '.svg';
    await fs.writeFile(
      path.join(f.root, filename),
      '<svg xmlns="http://www.w3.org/2000/svg" width="' +
        width +
        '" height="50mm" viewBox="0 0 50 50"><path d="M5 25h40" stroke="black"/></svg>',
    );
    const result = await f.client.callTool({
      name: 'export_drawing_pdf',
      arguments: {
        source_filename: filename,
        pdf_filename: 'physical-' + index + '.pdf',
        page_width_mm: 50,
        page_height_mm: 50,
      },
    });
    if (index === 0) {
      assert.equal(result.isError, true);
      assert.match(JSON.stringify(result), /PAGE_SIZE_MISMATCH/);
      await assert.rejects(fs.stat(path.join(f.root, 'physical-0.pdf')), { code: 'ENOENT' });
    } else {
      assert.equal(result.isError, undefined);
      const pdf = await PDFDocument.load(
        await fs.readFile(path.join(f.root, 'physical-' + index + '.pdf')),
      );
      assert.ok(Math.abs(pdf.getPage(0).getWidth() - (50 * 72) / 25.4) < 0.001);
    }
  }
});

test('rounded physical unit conversions fit the requested sheet without accepting a real size mismatch', async () => {
  const f = await fixture();
  for (const [index, width] of ['595.28pt', '49.6063pc', '8.26772in', '209.98mm'].entries()) {
    await fs.writeFile(
      path.join(f.root, 'rounded.svg'),
      `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="841.89pt"><path d="M10 10h100" stroke="black"/></svg>`,
    );
    const result = await f.client.callTool({
      name: 'export_drawing_pdf',
      arguments: {
        source_filename: 'rounded.svg',
        pdf_filename: `rounded-${index}.pdf`,
        page_width_mm: 210,
        page_height_mm: 297,
      },
    });
    if (index === 3) {
      assert.equal(result.isError, true);
      assert.match(JSON.stringify(result), /PAGE_SIZE_MISMATCH/);
      await assert.rejects(fs.stat(path.join(f.root, `rounded-${index}.pdf`)), { code: 'ENOENT' });
    } else {
      assert.equal(result.isError, undefined);
      const pdf = await PDFDocument.load(
        await fs.readFile(path.join(f.root, `rounded-${index}.pdf`)),
      );
      assert.ok(Math.abs(pdf.getPage(0).getWidth() - (210 * 72) / 25.4) < 0.001);
      assert.ok(Math.abs(pdf.getPage(0).getHeight() - (297 * 72) / 25.4) < 0.001);
    }
  }
});

function imageCount(pdf: PDFDocument): number {
  return pdf.context
    .enumerateIndirectObjects()
    .filter(
      ([, object]) =>
        object instanceof PDFRawStream &&
        object.dict.get(PDFName.of('Subtype'))?.toString() === '/Image',
    ).length;
}

test('large CAD sheets retain A0 vector geometry while previews stay bounded', async () => {
  const f = await fixture();
  await fs.writeFile(
    path.join(f.root, 'large.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="841mm" height="1189mm" viewBox="0 0 841 1189"><rect width="841" height="1189" fill="white"/><rect x="10" y="10" width="821" height="1169" fill="none" stroke="black" stroke-width="0.3"/></svg>',
  );
  const result = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: {
      source_filename: 'large.svg',
      pdf_filename: 'large.pdf',
      page_width_mm: 841,
      page_height_mm: 1189,
    },
  });
  assert.equal(result.isError, undefined);
  const pdf = await PDFDocument.load(await fs.readFile(path.join(f.root, 'large.pdf')));
  assert.ok(Math.abs(pdf.getPage(0).getWidth() - (841 * 72) / 25.4) < 0.001);
  assert.ok(Math.abs(pdf.getPage(0).getHeight() - (1189 * 72) / 25.4) < 0.001);
  assert.equal(imageCount(pdf), 0);
  const preview = CallToolResultSchema.parse(result).content.find((item) => item.type === 'image');
  assert.ok(preview?.type === 'image');
  const metadata = await sharp(Buffer.from(preview.data, 'base64')).metadata();
  assert.ok(
    metadata.width &&
      metadata.height &&
      metadata.width * metadata.height <= f.settings.maxImagePixels,
  );
  const limited = await fixture({ maxImagePixels: 10000 });
  await fs.copyFile(path.join(f.root, 'large.svg'), path.join(limited.root, 'large.svg'));
  const rejected = await limited.client.callTool({
    name: 'export_drawing_pdf',
    arguments: {
      source_filename: 'large.svg',
      pdf_filename: 'large.pdf',
      page_width_mm: 841,
      page_height_mm: 1189,
    },
  });
  assert.equal(rejected.isError, true);
  assert.match(JSON.stringify(rejected), /PAGE_PIXEL_LIMIT/);
});

test('ordered drawing sets preserve native pages, CJK text, vectors, paper sizes and rotations', async () => {
  const f = await fixture();
  const native = await PDFDocument.create();
  native.catalog.getOrCreateViewerPreferences().setPrintScaling(PrintScaling.None);
  native.addPage([(297 * 72) / 25.4, (420 * 72) / 25.4]).drawText('ASSEMBLY - G01');
  const rotated = native.addPage([(210 * 72) / 25.4, (297 * 72) / 25.4]);
  rotated.setRotation(degrees(90));
  rotated.drawText('SECTION - G02', { x: 50, y: 50 });
  await fs.writeFile(path.join(f.root, 'native.pdf'), await native.save());
  await fs.writeFile(
    path.join(f.root, 'parts.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="420mm" height="297mm" viewBox="0 0 420 297"><path d="M20 80h180v100H20z" fill="none" stroke="black"/><text x="20" y="40" font-family="Noto Sans CJK SC" font-size="5">侧板 - P03</text></svg>',
  );
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'set.pdf',
      sheets: [
        { source_filename: 'native.pdf' },
        { source_filename: 'parts.svg', page_width_mm: 420, page_height_mm: 297 },
      ],
    },
  });
  assert.equal(result.isError, undefined);
  const text = CallToolResultSchema.parse(result).content.find((item) => item.type === 'text');
  assert.ok(text?.type === 'text');
  assert.deepEqual(
    JSON.parse(text.text).pages.map((item: { source_page: number }) => item.source_page),
    [1, 2, 1],
  );
  const bytes = await fs.readFile(path.join(f.root, 'set.pdf'));
  const pdf = await PDFDocument.load(bytes);
  assert.equal(pdf.getPageCount(), 3);
  assert.equal(pdf.catalog.getOrCreateViewerPreferences().getPrintScaling(), PrintScaling.None);
  assert.equal(imageCount(pdf), 0);
  assert.equal(pdf.getPage(1).getRotation().angle, 90);
  assert.ok(Math.abs(pdf.getPage(2).getWidth() - (420 * 72) / 25.4) < 0.001);
  const extracted = spawnSync('pdftotext', ['-', '-'], { input: bytes, encoding: 'utf8' });
  assert.equal(extracted.status, 0);
  assert.match(extracted.stdout, /ASSEMBLY - G01[\s\S]*SECTION - G02[\s\S]*侧板 - P03/);
  for (const page of [1, 2, 3]) {
    const inspected = await f.client.callTool({
      name: 'inspect_pdf',
      arguments: { pdf_filename: 'set.pdf', page },
    });
    assert.equal(inspected.isError, undefined);
    assert.match(
      JSON.stringify(
        CallToolResultSchema.parse(inspected).content.filter((item) => item.type === 'text'),
      ),
      /finished_pdf/,
    );
    assert.ok(CallToolResultSchema.parse(inspected).content.some((item) => item.type === 'image'));
  }
  const region = await f.client.callTool({
    name: 'inspect_pdf',
    arguments: {
      pdf_filename: 'set.pdf',
      page: 3,
      region: { left_mm: 18, top_mm: 34, width_mm: 60, height_mm: 10 },
    },
  });
  assert.equal(region.isError, undefined);
  const image = CallToolResultSchema.parse(region).content.find((item) => item.type === 'image');
  assert.ok(image?.type === 'image');
  const stats = await sharp(Buffer.from(image.data, 'base64')).stats();
  assert.ok(stats.channels.some((channel) => channel.min < 50));
  for (const arguments_ of [
    { pdf_filename: 'set.pdf', page: 4 },
    {
      pdf_filename: 'set.pdf',
      page: 2,
      region: { left_mm: 296, top_mm: 0, width_mm: 2, height_mm: 10 },
    },
  ]) {
    assert.equal(
      (await f.client.callTool({ name: 'inspect_pdf', arguments: arguments_ })).isError,
      true,
    );
  }
  const repeated = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'set.pdf',
      sheets: [{ source_filename: 'native.pdf' }],
    },
  });
  assert.match(JSON.stringify(repeated), /FILE_EXISTS/);
  assert.deepEqual(await fs.readFile(path.join(f.root, 'set.pdf')), bytes);
});

test('native CAD page rotations accept negative and non-canonical multiples of 90', async () => {
  const f = await fixture();
  const native = await PDFDocument.create();
  const rotations = [-90, 360, 450];
  for (const rotation of rotations) {
    const page = native.addPage([(210 * 72) / 25.4, (297 * 72) / 25.4]);
    page.setRotation(degrees(rotation));
    page.drawText(`ROTATION ${rotation}`, { x: 20, y: 20 });
    page.node.set(PDFName.of('UserUnit'), native.context.register(native.context.obj(1)));
  }
  await fs.writeFile(path.join(f.root, 'rotated.pdf'), await native.save());
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'rotations.pdf',
      sheets: [{ source_filename: 'rotated.pdf' }],
    },
  });
  assert.equal(result.isError, undefined);
  const pdf = await PDFDocument.load(await fs.readFile(path.join(f.root, 'rotations.pdf')));
  assert.deepEqual(
    pdf.getPages().map((page) => page.getRotation().angle),
    rotations,
  );
  for (const [index, rotation] of rotations.entries()) {
    const inspection = await f.client.callTool({
      name: 'inspect_pdf',
      arguments: { pdf_filename: 'rotations.pdf', page: index + 1 },
    });
    assert.equal(inspection.isError, undefined);
    const text = CallToolResultSchema.parse(inspection).content.find(
      (item) => item.type === 'text',
    );
    assert.ok(text?.type === 'text');
    const [width, height] = JSON.parse(text.text).page_size_mm;
    assert.ok(Math.abs(width - (rotation % 180 ? 297 : 210)) < 0.001);
    assert.ok(Math.abs(height - (rotation % 180 ? 210 : 297)) < 0.001);
  }
});

test('layered native PDFs are rejected rather than changing hidden geometry', async () => {
  const f = await fixture();
  const native = await PDFDocument.create();
  const page = native.addPage();
  const layer = native.context.register(
    native.context.obj({ Type: 'OCG', Name: 'Hidden CAD layer' }),
  );
  native.catalog.set(
    PDFName.of('OCProperties'),
    native.context.obj({ OCGs: [layer], D: { OFF: [layer] } }),
  );
  page.node.set(PDFName.of('Resources'), native.context.obj({ Properties: { Hidden: layer } }));
  page.node.set(
    PDFName.of('Contents'),
    native.context.register(native.context.flateStream('/OC /Hidden BDC 10 10 20 20 re f EMC')),
  );
  const bytes = Buffer.from(await native.save());
  await fs.writeFile(path.join(f.root, 'layered.pdf'), bytes);
  const rejected = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'flat.pdf',
      sheets: [{ source_filename: 'layered.pdf' }],
    },
  });
  assert.match(JSON.stringify(rejected), /PDF_LAYERS_UNSUPPORTED/);
  await assert.rejects(fs.stat(path.join(f.root, 'flat.pdf')), { code: 'ENOENT' });
  assert.deepEqual(await fs.readFile(path.join(f.root, 'layered.pdf')), bytes);
});

test('PDF staging rolls back cancellation during writing and permits a clean retry', async () => {
  const f = await fixture();
  const controller = new AbortController();
  const observer = watch(f.root, (_, filename) => {
    if (filename?.endsWith('.tmp')) controller.abort();
  });
  try {
    await assert.rejects(
      writePdf(f.settings, 'cancelled.pdf', Buffer.alloc(16 * 1024 * 1024), controller.signal),
      (error: Error) => error.name === 'AbortError',
    );
  } finally {
    observer.close();
  }
  assert.deepEqual(await fs.readdir(f.root), []);
  await writePdf(
    f.settings,
    'cancelled.pdf',
    Buffer.from('complete replacement'),
    new AbortController().signal,
  );
  assert.equal(
    (await fs.readFile(path.join(f.root, 'cancelled.pdf'))).toString(),
    'complete replacement',
  );
  assert.deepEqual(await fs.readdir(f.root), ['cancelled.pdf']);
});

test('atomic PDF publication permits one writer and preserves its exact bytes', async () => {
  const f = await fixture();
  const candidates = [Buffer.from('first complete export'), Buffer.from('second complete export')];
  const results = await Promise.allSettled(
    candidates.map((bytes) =>
      writePdf(f.settings, 'same.pdf', bytes, new AbortController().signal),
    ),
  );
  assert.equal(results.filter((result) => result.status === 'fulfilled').length, 1);
  const failed = results.find((result) => result.status === 'rejected');
  assert.ok(
    failed?.status === 'rejected' &&
      failed.reason instanceof ToolFailure &&
      failed.reason.code === 'FILE_EXISTS',
  );
  const saved = await fs.readFile(path.join(f.root, 'same.pdf'));
  assert.ok(candidates.some((bytes) => bytes.equals(saved)));
  assert.deepEqual(await fs.readdir(f.root), ['same.pdf']);
});

test('cancellation after publication retains complete PDFs and concurrent user edits', async () => {
  const f = await fixture();
  for (const change of ['none', 'replace', 'edit'] as const) {
    const controller = new AbortController();
    const filename = `${change}.pdf`;
    await assert.rejects(
      writePdf(
        f.settings,
        filename,
        Buffer.from('complete export'),
        controller.signal,
        async (source, target) => {
          await fs.link(source, target);
          if (change === 'replace') await fs.unlink(target);
          if (change !== 'none') await fs.writeFile(target, 'changed by user');
          controller.abort();
        },
      ),
      (error: Error) => error.name === 'AbortError',
    );
    if (change === 'none') {
      assert.equal((await fs.readFile(path.join(f.root, filename))).toString(), 'complete export');
      await assert.rejects(
        writePdf(f.settings, filename, Buffer.from('retried export'), new AbortController().signal),
        (error: Error) => error instanceof ToolFailure && error.code === 'FILE_EXISTS',
      );
      assert.equal((await fs.readFile(path.join(f.root, filename))).toString(), 'complete export');
    } else {
      assert.equal((await fs.readFile(path.join(f.root, filename))).toString(), 'changed by user');
    }
  }
  assert.deepEqual((await fs.readdir(f.root)).sort(), ['edit.pdf', 'none.pdf', 'replace.pdf']);
});

test('native title-block forms and inconsistent paper boxes cannot enter a drawing set', async () => {
  const f = await fixture();
  const formPdf = await PDFDocument.create();
  const formPage = formPdf.addPage();
  const field = formPdf.getForm().createTextField('title');
  field.setText('Filled title block');
  field.addToPage(formPage, { x: 20, y: 20, width: 200, height: 25 });
  const sources = [
    { filename: 'form.pdf', bytes: await formPdf.save(), code: 'PDF_FORMS_UNSUPPORTED' },
  ];
  for (const [index, size] of [
    [100000, 100000],
    [595, 842],
    [-10, 842],
  ].entries()) {
    const pdf = await PDFDocument.create();
    const page = pdf.addPage([595, 842]);
    page.setMediaBox(0, 0, size[0], size[1]);
    page.setCropBox(10, 10, 200, 300);
    sources.push({
      filename: `box-${index}.pdf`,
      bytes: await pdf.save(),
      code: 'PDF_PAGE_UNSUPPORTED',
    });
  }
  for (const source of sources) {
    await fs.writeFile(path.join(f.root, source.filename), source.bytes);
    for (const tool of ['assemble_drawing_pdf', 'inspect_pdf']) {
      const arguments_ =
        tool === 'assemble_drawing_pdf'
          ? { pdf_filename: 'unsupported.pdf', sheets: [{ source_filename: source.filename }] }
          : { pdf_filename: source.filename, page: 1 };
      const rejected = await f.client.callTool({ name: tool, arguments: arguments_ });
      assert.equal(rejected.isError, true);
      assert.match(JSON.stringify(rejected), new RegExp(source.code));
    }
  }
  await assert.rejects(fs.stat(path.join(f.root, 'unsupported.pdf')), { code: 'ENOENT' });
});

test('static drawing sets reject page actions and annotation-bearing sources', async () => {
  const f = await fixture();
  for (const kind of ['page', 'presentation', 'link', 'comment']) {
    const pdf = await PDFDocument.create();
    const page = pdf.addPage();
    if (kind === 'page') {
      page.node.set(
        PDFName.of('AA'),
        pdf.context.obj({ O: { S: 'JavaScript', JS: PDFString.of('app.alert("fixture")') } }),
      );
    } else if (kind === 'presentation') {
      page.node.set(
        PDFName.of('PresSteps'),
        pdf.context.obj({
          Type: 'NavNode',
          NA: { S: 'JavaScript', JS: PDFString.of('app.alert("fixture")') },
        }),
      );
    } else {
      const annotation = pdf.context.obj({
        Type: 'Annot',
        Subtype: kind === 'link' ? 'Link' : 'Text',
        Rect: [10, 10, 100, 30],
        ...(kind === 'link'
          ? { A: { S: 'URI', URI: PDFString.of('https://example.test/fixture') } }
          : { Contents: PDFString.of('Static annotation') }),
      });
      page.node.set(PDFName.of('Annots'), pdf.context.obj([pdf.context.register(annotation)]));
    }
    await fs.writeFile(path.join(f.root, `${kind}.pdf`), await pdf.save());
    const rejected = await f.client.callTool({
      name: 'assemble_drawing_pdf',
      arguments: {
        pdf_filename: 'active.pdf',
        sheets: [{ source_filename: `${kind}.pdf` }],
      },
    });
    assert.match(JSON.stringify(rejected), /PDF_ACTIONS_UNSUPPORTED/);
    await assert.rejects(fs.stat(path.join(f.root, 'active.pdf')), { code: 'ENOENT' });
  }
});

test('drawing sets reject page-associated source files', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  const embedded = pdf.context.register(
    pdf.context.flateStream('private STEP source fixture', { Type: 'EmbeddedFile' }),
  );
  const file = pdf.context.register(
    pdf.context.obj({
      Type: 'Filespec',
      F: PDFString.of('source.step'),
      AFRelationship: 'Source',
      EF: { F: embedded },
    }),
  );
  page.node.set(PDFName.of('AF'), pdf.context.obj([file]));
  const bytes = await pdf.save();
  await fs.writeFile(path.join(f.root, 'associated.pdf'), bytes);
  const rejected = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'published.pdf',
      sheets: [{ source_filename: 'associated.pdf' }],
    },
  });
  assert.match(JSON.stringify(rejected), /PDF_ATTACHMENTS_UNSUPPORTED/);
  assert.doesNotMatch(JSON.stringify(rejected), /private STEP source fixture/);
  await assert.rejects(fs.stat(path.join(f.root, 'published.pdf')), { code: 'ENOENT' });
  assert.deepEqual(await fs.readFile(path.join(f.root, 'associated.pdf')), Buffer.from(bytes));
});

test('associated source files cannot hide in nested or inherited drawing resources', async () => {
  const f = await fixture();
  for (const inherited of [false, true]) {
    const pdf = await PDFDocument.create();
    const page = pdf.addPage();
    const payload = pdf.context.register(
      pdf.context.flateStream('attached CAD resource fixture', { Type: 'EmbeddedFile' }),
    );
    const file = pdf.context.register(
      pdf.context.obj({ Type: 'Filespec', F: PDFString.of('source.step'), EF: { F: payload } }),
    );
    const form = pdf.context.register(
      pdf.context.flateStream('0 0 10 10 re f', {
        Type: 'XObject',
        Subtype: 'Form',
        BBox: [0, 0, 10, 10],
        AF: [file],
      }),
    );
    const resources = pdf.context.obj({ XObject: { Drawing: form } });
    page.pushOperators(drawObject('Drawing'));
    if (inherited) {
      const parent = page.node.Parent();
      assert.ok(parent);
      parent.set(PDFName.of('Resources'), resources);
      page.node.delete(PDFName.of('Resources'));
    } else {
      page.node.set(PDFName.of('Resources'), resources);
    }
    await fs.writeFile(path.join(f.root, 'nested.pdf'), await pdf.save());
    const rejected = await f.client.callTool({
      name: 'assemble_drawing_pdf',
      arguments: {
        pdf_filename: 'nested-set.pdf',
        sheets: [{ source_filename: 'nested.pdf' }],
      },
    });
    assert.match(JSON.stringify(rejected), /PDF_ATTACHMENTS_UNSUPPORTED/);
    await assert.rejects(fs.stat(path.join(f.root, 'nested-set.pdf')), { code: 'ENOENT' });
  }
});

test('private editing data and external file streams are excluded from copied resources', async () => {
  const f = await fixture();
  for (const kind of ['piece', 'external']) {
    const pdf = await PDFDocument.create();
    const page = pdf.addPage();
    const privateData = pdf.context.register(pdf.context.flateStream('hidden editing fixture'));
    const form = pdf.context.register(
      pdf.context.flateStream('0 0 10 10 re f', {
        Type: 'XObject',
        Subtype: 'Form',
        BBox: [0, 0, 10, 10],
        ...(kind === 'piece'
          ? { PieceInfo: { Producer: { Private: privateData } } }
          : { F: PDFString.of('/not-present/source.data') }),
      }),
    );
    page.node.set(PDFName.of('Resources'), pdf.context.obj({ XObject: { Drawing: form } }));
    page.pushOperators(drawObject('Drawing'));
    await fs.writeFile(path.join(f.root, 'private.pdf'), await pdf.save());
    const rejected = await f.client.callTool({
      name: 'assemble_drawing_pdf',
      arguments: {
        pdf_filename: 'private-set.pdf',
        sheets: [{ source_filename: 'private.pdf' }],
      },
    });
    assert.match(
      JSON.stringify(rejected),
      kind === 'piece'
        ? /PDF_ATTACHMENTS_UNSUPPORTED/
        : /PDF_EXTERNAL_STREAM_UNSUPPORTED|INVALID_PDF/,
    );
    assert.doesNotMatch(JSON.stringify(rejected), /hidden editing fixture|not-present/);
    await assert.rejects(fs.stat(path.join(f.root, 'private-set.pdf')), { code: 'ENOENT' });
  }
});

test('external reference and OPI resources, alternate images and resource layers are rejected', async () => {
  const f = await fixture();
  for (const kind of ['reference', 'opi', 'alternate', 'layer', 'postscript']) {
    const pdf = await PDFDocument.create();
    const page = pdf.addPage();
    const external = PDFString.of('/private/old-drawing.pdf');
    const primary = pdf.context.register(
      pdf.context.flateStream(
        kind === 'reference'
          ? '0 0 10 10 re f'
          : kind === 'postscript'
            ? '0 0 moveto 10 10 lineto stroke'
            : Buffer.from([0, 0, 0]),
        {
          Type: 'XObject',
          ...(kind === 'postscript'
            ? { Subtype: 'PS' }
            : kind === 'reference'
              ? { Subtype: 'Form', BBox: [0, 0, 10, 10] }
              : {
                  Subtype: 'Image',
                  Width: 1,
                  Height: 1,
                  ColorSpace: 'DeviceRGB',
                  BitsPerComponent: 8,
                }),
          ...(kind === 'reference'
            ? { Ref: { F: external, Page: 0 } }
            : kind === 'opi'
              ? { OPI: { '2.0': { F: external, Size: [1, 1] } } }
              : kind === 'alternate'
                ? {
                    Alternates: [
                      {
                        Image: pdf.context.register(
                          pdf.context.flateStream(Buffer.from([255, 0, 0]), {
                            Type: 'XObject',
                            Subtype: 'Image',
                            Width: 1,
                            Height: 1,
                            ColorSpace: 'DeviceRGB',
                            BitsPerComponent: 8,
                          }),
                        ),
                        DefaultForPrinting: true,
                      },
                    ],
                  }
                : kind === 'layer'
                  ? { OC: { Type: 'OCG', Name: PDFString.of('hidden layer') } }
                  : {}),
        },
      ),
    );
    page.node.set(PDFName.of('Resources'), pdf.context.obj({ XObject: { Drawing: primary } }));
    page.pushOperators(drawObject('Drawing'));
    const bytes = Buffer.from(await pdf.save());
    await fs.writeFile(path.join(f.root, `${kind}.pdf`), bytes);
    const rejected = await f.client.callTool({
      name: 'assemble_drawing_pdf',
      arguments: {
        pdf_filename: 'unsupported.pdf',
        sheets: [{ source_filename: `${kind}.pdf` }],
      },
    });
    const code =
      kind === 'postscript'
        ? 'PDF_XOBJECT_UNSUPPORTED'
        : kind === 'alternate'
          ? 'PDF_IMAGE_ALTERNATES_UNSUPPORTED'
          : kind === 'layer'
            ? 'PDF_LAYERS_UNSUPPORTED'
            : 'PDF_EXTERNAL_STREAM_UNSUPPORTED';
    assert.equal(rejected.isError, true);
    assert.match(JSON.stringify(rejected), new RegExp(code));
    assert.doesNotMatch(JSON.stringify(rejected), /private|old-drawing/);
    await assert.rejects(fs.stat(path.join(f.root, 'unsupported.pdf')), { code: 'ENOENT' });
    assert.deepEqual(await fs.readFile(path.join(f.root, `${kind}.pdf`)), bytes);
  }
});

test('ordinary XObject aliases are not mistaken for control fields', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  const image = pdf.context.register(
    pdf.context.flateStream(Buffer.from([0, 0, 0]), {
      Type: 'XObject',
      Subtype: 'Image',
      Width: 1,
      Height: 1,
      ColorSpace: 'DeviceRGB',
      BitsPerComponent: 8,
    }),
  );
  page.node.set(
    PDFName.of('Resources'),
    pdf.context.obj({
      XObject: {
        Ref: image,
        OPI: image,
        Alternates: image,
        OC: image,
        Metadata: image,
        AF: image,
        EF: image,
        PieceInfo: image,
      },
    }),
  );
  page.node.set(
    PDFName.of('Contents'),
    pdf.context.register(
      pdf.context.flateStream(
        'q 50 0 0 50 20 20 cm /Metadata Do Q q 50 0 0 50 80 20 cm /AF Do Q q 50 0 0 50 140 20 cm /EF Do Q q 50 0 0 50 200 20 cm /PieceInfo Do Q',
      ),
    ),
  );
  await fs.writeFile(path.join(f.root, 'aliases.pdf'), await pdf.save());
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'aliases-set.pdf',
      sheets: [{ source_filename: 'aliases.pdf' }],
    },
  });
  assert.equal(result.isError, undefined);
  const output = await PDFDocument.load(await fs.readFile(path.join(f.root, 'aliases-set.pdf')));
  assert.equal(imageCount(output), 1);
  const preview = CallToolResultSchema.parse(result).content.find((item) => item.type === 'image');
  assert.ok(preview?.type === 'image');
  const stats = await sharp(Buffer.from(preview.data, 'base64')).stats();
  assert.ok(stats.channels.some((channel) => channel.min === 0));
});

test('drawing publication prunes stale unused page and Form resources', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  const image = pdf.context.register(
    pdf.context.flateStream(Buffer.from([255, 0, 0]), {
      Type: 'XObject',
      Subtype: 'Image',
      Width: 1,
      Height: 1,
      ColorSpace: 'DeviceRGB',
      BitsPerComponent: 8,
    }),
  );
  const stale = pdf.context.register(
    pdf.context.flateStream('0 0 50 50 re f', {
      Type: 'XObject',
      Subtype: 'Form',
      BBox: [0, 0, 50, 50],
    }),
  );
  const visible = pdf.context.register(
    pdf.context.flateStream('0 0 10 10 re f', {
      Type: 'XObject',
      Subtype: 'Form',
      BBox: [0, 0, 10, 10],
      Resources: { XObject: { OldImage: image } },
    }),
  );
  page.node.set(
    PDFName.of('Resources'),
    pdf.context.obj({
      XObject: { Drawing: visible, OldImage: image, OldForm: stale },
    }),
  );
  page.node.set(
    PDFName.of('Contents'),
    pdf.context.register(
      pdf.context.flateStream(
        '% /OldImage Do\n/Span << /ActualText (/OldImage Do) >> BDC /Dr#61wing Do EMC',
      ),
    ),
  );
  const bytes = Buffer.from(await pdf.save());
  await fs.writeFile(path.join(f.root, 'unused.pdf'), bytes);
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: { pdf_filename: 'visible.pdf', sheets: [{ source_filename: 'unused.pdf' }] },
  });
  assert.equal(result.isError, undefined);
  const output = await PDFDocument.load(await fs.readFile(path.join(f.root, 'visible.pdf')));
  assert.equal(imageCount(output), 0);
  assert.equal(
    output.context
      .enumerateIndirectObjects()
      .filter(
        ([, object]) =>
          object instanceof PDFRawStream &&
          object.dict.get(PDFName.of('Subtype')) === PDFName.of('Form'),
      ).length,
    1,
  );
  assert.deepEqual(await fs.readFile(path.join(f.root, 'unused.pdf')), bytes);
});

test('page content arrays retain drawing operands and strings across stream boundaries', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  const visible = pdf.context.register(
    pdf.context.flateStream('20 20 100 100 re f', {
      Type: 'XObject',
      Subtype: 'Form',
      BBox: [0, 0, 150, 150],
    }),
  );
  const stale = pdf.context.register(
    pdf.context.flateStream('0 0 10 10 re f', {
      Type: 'XObject',
      Subtype: 'Form',
      BBox: [0, 0, 10, 10],
    }),
  );
  page.node.set(
    PDFName.of('Resources'),
    pdf.context.obj({ XObject: { Drawing: visible, OldForm: stale } }),
  );
  page.node.set(
    PDFName.of('Contents'),
    pdf.context.obj([
      pdf.context.register(pdf.context.flateStream('/Drawing ')),
      pdf.context.register(pdf.context.flateStream('Do /Span << /ActualText (/Old')),
      pdf.context.register(pdf.context.flateStream('Form Do) >> BDC EMC')),
    ]),
  );
  const bytes = Buffer.from(await pdf.save());
  const result = await pdfTask(
    {
      operation: 'assemble',
      sources: [{ source_filename: 'streams.pdf', bytes: new Uint8Array(bytes) }],
      limits: f.settings,
    },
    f.settings,
    new AbortController().signal,
  );
  const output = await PDFDocument.load(result.bytes);
  const resources = output.getPage(0).node.Resources()?.lookup(PDFName.of('XObject'));
  assert.ok(resources instanceof PDFDict);
  assert.equal(resources.has(PDFName.of('Drawing')), true);
  assert.equal(resources.has(PDFName.of('OldForm')), false);
  await fs.writeFile(path.join(f.root, 'streams.pdf'), bytes);
  const published = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'streams-set.pdf',
      sheets: [{ source_filename: 'streams.pdf' }],
    },
  });
  assert.equal(published.isError, undefined);
  const image = CallToolResultSchema.parse(published).content.find((item) => item.type === 'image');
  assert.ok(image?.type === 'image');
  assert.ok(
    (await sharp(Buffer.from(image.data, 'base64')).stats()).channels.some(
      (channel) => channel.min < 30,
    ),
  );
  assert.deepEqual(await fs.readFile(path.join(f.root, 'streams.pdf')), bytes);
});

test('nested drawing resources retain selected patterns and states and omit unused programs', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  const pattern = pdf.context.register(
    pdf.context.flateStream('0 0 5 5 re f', {
      Type: 'Pattern',
      PatternType: 1,
      PaintType: 1,
      TilingType: 1,
      BBox: [0, 0, 10, 10],
      XStep: 10,
      YStep: 10,
      Resources: {},
    }),
  );
  const stale = pdf.context.register(
    pdf.context.flateStream('0 0 200 200 re f', {
      Type: 'Pattern',
      PatternType: 1,
      PaintType: 1,
      TilingType: 1,
      BBox: [0, 0, 200, 200],
      XStep: 200,
      YStep: 200,
      Resources: {},
    }),
  );
  const state = pdf.context.register(pdf.context.obj({ Type: 'ExtGState', CA: 1, ca: 1 }));
  const unusedState = pdf.context.register(
    pdf.context.obj({ Type: 'ExtGState', CA: 0.5, ca: 0.5 }),
  );
  const unusedFont = pdf.context.register(
    pdf.context.obj({ Type: 'Font', Subtype: 'Type1', BaseFont: 'Helvetica' }),
  );
  const visible = pdf.context.register(
    pdf.context.flateStream('/LiveState gs /Pattern cs /Live scn 20 20 100 100 re f', {
      Type: 'XObject',
      Subtype: 'Form',
      BBox: [0, 0, 150, 150],
      Resources: {
        Pattern: { Live: pattern, Old: stale },
        ExtGState: { LiveState: state, OldState: unusedState },
        Font: { OldFont: unusedFont },
      },
    }),
  );
  page.node.set(PDFName.of('Resources'), pdf.context.obj({ XObject: { Drawing: visible } }));
  page.pushOperators(drawObject('Drawing'));
  const bytes = Buffer.from(await pdf.save());
  await fs.writeFile(path.join(f.root, 'patterns.pdf'), bytes);
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'patterns-set.pdf',
      sheets: [{ source_filename: 'patterns.pdf' }],
    },
  });
  assert.equal(result.isError, undefined);
  const output = await PDFDocument.load(await fs.readFile(path.join(f.root, 'patterns-set.pdf')));
  const objects = output.getPage(0).node.Resources()?.lookup(PDFName.of('XObject'));
  assert.ok(objects instanceof PDFDict);
  const form = objects.lookup(PDFName.of('Drawing'));
  assert.ok(form instanceof PDFRawStream);
  const resources = form.dict.lookup(PDFName.of('Resources'));
  assert.ok(resources instanceof PDFDict);
  for (const [key, expected] of [
    ['Pattern', ['Live']],
    ['ExtGState', ['LiveState']],
    ['Font', []],
  ] as const) {
    const entries: PDFObject | undefined = resources.lookup(PDFName.of(key));
    assert.ok(entries instanceof PDFDict);
    assert.deepEqual(
      entries.keys().map((name) => name.decodeText()),
      expected,
    );
  }
  const image = CallToolResultSchema.parse(result).content.find((item) => item.type === 'image');
  assert.ok(image?.type === 'image');
  assert.ok(
    (await sharp(Buffer.from(image.data, 'base64')).stats()).channels.some(
      (channel) => channel.min < 30,
    ),
  );
  assert.deepEqual(await fs.readFile(path.join(f.root, 'patterns.pdf')), bytes);
});

test('nested shading keeps selected gradients and removes unused function streams', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  const gradient = pdf.context.register(
    pdf.context.obj({
      ShadingType: 2,
      ColorSpace: 'DeviceRGB',
      Coords: [0, 0, 150, 0],
      Extend: [true, true],
      Function: { FunctionType: 2, Domain: [0, 1], C0: [0, 0, 0], C1: [1, 1, 1], N: 1 },
    }),
  );
  const functionRef = pdf.context.register(
    pdf.context.flateStream('{ 0 0 0 }', {
      FunctionType: 4,
      Domain: [0, 1],
      Range: [0, 1, 0, 1, 0, 1],
    }),
  );
  const stale = pdf.context.register(
    pdf.context.obj({
      ShadingType: 2,
      ColorSpace: 'DeviceRGB',
      Coords: [0, 0, 200, 0],
      Function: functionRef,
    }),
  );
  const form = pdf.context.register(
    pdf.context.flateStream('/Current sh', {
      Type: 'XObject',
      Subtype: 'Form',
      BBox: [0, 0, 150, 150],
      Resources: { Shading: { Current: gradient, Old: stale } },
    }),
  );
  page.node.set(PDFName.of('Resources'), pdf.context.obj({ XObject: { Drawing: form } }));
  page.pushOperators(drawObject('Drawing'));
  const bytes = Buffer.from(await pdf.save());
  await fs.writeFile(path.join(f.root, 'shading.pdf'), bytes);
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'shading-set.pdf',
      sheets: [{ source_filename: 'shading.pdf' }],
    },
  });
  assert.equal(result.isError, undefined);
  const output = await PDFDocument.load(await fs.readFile(path.join(f.root, 'shading-set.pdf')));
  const objects = output.getPage(0).node.Resources()?.lookup(PDFName.of('XObject'));
  assert.ok(objects instanceof PDFDict);
  const copiedForm = objects.lookup(PDFName.of('Drawing'));
  assert.ok(copiedForm instanceof PDFRawStream);
  const resources = copiedForm.dict.lookup(PDFName.of('Resources'));
  assert.ok(resources instanceof PDFDict);
  const shading = resources.lookup(PDFName.of('Shading'));
  assert.ok(shading instanceof PDFDict);
  assert.deepEqual(
    shading.keys().map((name) => name.decodeText()),
    ['Current'],
  );
  assert.equal(
    output.context
      .enumerateIndirectObjects()
      .some(
        ([, object]) =>
          object instanceof PDFRawStream && object.dict.has(PDFName.of('FunctionType')),
      ),
    false,
  );
  const image = CallToolResultSchema.parse(result).content.find((item) => item.type === 'image');
  assert.ok(image?.type === 'image');
  assert.ok(
    (await sharp(Buffer.from(image.data, 'base64')).stats()).channels.some(
      (channel) => channel.min < 30,
    ),
  );
  assert.deepEqual(await fs.readFile(path.join(f.root, 'shading.pdf')), bytes);
});

test('PDF NUL whitespace and null Flate parameters retain visible drawing resources', async () => {
  const f = await fixture();
  for (const [index, params] of [PDFNull, undefined].entries()) {
    const pdf = await PDFDocument.create();
    const page = pdf.addPage();
    const form = pdf.context.register(
      pdf.context.flateStream('20 20 100 100 re f', {
        Type: 'XObject',
        Subtype: 'Form',
        BBox: [0, 0, 150, 150],
      }),
    );
    page.node.set(PDFName.of('Resources'), pdf.context.obj({ XObject: { Drawing: form } }));
    const content = pdf.context.flateStream('/Drawing\x00Do');
    content.dict.set(PDFName.of('Filter'), pdf.context.obj(['FlateDecode']));
    content.dict.set(PDFName.of('DecodeParms'), params ?? pdf.context.obj([null]));
    page.node.set(PDFName.of('Contents'), pdf.context.register(content));
    const bytes = Buffer.from(await pdf.save());
    const copied = await pdfTask(
      {
        operation: 'assemble',
        sources: [{ source_filename: 'null.pdf', bytes: new Uint8Array(bytes) }],
        limits: f.settings,
      },
      f.settings,
      new AbortController().signal,
    );
    const document = await PDFDocument.load(copied.bytes);
    const resources = document.getPage(0).node.Resources();
    assert.ok(resources?.lookup(PDFName.of('XObject')) instanceof PDFDict);
    assert.equal(
      (resources.lookup(PDFName.of('XObject')) as PDFDict).has(PDFName.of('Drawing')),
      true,
    );
    await fs.writeFile(path.join(f.root, `null-${index}.pdf`), bytes);
    const result = await f.client.callTool({
      name: 'assemble_drawing_pdf',
      arguments: {
        pdf_filename: `visible-${index}.pdf`,
        sheets: [{ source_filename: `null-${index}.pdf` }],
      },
    });
    assert.equal(result.isError, undefined);
    const image = CallToolResultSchema.parse(result).content.find((item) => item.type === 'image');
    assert.ok(image?.type === 'image');
    assert.ok(
      (await sharp(Buffer.from(image.data, 'base64')).stats()).channels.some(
        (channel) => channel.min < 30,
      ),
    );
    assert.deepEqual(await fs.readFile(path.join(f.root, `null-${index}.pdf`)), bytes);
  }
});

test('Type3 fonts require producer conversion before dormant glyph assets can be published', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  const image = pdf.context.register(
    pdf.context.flateStream(Buffer.from([255, 0, 0]), {
      Type: 'XObject',
      Subtype: 'Image',
      Width: 1,
      Height: 1,
      ColorSpace: 'DeviceRGB',
      BitsPerComponent: 8,
    }),
  );
  const visible = pdf.context.register(pdf.context.flateStream('600 0 d0 0 0 100 100 re f'));
  const dormant = pdf.context.register(pdf.context.flateStream('600 0 d0 /OldImage Do'));
  const font = pdf.context.register(
    pdf.context.obj({
      Type: 'Font',
      Subtype: 'Type3',
      FontBBox: [0, 0, 600, 600],
      FontMatrix: [0.001, 0, 0, 0.001, 0, 0],
      CharProcs: { A: visible, B: dormant },
      Encoding: { Type: 'Encoding', Differences: [65, 'A', 'B'] },
      FirstChar: 65,
      LastChar: 66,
      Widths: [600, 600],
      Resources: { XObject: { OldImage: image } },
    }),
  );
  page.node.set(PDFName.of('Resources'), pdf.context.obj({ Font: { F1: font } }));
  page.node.set(
    PDFName.of('Contents'),
    pdf.context.register(pdf.context.flateStream('BT /F1 12 Tf 20 20 Td (A) Tj ET')),
  );
  const bytes = Buffer.from(await pdf.save());
  await fs.writeFile(path.join(f.root, 'glyphs.pdf'), bytes);
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: { pdf_filename: 'glyphs-set.pdf', sheets: [{ source_filename: 'glyphs.pdf' }] },
  });
  assert.equal(result.isError, true);
  assert.match(JSON.stringify(result), /PDF_TYPE3_FONT_UNSUPPORTED/);
  await assert.rejects(fs.stat(path.join(f.root, 'glyphs-set.pdf')), { code: 'ENOENT' });
  assert.deepEqual(await fs.readFile(path.join(f.root, 'glyphs.pdf')), bytes);
});

test('PDF exports, assembly and finished-page inspection accept mixed-case file extensions', async () => {
  const f = await fixture();
  await fs.writeFile(
    path.join(f.root, 'case.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm"><rect x="10" y="10" width="50" height="20"/></svg>',
  );
  const exported = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: {
      source_filename: 'case.svg',
      pdf_filename: 'SHEET.PDF',
      page_width_mm: 210,
      page_height_mm: 297,
    },
  });
  assert.equal(exported.isError, undefined);
  assert.equal(fileType('SHEET.PDF'), 'application/pdf');
  const assembled = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: { pdf_filename: 'Set.PdF', sheets: [{ source_filename: 'SHEET.PDF' }] },
  });
  assert.equal(assembled.isError, undefined);
  for (const pdf_filename of ['SHEET.PDF', 'Set.PdF']) {
    const inspected = await f.client.callTool({
      name: 'inspect_pdf',
      arguments: { pdf_filename, page: 1 },
    });
    assert.equal(inspected.isError, undefined);
    assert.match(JSON.stringify(inspected), /finished_pdf/);
  }
});

test('compressed drawing content is bounded before its resource references are scanned', async () => {
  const f = await fixture({ maxFileBytes: 65536 });
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  page.node.set(
    PDFName.of('Contents'),
    pdf.context.register(pdf.context.flateStream(' '.repeat(200000))),
  );
  const bytes = Buffer.from(await pdf.save());
  assert.ok(bytes.length < f.settings.maxFileBytes);
  await fs.writeFile(path.join(f.root, 'expanded-content.pdf'), bytes);
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'expanded-content-set.pdf',
      sheets: [{ source_filename: 'expanded-content.pdf' }],
    },
  });
  assert.equal(result.isError, true);
  assert.match(JSON.stringify(result), /FILE_TOO_LARGE/);
  await assert.rejects(fs.stat(path.join(f.root, 'expanded-content-set.pdf')), { code: 'ENOENT' });
  assert.equal((await fetch(new URL('/health', f.url))).status, 200);
});

test('renderer concurrency fits one aggregate budget while permitted concurrent exports work', async () => {
  for (const config of [
    { maxConcurrentJobs: 2, maxRenderMemoryBytes: 384 * 1024 * 1024 },
    { maxConcurrentJobs: 4, maxRenderMemoryBytes: 256 * 1024 * 1024 },
    {
      maxConcurrentJobs: 4,
      maxRenderMemoryBytes: 96 * 1024 * 1024,
      maxFileBytes: 25 * 1024 * 1024,
    },
  ]) {
    assert.equal(
      settingsSchema.safeParse({ workspaceUrl: 'https://example.test/cad/', ...config }).success,
      false,
    );
  }
  const f = await fixture({
    maxConcurrentJobs: 4,
    maxRenderMemoryBytes: 96 * 1024 * 1024,
    maxFileBytes: 16 * 1024 * 1024,
  });
  await fs.writeFile(
    path.join(f.root, 'concurrent.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm"><rect x="20" y="20" width="100" height="50" fill="none" stroke="black"/></svg>',
  );
  const results = await Promise.all(
    [1, 2, 3, 4].map((index) =>
      f.client.callTool({
        name: 'export_drawing_pdf',
        arguments: {
          source_filename: 'concurrent.svg',
          pdf_filename: `concurrent-${index}.pdf`,
          page_width_mm: 210,
          page_height_mm: 297,
        },
      }),
    ),
  );
  for (const [index, result] of results.entries()) {
    assert.equal(result.isError, undefined);
    const pdf = await PDFDocument.load(
      await fs.readFile(path.join(f.root, `concurrent-${index + 1}.pdf`)),
    );
    assert.equal(pdf.getPageCount(), 1);
    assert.equal(pdf.catalog.getOrCreateViewerPreferences().getPrintScaling(), PrintScaling.None);
    assert.equal(imageCount(pdf), 0);
  }
  assert.equal((await fetch(new URL('/health', f.url))).status, 200);
});

test('catalog color output intents are rejected without changing the source', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  pdf.addPage();
  const profile = pdf.context.register(pdf.context.flateStream('ICC fixture', { N: 3 }));
  pdf.catalog.set(
    PDFName.of('OutputIntents'),
    pdf.context.obj([{ Type: 'OutputIntent', S: 'GTS_PDFX', DestOutputProfile: profile }]),
  );
  await fs.writeFile(path.join(f.root, 'managed.pdf'), await pdf.save());
  const rejected = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'managed-set.pdf',
      sheets: [{ source_filename: 'managed.pdf' }],
    },
  });
  assert.match(JSON.stringify(rejected), /PDF_COLOR_PROFILE_UNSUPPORTED/);
  await assert.rejects(fs.stat(path.join(f.root, 'managed-set.pdf')), { code: 'ENOENT' });
});

test('drawing sets omit stale page thumbnails and preserve the original source', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  page.drawLine({ start: { x: 20, y: 20 }, end: { x: 120, y: 20 }, thickness: 1 });
  const oldImage = await sharp({
    create: { width: 8, height: 8, channels: 3, background: '#ff0000' },
  })
    .png()
    .toBuffer();
  const thumbnail = await pdf.embedPng(oldImage);
  page.node.set(PDFName.of('Thumb'), thumbnail.ref);
  const bytes = Buffer.from(await pdf.save());
  await fs.writeFile(path.join(f.root, 'thumbnail.pdf'), bytes);
  assert.equal(imageCount(await PDFDocument.load(bytes)), 1);
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'current.pdf',
      sheets: [{ source_filename: 'thumbnail.pdf' }],
    },
  });
  assert.equal(result.isError, undefined);
  const current = await PDFDocument.load(await fs.readFile(path.join(f.root, 'current.pdf')));
  assert.equal(current.getPage(0).node.has(PDFName.of('Thumb')), false);
  assert.equal(imageCount(current), 0);
  assert.deepEqual(await fs.readFile(path.join(f.root, 'thumbnail.pdf')), bytes);
  assert.equal((await PDFDocument.load(bytes)).getPage(0).node.has(PDFName.of('Thumb')), true);
});

test('combined drawing sets bound the total expanded object data before copying pages', async () => {
  const f = await fixture({ maxFileBytes: 300 * 1024 });
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  page.drawLine({ start: { x: 20, y: 20 }, end: { x: 120, y: 20 }, thickness: 1 });
  page.node.set(PDFName.of('Fixture'), PDFString.of('X'.repeat(200 * 1024)));
  const bytes = Buffer.from(await pdf.save());
  assert.ok(bytes.length < 4096);
  await fs.writeFile(path.join(f.root, 'compressed.pdf'), bytes);
  const single = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'single.pdf',
      sheets: [{ source_filename: 'compressed.pdf' }],
    },
  });
  assert.equal(single.isError, undefined);
  const rejected = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'combined.pdf',
      sheets: [{ source_filename: 'compressed.pdf' }, { source_filename: 'compressed.pdf' }],
    },
  });
  assert.equal(rejected.isError, true);
  assert.match(JSON.stringify(rejected), /FILE_TOO_LARGE/);
  await assert.rejects(fs.stat(path.join(f.root, 'combined.pdf')), { code: 'ENOENT' });
  assert.deepEqual(await fs.readFile(path.join(f.root, 'compressed.pdf')), bytes);
  assert.equal((await fetch(new URL('/health', f.url))).status, 200);
  assert.equal(
    (await f.client.callTool({ name: 'workspace_files', arguments: {} })).isError,
    undefined,
  );
});

test('drawing sets omit invisible page and resource metadata without modifying sources', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  page.drawLine({ start: { x: 20, y: 20 }, end: { x: 120, y: 20 }, thickness: 1 });
  const metadata = pdf.context.register(
    pdf.context.flateStream('<fixture>old revision and private path</fixture>', {
      Type: 'Metadata',
      Subtype: 'XML',
    }),
  );
  page.node.set(PDFName.of('Metadata'), metadata);
  const form = pdf.context.register(
    pdf.context.flateStream('0 0 10 10 re f', {
      Type: 'XObject',
      Subtype: 'Form',
      BBox: [0, 0, 10, 10],
      Metadata: metadata,
    }),
  );
  page.node.set(PDFName.of('Resources'), pdf.context.obj({ XObject: { Drawing: form } }));
  page.pushOperators(drawObject('Drawing'));
  const bytes = Buffer.from(await pdf.save());
  await fs.writeFile(path.join(f.root, 'metadata.pdf'), bytes);
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'published.pdf',
      sheets: [{ source_filename: 'metadata.pdf' }],
    },
  });
  assert.equal(result.isError, undefined);
  const current = await PDFDocument.load(await fs.readFile(path.join(f.root, 'published.pdf')));
  assert.equal(current.getPage(0).node.has(PDFName.of('Metadata')), false);
  assert.equal(
    current.context.enumerateIndirectObjects().some(([, object]) => {
      const dict = object instanceof PDFRawStream ? object.dict : object;
      return (
        dict instanceof PDFDict &&
        (dict.has(PDFName.of('Metadata')) ||
          dict.get(PDFName.of('Type')) === PDFName.of('Metadata'))
      );
    }),
    false,
  );
  assert.deepEqual(await fs.readFile(path.join(f.root, 'metadata.pdf')), bytes);
  assert.equal((await PDFDocument.load(bytes)).getPage(0).node.has(PDFName.of('Metadata')), true);
});

test('drawing sets omit invisible article beads and thread metadata', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  page.drawLine({ start: { x: 20, y: 20 }, end: { x: 120, y: 20 }, thickness: 1 });
  const thread = pdf.context.obj({
    Type: 'Thread',
    I: { Author: PDFString.of('private author fixture'), Subject: PDFString.of('old revision') },
  });
  const threadRef = pdf.context.register(thread);
  const bead = pdf.context.obj({ Type: 'Bead', T: threadRef, P: page.ref, R: [0, 0, 10, 10] });
  const beadRef = pdf.context.register(bead);
  bead.set(PDFName.of('N'), beadRef);
  bead.set(PDFName.of('V'), beadRef);
  thread.set(PDFName.of('F'), beadRef);
  page.node.set(PDFName.of('B'), pdf.context.obj([beadRef]));
  const bytes = Buffer.from(await pdf.save());
  await fs.writeFile(path.join(f.root, 'article.pdf'), bytes);
  const result = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: { pdf_filename: 'article-set.pdf', sheets: [{ source_filename: 'article.pdf' }] },
  });
  assert.equal(result.isError, undefined);
  const output = await PDFDocument.load(await fs.readFile(path.join(f.root, 'article-set.pdf')));
  assert.equal(output.getPage(0).node.has(PDFName.of('B')), false);
  assert.equal(
    output.context.enumerateIndirectObjects().some(([, object]) => {
      const type = object instanceof PDFDict ? object.lookup(PDFName.of('Type')) : undefined;
      return type === PDFName.of('Thread') || type === PDFName.of('Bead');
    }),
    false,
  );
  assert.deepEqual(await fs.readFile(path.join(f.root, 'article.pdf')), bytes);
});

test('object-heavy PDF parsing stops on cancellation and the parent service stays usable', async () => {
  const f = await fixture();
  const pdf = await PDFDocument.create();
  const page = pdf.addPage();
  const objects = [];
  for (let i = 0; i < 30000; i++)
    objects.push(pdf.context.register(pdf.context.obj({ Position: [i, i + 1, i + 2] })));
  page.node.set(PDFName.of('FixtureRows'), pdf.context.obj(objects));
  const bytes = await pdf.save({ useObjectStreams: false });
  const started = Date.now();
  await assert.rejects(
    pdfTask(
      { operation: 'inspect', bytes, page: 1, limits: f.settings },
      f.settings,
      AbortSignal.timeout(120),
    ),
    (error: Error) => error instanceof ToolFailure && error.code === 'REQUEST_CANCELLED',
  );
  assert.ok(Date.now() - started < 1500);
  assert.equal((await fetch(new URL('/health', f.url))).status, 200);
  const small = await PDFDocument.create();
  small.addPage();
  const next = await pdfTask(
    { operation: 'inspect', bytes: await small.save(), page: 1, limits: f.settings },
    f.settings,
    new AbortController().signal,
  );
  assert.equal(next.pageCount, 1);
  assert.equal(
    (await f.client.callTool({ name: 'workspace_files', arguments: {} })).isError,
    undefined,
  );
});

test('compressed PDF object expansion is bounded outside the long-running service', async () => {
  const f = await fixture({ maxRenderMemoryBytes: 64 * 1024 * 1024 });
  const expanded = Buffer.alloc(80 * 1024 * 1024, 88);
  const compressed = deflateSync(Buffer.concat([Buffer.from('5 0 ('), expanded, Buffer.from(')')]));
  const chunks: Buffer[] = [Buffer.from('%PDF-1.5\n')];
  const offsets = [0];
  let offset = chunks[0].length;
  function object(number: number, bytes: Buffer) {
    offsets[number] = offset;
    const wrapped = Buffer.concat([
      Buffer.from(`${number} 0 obj\n`),
      bytes,
      Buffer.from('\nendobj\n'),
    ]);
    chunks.push(wrapped);
    offset += wrapped.length;
  }
  object(1, Buffer.from('<< /Type /Catalog /Pages 2 0 R >>'));
  object(2, Buffer.from('<< /Type /Pages /Kids [3 0 R] /Count 1 >>'));
  object(
    3,
    Buffer.from(
      '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << >> /PieceInfo 5 0 R >>',
    ),
  );
  object(4, Buffer.from('<< >>'));
  object(
    6,
    Buffer.concat([
      Buffer.from(
        `<< /Type /ObjStm /N 1 /First 4 /Filter /FlateDecode /Length ${compressed.length} >>\nstream\n`,
      ),
      compressed,
      Buffer.from('\nendstream'),
    ]),
  );
  offsets[7] = offset;
  const xref = Buffer.alloc(8 * 7);
  for (let i = 0; i < 8; i++) {
    xref[i * 7] = i === 0 ? 0 : i === 5 ? 2 : 1;
    xref.writeUInt32BE(i === 5 ? 6 : (offsets[i] ?? 0), i * 7 + 1);
    xref.writeUInt16BE(i === 0 ? 65535 : 0, i * 7 + 5);
  }
  object(
    7,
    Buffer.concat([
      Buffer.from(
        `<< /Type /XRef /Size 8 /Root 1 0 R /W [1 4 2] /Length ${xref.length} >>\nstream\n`,
      ),
      xref,
      Buffer.from('\nendstream'),
    ]),
  );
  chunks.push(Buffer.from(`startxref\n${offsets[7]}\n%%EOF\n`));
  const bytes = Buffer.concat(chunks);
  assert.ok(bytes.length < 1024 * 1024);
  await fs.writeFile(path.join(f.root, 'compressed.pdf'), bytes);
  const rejected = await f.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'expanded.pdf',
      sheets: [{ source_filename: 'compressed.pdf' }],
    },
  });
  assert.equal(rejected.isError, true);
  assert.match(JSON.stringify(rejected), /INVALID_PDF|FILE_TOO_LARGE/);
  await assert.rejects(fs.stat(path.join(f.root, 'expanded.pdf')), { code: 'ENOENT' });
  assert.equal((await fetch(new URL('/health', f.url))).status, 200);
  assert.equal(
    (await f.client.callTool({ name: 'workspace_files', arguments: {} })).isError,
    undefined,
  );
});

test('all native print boundaries must match the full sheet', async () => {
  const f = await fixture();
  for (const name of ['TrimBox', 'BleedBox', 'ArtBox']) {
    const pdf = await PDFDocument.create();
    const page = pdf.addPage([595, 842]);
    page.node.set(PDFName.of(name), pdf.context.obj([10, 10, 500, 700]));
    await fs.writeFile(path.join(f.root, `${name}.pdf`), await pdf.save());
    const rejected = await f.client.callTool({
      name: 'assemble_drawing_pdf',
      arguments: {
        pdf_filename: 'boundary.pdf',
        sheets: [{ source_filename: `${name}.pdf` }],
      },
    });
    assert.match(JSON.stringify(rejected), /PDF_PAGE_UNSUPPORTED/);
    await assert.rejects(fs.stat(path.join(f.root, 'boundary.pdf')), { code: 'ENOENT' });
  }
});

test('SVG encoding policy accepts UTF-8 BOMs and reports unsupported encodings explicitly', async () => {
  const f = await fixture();
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm"><rect x="10" y="10" width="50" height="20"/></svg>';
  const cases = [
    { bytes: Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(svg, 'utf16le')]), error: true },
    { bytes: Buffer.from('<?xml version="1.0" encoding="ISO-8859-1"?>' + svg), error: true },
    {
      bytes: Buffer.concat([
        Buffer.from([0xef, 0xbb, 0xbf]),
        Buffer.from('<?xml version="1.0" encoding="UTF-8"?>' + svg),
      ]),
      error: false,
    },
  ];
  for (const [index, source] of cases.entries()) {
    await fs.writeFile(path.join(f.root, 'encoded.svg'), source.bytes);
    const result = await f.client.callTool({
      name: 'export_drawing_pdf',
      arguments: {
        source_filename: 'encoded.svg',
        pdf_filename: `encoding-${index}.pdf`,
        page_width_mm: 210,
        page_height_mm: 297,
      },
    });
    if (source.error) {
      assert.match(JSON.stringify(result), /SVG_ENCODING_UNSUPPORTED/);
      await assert.rejects(fs.stat(path.join(f.root, `encoding-${index}.pdf`)), { code: 'ENOENT' });
    } else {
      assert.equal(result.isError, undefined);
    }
  }
});

test('drawing assembly rejects page and aggregate byte overflow, malformed and unscaled sources without writing', async () => {
  const f = await fixture({ maxPdfPages: 2 });
  const native = await PDFDocument.create();
  native.addPage();
  native.addPage();
  const source = Buffer.from(await native.save());
  await fs.writeFile(path.join(f.root, 'two.pdf'), source);
  await fs.writeFile(path.join(f.root, 'bad.pdf'), 'not a PDF: private input');
  await fs.writeFile(
    path.join(f.root, 'pixel.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="1191" height="842"><path d="M0 0h100"/></svg>',
  );
  const cases = [
    {
      sheets: [{ source_filename: 'two.pdf' }, { source_filename: 'two.pdf' }],
      code: 'PDF_PAGE_LIMIT',
    },
    {
      sheets: [{ source_filename: 'two.pdf', page_width_mm: 420 }],
      code: 'PDF_PAGE_SIZE_PRESERVED',
    },
    { sheets: [{ source_filename: 'pixel.svg' }], code: 'PAGE_SIZE_REQUIRED' },
    {
      sheets: [{ source_filename: 'pixel.svg', page_width_mm: 420, page_height_mm: 297 }],
      code: 'SVG_PHYSICAL_SIZE_REQUIRED',
    },
    { sheets: [{ source_filename: 'bad.pdf' }], code: 'INVALID_PDF' },
    { sheets: [{ source_filename: 'model.step' }], code: 'DRAWING_SHEET_REQUIRED' },
  ];
  for (const { sheets, code } of cases) {
    const rejected = await f.client.callTool({
      name: 'assemble_drawing_pdf',
      arguments: { pdf_filename: 'rejected.pdf', sheets },
    });
    assert.equal(rejected.isError, true);
    assert.match(JSON.stringify(rejected), new RegExp(code));
    assert.doesNotMatch(JSON.stringify(rejected), /private input/);
    await assert.rejects(fs.stat(path.join(f.root, 'rejected.pdf')), { code: 'ENOENT' });
  }
  const limited = await fixture({ maxFileBytes: source.length + 1 });
  await fs.writeFile(path.join(limited.root, 'two.pdf'), source);
  const overflow = await limited.client.callTool({
    name: 'assemble_drawing_pdf',
    arguments: {
      pdf_filename: 'overflow.pdf',
      sheets: [{ source_filename: 'two.pdf' }, { source_filename: 'two.pdf' }],
    },
  });
  assert.match(JSON.stringify(overflow), /FILE_TOO_LARGE/);
  await assert.rejects(fs.stat(path.join(limited.root, 'overflow.pdf')), { code: 'ENOENT' });
});

test('SVG exports reject external resources and DTDs instead of silently losing them', async () => {
  const f = await fixture();
  for (const content of [
    '<image href="file:///run/secrets/design.json" width="40" height="40"/>',
    '<image href="&#104;ttps://example.test/private.png" width="40" height="40"/>',
    '<style><![CDATA[@import url(https://example.test/font.css);]]></style>',
    '<rect style="fill:u/**/rl(https://example.test/resource.svg#paint)"/>',
    '<style><![CDATA[@im/**/port url(https://example.test/font.css);]]></style>',
    '<rect style="fill:url(file:///run/secrets/design.json)"/>',
    String.raw`<rect style="fill:u\72l(https://example.test/resource.svg#paint)"/>`,
    String.raw`<style><![CDATA[.part { fill:u\000072l(https://example.test/resource.svg#paint); }]]></style>`,
    '<style>rect{fill:u<![CDATA[rl(https://example.test/p.svg#paint)]]>}</style>',
    '<style>@im<![CDATA[port url(https://example.test/font.css);]]></style>',
    '<g xml:base="https://example.test/external.svg"><use href="#part"/></g>',
  ]) {
    await fs.writeFile(
      path.join(f.root, 'external.svg'),
      `<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm">${content}</svg>`,
    );
    const rejected = await f.client.callTool({
      name: 'export_drawing_pdf',
      arguments: {
        source_filename: 'external.svg',
        pdf_filename: 'external.pdf',
        page_width_mm: 210,
        page_height_mm: 297,
      },
    });
    assert.match(JSON.stringify(rejected), /SVG_NOT_SELF_CONTAINED/);
    assert.doesNotMatch(JSON.stringify(rejected), /run\/secrets|private.png|font.css/);
    await assert.rejects(fs.stat(path.join(f.root, 'external.pdf')), { code: 'ENOENT' });
  }
  await fs.writeFile(
    path.join(f.root, 'external.svg'),
    '<!DOCTYPE svg [<!ENTITY secret SYSTEM "file:///run/secrets/design.json">]><svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm"/>',
  );
  const dtd = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: { source_filename: 'external.svg', pdf_filename: 'external.pdf' },
  });
  assert.match(JSON.stringify(dtd), /SVG_NOT_SELF_CONTAINED/);
});

test('nested SVG data resources are rejected while genuine embedded raster images remain usable', async () => {
  const f = await fixture();
  const nested =
    '<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///private/source.png"/></svg>';
  const encoded = Buffer.from(nested).toString('base64');
  const cases = [
    'data:image/svg+xml;base64,' + encoded,
    'data:image/png;base64,' + encoded,
    'data:image/svg+xml,' + encodeURIComponent(nested),
  ];
  for (const [index, uri] of cases.entries()) {
    await fs.writeFile(
      path.join(f.root, 'nested-' + index + '.svg'),
      '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm"><image href="' +
        uri +
        '" width="20" height="20"/></svg>',
    );
    const result = await f.client.callTool({
      name: 'export_drawing_pdf',
      arguments: { source_filename: 'nested-' + index + '.svg', pdf_filename: 'nested.pdf' },
    });
    assert.equal(result.isError, true);
    assert.match(JSON.stringify(result), /SVG_NOT_SELF_CONTAINED/);
  }
  const png = await sharp({
    create: { width: 8, height: 8, channels: 3, background: '#ff0000' },
  })
    .png()
    .toBuffer();
  await fs.writeFile(
    path.join(f.root, 'raster.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm"><image href="data:image/png;base64,' +
      png.toString('base64') +
      '" width="20" height="20"/></svg>',
  );
  const result = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: { source_filename: 'raster.svg', pdf_filename: 'raster.pdf' },
  });
  assert.equal(result.isError, undefined);
  assert.equal(
    (await PDFDocument.load(await fs.readFile(path.join(f.root, 'raster.pdf')))).getPageCount(),
    1,
  );
  await assert.rejects(fs.stat(path.join(f.root, 'nested.pdf')), { code: 'ENOENT' });
});

test('missing wrapped renderers report deployment unavailability instead of blaming the PDF', async () => {
  const f = await fixture();
  await fs.symlink('/usr/bin/prlimit', path.join(f.root, 'prlimit'));
  const script = [
    "import assert from 'node:assert/strict';",
    "import { PDFDocument } from 'pdf-lib';",
    "import { render } from './render.ts';",
    "import { normalizeDrawingPdf } from './pdf.ts';",
    "import { settingsSchema, ToolFailure } from './config.ts';",
    "const settings = settingsSchema.parse({ workspaceUrl: 'https://example.test/cad/' });",
    'const pdf = await PDFDocument.create(); pdf.addPage(); const bytes = Buffer.from(await pdf.save());',
    'for (const operation of [',
    "() => render('rsvg-convert', [], Buffer.alloc(0), 4096, new AbortController().signal),",
    '() => normalizeDrawingPdf(bytes, settings, new AbortController().signal),',
    "]) await assert.rejects(operation(), error => error instanceof ToolFailure && error.code === 'PDF_RENDERER_UNAVAILABLE');",
    "console.log('Missing renderer classification verified.');",
  ].join('\n');
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
    env: { ...process.env, PATH: f.root },
    encoding: 'utf8',
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /Missing renderer classification verified/);
});

test('native renderers bound output and honor cancellation without exposing input diagnostics', async () => {
  const input = Buffer.from(
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="50" height="50"/></svg>',
  );
  await assert.rejects(
    render('rsvg-convert', ['--format=pdf'], input, 16, new AbortController().signal),
    (error: Error) => error instanceof ToolFailure && error.code === 'FILE_TOO_LARGE',
  );
  await assert.rejects(
    render(
      'rsvg-convert',
      ['--format=pdf'],
      Buffer.from('private malformed input'),
      1024,
      new AbortController().signal,
    ),
    (error: Error) =>
      error instanceof ToolFailure &&
      error.code === 'DRAWING_RENDER_FAILED' &&
      !error.message.includes('private'),
  );
  const controller = new AbortController();
  const running = render('rsvg-convert', ['--format=pdf'], input, 100000, controller.signal);
  controller.abort();
  await assert.rejects(
    running,
    (error: Error) => error instanceof ToolFailure && error.code === 'REQUEST_CANCELLED',
  );
});

test('native render memory is bounded before large allocations and the service remains usable', async () => {
  const input = Buffer.from(
    '<svg xmlns="http://www.w3.org/2000/svg" width="30000" height="30000"><rect width="30000" height="30000" fill="white"/></svg>',
  );
  await assert.rejects(
    render(
      'rsvg-convert',
      ['--format=png'],
      input,
      1024 * 1024,
      new AbortController().signal,
      64 * 1024 * 1024,
    ),
    (error: Error) => error instanceof ToolFailure && error.code === 'DRAWING_RENDER_FAILED',
  );
  const f = await fixture();
  assert.equal((await fetch(new URL('/health', f.url))).status, 200);
  assert.equal(
    (await f.client.callTool({ name: 'workspace_files', arguments: {} })).isError,
    undefined,
  );
});

test('large-sheet detail inspection retains readable annotation pixels and bounds the selected region', async () => {
  const f = await fixture();
  await fs.writeFile(
    path.join(f.root, 'detail.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="841mm" height="1189mm" viewBox="0 0 841 1189"><rect width="841" height="1189" fill="white"/><text x="100" y="104" font-family="sans-serif" font-size="2.5">HOLE 6 THRU</text></svg>',
  );
  const arguments_ = {
    source_filename: 'detail.svg',
    page_width_mm: 841,
    page_height_mm: 1189,
    left_mm: 98,
    top_mm: 99,
    width_mm: 45,
    height_mm: 10,
  };
  const result = await f.client.callTool({ name: 'inspect_drawing', arguments: arguments_ });
  assert.equal(result.isError, undefined);
  const preview = CallToolResultSchema.parse(result).content.find((item) => item.type === 'image');
  assert.ok(preview?.type === 'image');
  const bytes = Buffer.from(preview.data, 'base64');
  const { data, info } = await sharp(bytes)
    .removeAlpha()
    .raw()
    .toBuffer({ resolveWithObject: true });
  const inkRows = new Set<number>();
  for (let y = 0; y < info.height; y++) {
    for (let x = 0; x < info.width; x++) {
      if (data[(y * info.width + x) * info.channels] < 128) {
        inkRows.add(y);
        break;
      }
    }
  }
  assert.ok(inkRows.size >= 8, `Annotation covers only ${inkRows.size} pixel rows`);
  assert.ok(bytes.length <= f.settings.maxImageBytes);
  const rejected = await f.client.callTool({
    name: 'inspect_drawing',
    arguments: { ...arguments_, left_mm: 840 },
  });
  assert.equal(rejected.isError, true);
  assert.match(JSON.stringify(rejected), /REGION_OUT_OF_BOUNDS/);
});

test('large-sheet PDF detail previews budget the crop and retain readable annotation pixels', async () => {
  const f = await fixture({ maxImagePixels: 20000 });
  const pdf = await PDFDocument.create();
  const page = pdf.addPage([(841 * 72) / 25.4, (1189 * 72) / 25.4]);
  page.drawText('M6', { x: (10 * 72) / 25.4, y: page.getHeight() - (15 * 72) / 25.4, size: 12 });
  await fs.writeFile(path.join(f.root, 'detail.pdf'), await pdf.save());
  const result = await f.client.callTool({
    name: 'inspect_pdf',
    arguments: {
      pdf_filename: 'detail.pdf',
      page: 1,
      region: { left_mm: 9, top_mm: 11, width_mm: 20, height_mm: 10 },
    },
  });
  assert.equal(result.isError, undefined);
  const image = CallToolResultSchema.parse(result).content.find((item) => item.type === 'image');
  assert.ok(image?.type === 'image');
  const bytes = Buffer.from(image.data, 'base64');
  const metadata = await sharp(bytes).metadata();
  assert.ok(metadata.width && metadata.width >= 115);
  assert.ok(metadata.height && metadata.height >= 58);
  assert.ok(metadata.width * metadata.height <= f.settings.maxImagePixels);
  const stats = await sharp(bytes).stats();
  assert.ok(stats.channels.some((channel) => channel.min < 30));
});

test('a preview exceeding the shared byte cap does not commit a PDF', async () => {
  const f = await fixture({ maxImageBytes: 1 });
  await fs.writeFile(
    path.join(f.root, 'sheet.svg'),
    '<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297"><rect width="210" height="297" fill="white"/><text x="10" y="20">Visible text</text></svg>',
  );
  const rejected = await f.client.callTool({
    name: 'export_drawing_pdf',
    arguments: {
      source_filename: 'sheet.svg',
      pdf_filename: 'sheet.pdf',
      page_width_mm: 210,
      page_height_mm: 297,
    },
  });
  assert.equal(rejected.isError, true);
  assert.match(JSON.stringify(rejected), /IMAGE_TOO_LARGE/);
  await assert.rejects(fs.stat(path.join(f.root, 'sheet.pdf')), { code: 'ENOENT' });
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
