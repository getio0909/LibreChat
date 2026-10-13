import sharp from 'sharp';
import { SaxesParser } from 'saxes';
import { PDFDocument, PDFName, PDFNumber } from 'pdf-lib';
import type { Settings } from './config.ts';
import { ToolFailure } from './config.ts';
import { render } from './render.ts';

export type PaperSize = { widthMm: number; heightMm: number };
export type DrawingRegion = { leftMm: number; topMm: number; widthMm: number; heightMm: number };

function selfContainedSvg(bytes: Buffer, physical = false): void {
  const parser = new SaxesParser({ xmlns: true });
  let root = true;
  let inStyle = false;
  const unsupported = () => {
    throw new ToolFailure('SVG_NOT_SELF_CONTAINED');
  };
  const checkCss = (value: string) => {
    // 外部资源无法在无基准路径的渲染中可靠交付，拒绝而不是静默缺图。
    if (/@import|\\/i.test(value)) unsupported();
    for (const match of value.matchAll(/url\(\s*['"]?([^)'"\s]+)/gi)) {
      if (!/^(#|data:)/i.test(match[1])) unsupported();
    }
  };
  parser.on('error', () => {
    throw new ToolFailure('INVALID_SVG');
  });
  parser.on('doctype', unsupported);
  parser.on('processinginstruction', (instruction) => {
    if (instruction.target === 'xml-stylesheet') unsupported();
  });
  parser.on('opentag', (tag) => {
    if (root) {
      root = false;
      if (tag.local !== 'svg' || tag.uri !== 'http://www.w3.org/2000/svg')
        throw new ToolFailure('INVALID_SVG');
      if (
        physical &&
        !['width', 'height'].every((name) =>
          /^\s*(?:\d+(?:\.\d*)?|\.\d+)\s*(?:mm|cm|in|pt|pc)\s*$/.test(
            tag.attributes[name]?.value ?? '',
          ),
        )
      )
        throw new ToolFailure('SVG_PHYSICAL_SIZE_REQUIRED');
    }
    for (const attribute of Object.values(tag.attributes)) {
      if (attribute.local === 'href' && !/^(#|data:)/i.test(attribute.value)) unsupported();
      checkCss(attribute.value);
    }
    if (tag.local === 'style') inStyle = true;
  });
  parser.on('closetag', (tag) => {
    if (tag.local === 'style') inStyle = false;
  });
  parser.on('text', (value) => {
    if (inStyle) checkCss(value);
  });
  parser.on('cdata', (value) => {
    if (inStyle) checkCss(value);
  });
  parser.write(bytes.toString('utf8')).close();
}

async function renderDrawing(
  bytes: Buffer,
  maxImagePixels: number,
  paper?: PaperSize,
): Promise<{ bytes: Buffer; width: number; height: number }> {
  const metadata = await sharp(bytes, { density: 72, limitInputPixels: maxImagePixels }).metadata();
  if (metadata.format === 'svg') selfContainedSvg(bytes, paper != null);
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
  settings: Settings,
  signal: AbortSignal,
  paper?: PaperSize,
): Promise<Buffer> {
  signal.throwIfAborted();
  if (
    paper &&
    Math.ceil((paper.widthMm * 72) / 25.4) * Math.ceil((paper.heightMm * 72) / 25.4) >
      settings.maxImagePixels
  )
    throw new ToolFailure('PAGE_PIXEL_LIMIT');
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
    selfContainedSvg(bytes, paper != null);
    const scale = paper
      ? 1
      : Math.min((pageSize.width - 48) / source.width, (pageSize.height - 48) / source.height);
    const width = paper ? pageSize.width : source.width * scale;
    const height = paper ? pageSize.height : source.height * scale;
    return render(
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
    );
  }
  const { bytes: png } = await renderDrawing(bytes, settings.maxImagePixels);
  const pdf = await PDFDocument.create();
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

export async function loadDrawingPdf(bytes: Buffer, maxPages: number): Promise<PDFDocument> {
  let pdf: PDFDocument;
  try {
    pdf = await PDFDocument.load(bytes, { throwOnInvalidObject: true });
  } catch {
    throw new ToolFailure('INVALID_PDF');
  }
  if (pdf.getPageCount() < 1 || pdf.getPageCount() > maxPages)
    throw new ToolFailure('PDF_PAGE_LIMIT');
  if (pdf.catalog.has(PDFName.of('OCProperties'))) throw new ToolFailure('PDF_LAYERS_UNSUPPORTED');
  if (pdf.catalog.has(PDFName.of('AcroForm'))) throw new ToolFailure('PDF_FORMS_UNSUPPORTED');
  for (const page of pdf.getPages()) {
    const unit = page.node.get(PDFName.of('UserUnit'));
    if (unit != null && (!(unit instanceof PDFNumber) || unit.asNumber() !== 1))
      throw new ToolFailure('PDF_PAGE_UNSUPPORTED');
    const media = page.getMediaBox();
    const crop = page.getCropBox();
    for (const box of [media, crop]) {
      if (
        ![box.x, box.y, box.width, box.height].every(Number.isFinite) ||
        ![box.width, box.height].every((v) => v > 0 && v <= (1200 * 72) / 25.4)
      )
        throw new ToolFailure('PDF_PAGE_UNSUPPORTED');
    }
    // 制造图纸必须检查完整纸张，拒绝打印区域与预览区域不同的源页。
    if (
      Math.abs(media.x - crop.x) > 0.001 ||
      Math.abs(media.y - crop.y) > 0.001 ||
      Math.abs(media.width - crop.width) > 0.001 ||
      Math.abs(media.height - crop.height) > 0.001
    )
      throw new ToolFailure('PDF_PAGE_UNSUPPORTED');
    const rotation = ((page.getRotation().angle % 360) + 360) % 360;
    if (![0, 90, 180, 270].includes(rotation)) throw new ToolFailure('PDF_PAGE_UNSUPPORTED');
  }
  return pdf;
}

export async function pdfPreview(
  bytes: Buffer,
  settings: Settings,
  signal: AbortSignal,
  pageNumber = 1,
  region?: DrawingRegion,
): Promise<{ preview: Buffer; paper: PaperSize; pageCount: number }> {
  const pdf = await loadDrawingPdf(bytes, settings.maxPdfPages);
  if (pageNumber > pdf.getPageCount()) throw new ToolFailure('PDF_PAGE_NOT_FOUND');
  const page = pdf.getPage(pageNumber - 1);
  const box = page.getCropBox();
  const rotated = page.getRotation().angle % 180 !== 0;
  const widthPt = rotated ? box.height : box.width;
  const heightPt = rotated ? box.width : box.height;
  const paper = { widthMm: (widthPt * 25.4) / 72, heightMm: (heightPt * 25.4) / 72 };
  if (Math.ceil(widthPt) * Math.ceil(heightPt) > settings.maxImagePixels)
    throw new ToolFailure('PAGE_PIXEL_LIMIT');
  const args = ['-png', '-singlefile', '-cropbox', '-f', `${pageNumber}`, '-l', `${pageNumber}`];
  if (region) {
    if (
      region.leftMm + region.widthMm > paper.widthMm + 0.001 ||
      region.topMm + region.heightMm > paper.heightMm + 0.001
    )
      throw new ToolFailure('REGION_OUT_OF_BOUNDS');
    let density = 150;
    while (
      Math.ceil((widthPt * density) / 72) * Math.ceil((heightPt * density) / 72) >
      settings.maxImagePixels
    )
      density -= 1;
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
  const preview = await render('pdftoppm', [...args, '-'], bytes, settings.maxFileBytes, signal);
  return { preview, paper, pageCount: pdf.getPageCount() };
}
