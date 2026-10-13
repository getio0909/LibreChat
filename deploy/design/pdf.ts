import sharp from 'sharp';
import { PDFDocument } from 'pdf-lib';
import { ToolFailure } from './config.ts';

export type PaperSize = { widthMm: number; heightMm: number };
export type DrawingRegion = { leftMm: number; topMm: number; widthMm: number; heightMm: number };

async function renderDrawing(
  bytes: Buffer,
  maxImagePixels: number,
  paper?: PaperSize,
): Promise<{ bytes: Buffer; width: number; height: number }> {
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
  const rendered = await sharp(bytes, { density, limitInputPixels: maxImagePixels })
    .png()
    .toBuffer({ resolveWithObject: true });
  return { bytes: rendered.data, width: rendered.info.width, height: rendered.info.height };
}

export async function drawingRegion(
  bytes: Buffer,
  maxImagePixels: number,
  paper: PaperSize,
  region: DrawingRegion,
): Promise<Buffer> {
  if (
    region.leftMm + region.widthMm > paper.widthMm ||
    region.topMm + region.heightMm > paper.heightMm
  )
    throw new ToolFailure('REGION_OUT_OF_BOUNDS');
  const rendered = await renderDrawing(bytes, maxImagePixels, paper);
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
  return sharp(rendered.bytes, { limitInputPixels: maxImagePixels })
    .extract({ left, top, width, height })
    .flatten({ background: 'white' })
    .png()
    .toBuffer();
}

export async function drawingPdf(
  bytes: Buffer,
  maxImagePixels: number,
  paper?: PaperSize,
): Promise<{ bytes: Buffer; preview: Buffer }> {
  const { bytes: png } = await renderDrawing(bytes, maxImagePixels, paper);
  const widthPt = paper ? (paper.widthMm * 72) / 25.4 : undefined;
  const heightPt = paper ? (paper.heightMm * 72) / 25.4 : undefined;
  const pdf = await PDFDocument.create();
  const image = await pdf.embedPng(png);
  if (widthPt != null && heightPt != null) {
    const page = pdf.addPage([widthPt, heightPt]);
    page.drawImage(image, { x: 0, y: 0, width: widthPt, height: heightPt });
  } else {
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
  }
  const preview = await sharp(png, { limitInputPixels: maxImagePixels })
    .flatten({ background: 'white' })
    .resize({ width: 1600, height: 1600, fit: 'inside', withoutEnlargement: true })
    .png()
    .toBuffer();
  return { bytes: Buffer.from(await pdf.save()), preview };
}
