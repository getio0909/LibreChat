import sharp from 'sharp';
import os from 'node:os';
import path from 'node:path';
import * as fs from 'node:fs/promises';

import { PDFDocument, PrintScaling } from 'pdf-lib';
import type { Settings } from './config.ts';
import { ToolFailure } from './config.ts';
import { render } from './render.ts';
import { pdfTask } from './parse.ts';

export type PaperSize = { widthMm: number; heightMm: number };
export type DrawingRegion = { leftMm: number; topMm: number; widthMm: number; heightMm: number };

async function validateSvg(
  bytes: Buffer,
  settings: Settings,
  signal: AbortSignal,
  paper?: PaperSize,
): Promise<void> {
  // 验证副本转交有界线程，原始输入保留用于原生渲染；取消等待线程退出。
  await pdfTask(
    { operation: 'validateSvg', bytes: new Uint8Array(bytes), paper, limits: settings },
    settings,
    signal,
  );
}
async function renderDrawing(
  bytes: Buffer,
  settings: Settings,
  signal: AbortSignal,
  paper?: PaperSize,
): Promise<{ bytes: Buffer; width: number; height: number }> {
  const maxImagePixels = settings.maxImagePixels;
  if (paper) await validateSvg(bytes, settings, signal, paper);
  const metadata = await sharp(bytes, { density: 72, limitInputPixels: maxImagePixels }).metadata();
  if (metadata.format === 'svg' && !paper) await validateSvg(bytes, settings, signal);
  const widthPt = paper ? (paper.widthMm * 72) / 25.4 : undefined;
  const heightPt = paper ? (paper.heightMm * 72) / 25.4 : undefined;
  let density = 150;
  if (widthPt != null && heightPt != null) {
    if (Math.ceil(widthPt) * Math.ceil(heightPt) > maxImagePixels)
      throw new ToolFailure('PAGE_PIXEL_LIMIT');
    const source = await sharp(bytes, { density: 72, limitInputPixels: maxImagePixels }).metadata();
    if (source.format !== 'svg') throw new ToolFailure('SVG_SHEET_REQUIRED');
    // 使用渲染器解析的物理尺寸，拒绝改变纸张大小而悄悄缩放图纸。
    if (
      source.width == null ||
      source.height == null ||
      Math.abs(source.width - widthPt) > 1 ||
      Math.abs(source.height - heightPt) > 1
    ) {
      throw new ToolFailure('PAGE_SIZE_MISMATCH');
    }
    // 根据渲染器实际解析的像素预算选择分辨率，纸张尺寸和标注比例不变。
    let low = 72;
    let high = density;
    while (low < high) {
      const candidate = Math.ceil((low + high) / 2);
      try {
        await sharp(bytes, { density: candidate, limitInputPixels: maxImagePixels }).metadata();
        low = candidate;
      } catch (error) {
        if (!(error instanceof Error) || error.message !== 'Input image exceeds pixel limit')
          throw error;
        high = candidate - 1;
      }
    }
    density = low;
  }
  if (metadata.format === 'svg') {
    const png = await render(
      'rsvg-convert',
      ['--format=png', `--dpi-x=${density}`, `--dpi-y=${density}`],
      bytes,
      settings.maxFileBytes,
      signal,
      settings.maxRenderMemoryBytes,
    );
    const size = await sharp(png, { limitInputPixels: maxImagePixels }).metadata();
    if (!size.width || !size.height) throw new ToolFailure('DRAWING_RENDER_FAILED');
    return { bytes: png, width: size.width, height: size.height };
  }
  const rendered = await sharp(bytes, { density, limitInputPixels: maxImagePixels })
    .png()
    .toBuffer({ resolveWithObject: true });
  return { bytes: rendered.data, width: rendered.info.width, height: rendered.info.height };
}

export async function drawingRegion(
  bytes: Buffer,
  settings: Settings,
  signal: AbortSignal,
  paper: PaperSize,
  region: DrawingRegion,
): Promise<Buffer> {
  if (
    region.leftMm + region.widthMm > paper.widthMm ||
    region.topMm + region.heightMm > paper.heightMm
  )
    throw new ToolFailure('REGION_OUT_OF_BOUNDS');
  const rendered = await renderDrawing(bytes, settings, signal, paper);
  const left = Math.floor((region.leftMm / paper.widthMm) * rendered.width);
  const top = Math.floor((region.topMm / paper.heightMm) * rendered.height);
  const width = Math.min(
    rendered.width - left,
    Math.max(1, Math.ceil((region.widthMm / paper.widthMm) * rendered.width)),
  );
  const height = Math.min(
    rendered.height - top,
    Math.max(1, Math.ceil((region.heightMm / paper.heightMm) * rendered.height)),
  );
  return sharp(rendered.bytes, { limitInputPixels: settings.maxImagePixels })
    .extract({ left, top, width, height })
    .flatten({ background: 'white' })
    .png()
    .toBuffer();
}

export async function drawingPdf(
  bytes: Buffer,
  settings: Settings,
  signal: AbortSignal,
  paper?: PaperSize,
  svg = false,
): Promise<Buffer> {
  signal.throwIfAborted();
  if (
    paper &&
    Math.ceil((paper.widthMm * 72) / 25.4) * Math.ceil((paper.heightMm * 72) / 25.4) >
      settings.maxImagePixels
  )
    throw new ToolFailure('PAGE_PIXEL_LIMIT');
  if (svg) await validateSvg(bytes, settings, signal, paper);
  const source = await sharp(bytes, {
    density: 72,
    limitInputPixels: settings.maxImagePixels,
  }).metadata();
  if (!source.width || !source.height) throw new ToolFailure('INVALID_DRAWING');
  const pageSize = paper
    ? { width: (paper.widthMm * 72) / 25.4, height: (paper.heightMm * 72) / 25.4 }
    : source.width > source.height
      ? { width: 841.89, height: 595.28 }
      : { width: 595.28, height: 841.89 };
  if (paper) {
    if (source.format !== 'svg') throw new ToolFailure('SVG_SHEET_REQUIRED');
    if (
      Math.abs(source.width - pageSize.width) > 1 ||
      Math.abs(source.height - pageSize.height) > 1
    )
      throw new ToolFailure('PAGE_SIZE_MISMATCH');
  }
  if (source.format === 'svg') {
    if (!svg) await validateSvg(bytes, settings, signal, paper);
    const scale = paper
      ? 1
      : Math.min((pageSize.width - 48) / source.width, (pageSize.height - 48) / source.height);
    const width = paper ? pageSize.width : source.width * scale;
    const height = paper ? pageSize.height : source.height * scale;
    const rendered = await render(
      'rsvg-convert',
      [
        '--format=pdf',
        '--dpi-x=72',
        '--dpi-y=72',
        `--page-width=${pageSize.width}pt`,
        `--page-height=${pageSize.height}pt`,
        `--width=${width}pt`,
        `--height=${height}pt`,
        '--keep-aspect-ratio',
        `--left=${(pageSize.width - width) / 2}pt`,
        `--top=${(pageSize.height - height) / 2}pt`,
      ],
      bytes,
      settings.maxFileBytes,
      signal,
      settings.maxRenderMemoryBytes,
    );
    const result = await pdfTask(
      { operation: 'prepare', bytes: rendered, limits: settings },
      settings,
      signal,
    );
    return Buffer.from(result.bytes.buffer, result.bytes.byteOffset, result.bytes.byteLength);
  }
  const { bytes: png } = await renderDrawing(bytes, settings, signal);
  const pdf = await PDFDocument.create();
  pdf.catalog.getOrCreateViewerPreferences().setPrintScaling(PrintScaling.None);
  const image = await pdf.embedPng(png);
  const page = pdf.addPage(image.width > image.height ? [841.89, 595.28] : [595.28, 841.89]);
  const scale = Math.min(
    (page.getWidth() - 48) / image.width,
    (page.getHeight() - 48) / image.height,
  );
  const width = image.width * scale;
  const height = image.height * scale;
  page.drawImage(image, {
    x: (page.getWidth() - width) / 2,
    y: (page.getHeight() - height) / 2,
    width,
    height,
  });
  signal.throwIfAborted();
  return Buffer.from(await pdf.save());
}

export async function normalizeDrawingPdf(
  bytes: Buffer,
  settings: Settings,
  signal: AbortSignal,
  normalizedLimit = settings.maxFileBytes,
): Promise<Buffer> {
  if (normalizedLimit < 1) throw new ToolFailure('FILE_TOO_LARGE');
  signal.throwIfAborted();
  const folder = await fs.mkdtemp(path.join(os.tmpdir(), 'cad-pdf-'));
  let normalized: Buffer;
  try {
    const input = path.join(folder, 'source.pdf');
    await fs.writeFile(input, bytes, { mode: 0o600, signal });
    // 先在受限进程中展开对象流，再在线程中解析有界普通对象。
    normalized = await render(
      'qpdf',
      [
        '--object-streams=disable',
        '--stream-data=preserve',
        '--remove-unreferenced-resources=yes',
        input,
        '-',
      ],
      Buffer.alloc(0),
      normalizedLimit,
      signal,
      settings.maxRenderMemoryBytes,
    );
  } catch (error) {
    if (error instanceof ToolFailure && error.code === 'DRAWING_RENDER_FAILED')
      throw new ToolFailure('INVALID_PDF');
    throw error;
  } finally {
    await fs.rm(folder, { recursive: true, force: true });
  }
  return normalized;
}

export async function pdfPreview(
  bytes: Buffer,
  settings: Settings,
  signal: AbortSignal,
  pageNumber = 1,
  region?: DrawingRegion,
): Promise<{ preview: Buffer; paper: PaperSize; pageCount: number }> {
  const normalized = await normalizeDrawingPdf(bytes, settings, signal);
  const { paper, pageCount } = await pdfTask(
    { operation: 'inspect', bytes: normalized, page: pageNumber, limits: settings },
    settings,
    signal,
  );
  const widthPt = (paper.widthMm * 72) / 25.4;
  const heightPt = (paper.heightMm * 72) / 25.4;
  const args = ['-png', '-singlefile', '-cropbox', '-f', `${pageNumber}`, '-l', `${pageNumber}`];
  if (region) {
    if (
      region.leftMm + region.widthMm > paper.widthMm + 0.001 ||
      region.topMm + region.heightMm > paper.heightMm + 0.001
    )
      throw new ToolFailure('REGION_OUT_OF_BOUNDS');
    let density = Math.min(
      150,
      Math.sqrt(settings.maxImagePixels / (region.widthMm * region.heightMm)) * 25.4 * 0.99,
    );
    while (
      Math.ceil((region.widthMm * density) / 25.4) * Math.ceil((region.heightMm * density) / 25.4) >
      settings.maxImagePixels
    )
      density *= 0.99;
    const pixelsPerMm = density / 25.4;
    const left = Math.floor(region.leftMm * pixelsPerMm);
    const top = Math.floor(region.topMm * pixelsPerMm);
    const width = Math.min(
      Math.ceil((widthPt * density) / 72) - left,
      Math.ceil(region.widthMm * pixelsPerMm),
    );
    const height = Math.min(
      Math.ceil((heightPt * density) / 72) - top,
      Math.ceil(region.heightMm * pixelsPerMm),
    );
    args.push(
      '-r',
      `${density}`,
      '-x',
      `${left}`,
      '-y',
      `${top}`,
      '-W',
      `${width}`,
      '-H',
      `${height}`,
    );
  } else {
    const maxSide = Math.min(1600, Math.floor(Math.sqrt(settings.maxImagePixels)));
    args.push('-scale-to', `${maxSide}`);
  }
  const preview = await render(
    'pdftoppm',
    [...args, '-'],
    bytes,
    settings.maxFileBytes,
    signal,
    settings.maxRenderMemoryBytes,
  );
  return { preview, paper, pageCount };
}
