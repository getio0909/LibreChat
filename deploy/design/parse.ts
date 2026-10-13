import { z } from 'zod';
import { Worker, parentPort, workerData } from 'node:worker_threads';
import {
  PDFDocument,
  PDFName,
  PDFNumber,
  PDFArray,
  PDFDict,
  PDFRef,
  PDFStream,
  PDFString,
  PDFHexString,
  PrintScaling,
} from 'pdf-lib';
import type { PDFObject } from 'pdf-lib';
import type { Settings } from './config.ts';
import { ToolFailure } from './config.ts';

const limitsSchema = z.object({
  maxFileBytes: z
    .number()
    .int()
    .positive()
    .max(100 * 1024 * 1024),
  maxPdfPages: z.number().int().min(1).max(128),
});
const bytesSchema = z.custom<Uint8Array<ArrayBufferLike>>((value) => value instanceof Uint8Array);
const sourceSchema = z.object({ source_filename: z.string(), bytes: bytesSchema });
const pageSchema = z.object({
  page: z.number().int().positive(),
  source_filename: z.string(),
  source_page: z.number().int().positive(),
});
const inputSchema = z.discriminatedUnion('operation', [
  z.object({ operation: z.literal('prepare'), bytes: bytesSchema, limits: limitsSchema }),
  z.object({
    operation: z.literal('inspect'),
    bytes: bytesSchema,
    page: z.number().int().positive(),
    limits: limitsSchema,
  }),
  z.object({
    operation: z.literal('assemble'),
    sources: z.array(sourceSchema).min(1).max(128),
    limits: limitsSchema,
  }),
]);
const resultSchema = z.discriminatedUnion('operation', [
  z.object({
    operation: z.literal('prepare'),
    bytes: bytesSchema,
    pageCount: z.number().int().positive(),
  }),
  z.object({
    operation: z.literal('inspect'),
    pageCount: z.number().int().positive(),
    paper: z.object({ widthMm: z.number().positive(), heightMm: z.number().positive() }),
  }),
  z.object({
    operation: z.literal('assemble'),
    bytes: bytesSchema,
    pageCount: z.number().int().positive(),
    pages: z.array(pageSchema),
  }),
]);
const packetSchema = z.discriminatedUnion('ok', [
  z.object({ ok: z.literal(true), value: resultSchema }),
  z.object({ ok: z.literal(false), error: z.object({ code: z.string().regex(/^[A-Z_]+$/) }) }),
]);
type PdfInput = z.infer<typeof inputSchema>;
type PdfResult = z.infer<typeof resultSchema>;
type PdfLimits = z.infer<typeof limitsSchema>;
type Packet = z.infer<typeof packetSchema>;
export type PdfSource = z.infer<typeof sourceSchema>;

async function readPdf(bytes: Uint8Array, settings: PdfLimits): Promise<PDFDocument> {
  let pdf: PDFDocument;
  try {
    pdf = await PDFDocument.load(bytes, { throwOnInvalidObject: true });
  } catch {
    throw new ToolFailure('INVALID_PDF');
  }
  if (pdf.getPageCount() < 1 || pdf.getPageCount() > settings.maxPdfPages)
    throw new ToolFailure('PDF_PAGE_LIMIT');
  if (pdf.catalog.has(PDFName.of('OCProperties'))) throw new ToolFailure('PDF_LAYERS_UNSUPPORTED');
  if (pdf.catalog.has(PDFName.of('AcroForm'))) throw new ToolFailure('PDF_FORMS_UNSUPPORTED');
  if (pdf.catalog.has(PDFName.of('OutputIntents')))
    throw new ToolFailure('PDF_COLOR_PROFILE_UNSUPPORTED');
  const pages = pdf.getPages();
  for (const page of pages) {
    // 缩略图不参与当前图纸渲染，移除后避免携带旧版本的隐藏画面。
    page.node.delete(PDFName.of('Thumb'));
    page.node.delete(PDFName.of('B'));
    if (page.node.has(PDFName.of('PresSteps'))) throw new ToolFailure('PDF_ACTIONS_UNSUPPORTED');
  }
  const roots = new Set<PDFObject>(pages.map((page) => page.node));
  const pending: PDFObject[] = [...roots];
  for (const page of pages) {
    const resources = page.node.Resources();
    if (resources) pending.push(resources);
  }
  const inspected = new Set<PDFObject>();
  while (pending.length) {
    const object = pending.pop();
    if (!object || inspected.has(object)) continue;
    inspected.add(object);
    if (object instanceof PDFRef) {
      const value = pdf.context.lookup(object);
      if (value) pending.push(value);
    } else if (object instanceof PDFArray) {
      for (const value of object.asArray()) pending.push(value);
    } else if (object instanceof PDFStream) {
      if (['F', 'FFilter', 'FDecodeParms'].some((key) => object.dict.has(PDFName.of(key))))
        throw new ToolFailure('PDF_EXTERNAL_STREAM_UNSUPPORTED');
      pending.push(object.dict);
    } else if (object instanceof PDFDict) {
      const type = object.lookup(PDFName.of('Type'));
      const subtype = object.lookup(PDFName.of('Subtype'));
      const image = subtype === PDFName.of('Image');
      const form = subtype === PDFName.of('Form');
      if (type === PDFName.of('XObject') && !image && !form)
        throw new ToolFailure('PDF_XOBJECT_UNSUPPORTED');
      // 元数据不参与图纸渲染，发布副本不携带源文件的隐私或旧版本信息。
      const metadata = object.lookup(PDFName.of('Metadata'));
      if (
        type === PDFName.of('Page') ||
        image ||
        form ||
        (metadata instanceof PDFStream &&
          (metadata.dict.lookup(PDFName.of('Type')) === PDFName.of('Metadata') ||
            metadata.dict.lookup(PDFName.of('Subtype')) === PDFName.of('XML')))
      )
        object.delete(PDFName.of('Metadata'));
      if (
        (form && object.has(PDFName.of('Ref'))) ||
        ((image || form) && object.has(PDFName.of('OPI')))
      )
        throw new ToolFailure('PDF_EXTERNAL_STREAM_UNSUPPORTED');
      if (image && object.has(PDFName.of('Alternates')))
        throw new ToolFailure('PDF_IMAGE_ALTERNATES_UNSUPPORTED');
      if (
        ((image || form) && object.has(PDFName.of('OC'))) ||
        type === PDFName.of('OCG') ||
        type === PDFName.of('OCMD')
      )
        throw new ToolFailure('PDF_LAYERS_UNSUPPORTED');
      const pieceInfo = object.lookup(PDFName.of('PieceInfo'));
      const privatePieces =
        pieceInfo instanceof PDFDict &&
        pieceInfo.entries().some(([, entry]) => {
          const value = pdf.context.lookup(entry);
          return value instanceof PDFDict && value.has(PDFName.of('Private'));
        });
      const filename = object.lookup(PDFName.of('F')) ?? object.lookup(PDFName.of('UF'));
      if (
        ((type === PDFName.of('Page') || image || form) &&
          (object.has(PDFName.of('AF')) || object.has(PDFName.of('PieceInfo')))) ||
        (object.has(PDFName.of('EF')) &&
          (filename instanceof PDFString || filename instanceof PDFHexString)) ||
        privatePieces ||
        type === PDFName.of('Filespec') ||
        type === PDFName.of('EmbeddedFile')
      )
        throw new ToolFailure('PDF_ATTACHMENTS_UNSUPPORTED');
      for (const [key, value] of object.entries()) {
        // 复制页会剥离父节点，继承资源已单独加入检查队列。
        if (roots.has(object) && key === PDFName.of('Parent')) continue;
        pending.push(value);
      }
    }
  }
  for (const page of pages) {
    if (page.node.has(PDFName.of('AF'))) throw new ToolFailure('PDF_ATTACHMENTS_UNSUPPORTED');
    if (page.node.has(PDFName.of('AA')) || (page.node.Annots()?.size() ?? 0) > 0)
      throw new ToolFailure('PDF_ACTIONS_UNSUPPORTED');
    const unit = page.node.lookup(PDFName.of('UserUnit'));
    if (unit != null && (!(unit instanceof PDFNumber) || unit.asNumber() !== 1))
      throw new ToolFailure('PDF_PAGE_UNSUPPORTED');
    const media = page.getMediaBox();
    const boundaries = [page.getCropBox(), page.getBleedBox(), page.getTrimBox(), page.getArtBox()];
    for (const box of [media, ...boundaries]) {
      if (
        ![box.x, box.y, box.width, box.height].every(Number.isFinite) ||
        ![box.width, box.height].every((v) => v > 0 && v <= (1200 * 72) / 25.4)
      )
        throw new ToolFailure('PDF_PAGE_UNSUPPORTED');
    }
    // 制造图纸必须检查完整纸张，拒绝打印区域与预览区域不同的源页。
    if (
      boundaries.some(
        (box) =>
          Math.abs(media.x - box.x) > 0.001 ||
          Math.abs(media.y - box.y) > 0.001 ||
          Math.abs(media.width - box.width) > 0.001 ||
          Math.abs(media.height - box.height) > 0.001,
      )
    )
      throw new ToolFailure('PDF_PAGE_UNSUPPORTED');
    const rotation = ((page.getRotation().angle % 360) + 360) % 360;
    if (![0, 90, 180, 270].includes(rotation)) throw new ToolFailure('PDF_PAGE_UNSUPPORTED');
  }
  return pdf;
}

async function processInput(input: PdfInput): Promise<PdfResult> {
  const sources =
    input.operation === 'assemble' ? input.sources : [{ source_filename: '', bytes: input.bytes }];
  if (
    sources.reduce((total, source) => total + source.bytes.byteLength, 0) >
    input.limits.maxFileBytes
  )
    throw new ToolFailure('FILE_TOO_LARGE');
  if (input.operation === 'inspect') {
    const pdf = await readPdf(input.bytes, input.limits);
    if (input.page > pdf.getPageCount()) throw new ToolFailure('PDF_PAGE_NOT_FOUND');
    const page = pdf.getPage(input.page - 1);
    const rotated = page.getRotation().angle % 180 !== 0;
    return {
      operation: 'inspect',
      pageCount: pdf.getPageCount(),
      paper: {
        widthMm: ((rotated ? page.getHeight() : page.getWidth()) * 25.4) / 72,
        heightMm: ((rotated ? page.getWidth() : page.getHeight()) * 25.4) / 72,
      },
    };
  }
  const combined = await PDFDocument.create();
  combined.catalog.getOrCreateViewerPreferences().setPrintScaling(PrintScaling.None);
  const pages: z.infer<typeof pageSchema>[] = [];
  for (const source of sources) {
    const pdf = await readPdf(source.bytes, input.limits);
    if (combined.getPageCount() + pdf.getPageCount() > input.limits.maxPdfPages)
      throw new ToolFailure('PDF_PAGE_LIMIT');
    const copied = await combined.copyPages(pdf, pdf.getPageIndices());
    for (const [index, page] of copied.entries()) {
      combined.addPage(page);
      pages.push({
        page: pages.length + 1,
        source_filename: source.source_filename,
        source_page: index + 1,
      });
    }
  }
  const bytes = await combined.save();
  if (bytes.byteLength > input.limits.maxFileBytes) throw new ToolFailure('FILE_TOO_LARGE');
  return input.operation === 'prepare'
    ? { operation: 'prepare', bytes, pageCount: pages.length }
    : { operation: 'assemble', bytes, pageCount: pages.length, pages };
}

if (parentPort) {
  const port = parentPort;
  processInput(inputSchema.parse(workerData))
    .then((value) => {
      if ('bytes' in value) {
        if (!(value.bytes.buffer instanceof ArrayBuffer)) throw new ToolFailure('INVALID_PDF');
        port.postMessage({ ok: true, value }, [value.bytes.buffer]);
      } else {
        port.postMessage({ ok: true, value });
      }
    })
    .catch((error) => {
      port.postMessage({
        ok: false,
        error: { code: error instanceof ToolFailure ? error.code : 'INVALID_PDF' },
      });
    })
    .finally(() => port.close());
}

export function pdfTask(
  input: Extract<PdfInput, { operation: 'prepare' }>,
  settings: Settings,
  signal: AbortSignal,
): Promise<Extract<PdfResult, { operation: 'prepare' }>>;
export function pdfTask(
  input: Extract<PdfInput, { operation: 'inspect' }>,
  settings: Settings,
  signal: AbortSignal,
): Promise<Extract<PdfResult, { operation: 'inspect' }>>;
export function pdfTask(
  input: Extract<PdfInput, { operation: 'assemble' }>,
  settings: Settings,
  signal: AbortSignal,
): Promise<Extract<PdfResult, { operation: 'assemble' }>>;
export function pdfTask(
  input: PdfInput,
  settings: Settings,
  signal: AbortSignal,
): Promise<PdfResult> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    // 线程只解析、检查和复制 PDF，不启动子进程；取消可终止全部解析工作。
    const worker = new Worker(new URL('./parse.ts', import.meta.url), {
      workerData: inputSchema.parse(input),
      resourceLimits: {
        maxOldGenerationSizeMb: Math.min(
          128,
          Math.max(16, Math.floor(settings.maxRenderMemoryBytes / (1024 * 1024)) - 48),
        ),
        maxYoungGenerationSizeMb: 8,
        stackSizeMb: 2,
      },
    });
    let packet: Packet | undefined;
    let failure: ToolFailure | undefined;
    const cancel = () => {
      void worker.terminate();
    };
    signal.addEventListener('abort', cancel, { once: true });
    if (signal.aborted) cancel();
    worker.on('message', (message: unknown) => {
      const result = packetSchema.safeParse(message);
      if (!result.success || (result.data.ok && result.data.value.operation !== input.operation)) {
        failure = new ToolFailure('INVALID_PDF');
        return;
      }
      packet = result.data;
    });
    worker.on('error', (error: Error & { code?: string }) => {
      failure = new ToolFailure(
        error.code === 'ERR_WORKER_OUT_OF_MEMORY' ? 'FILE_TOO_LARGE' : 'INVALID_PDF',
      );
    });
    // 线程退出后才释放调用方的工作名额，避免取消后继续消耗内存和 CPU。
    worker.once('exit', (code) => {
      signal.removeEventListener('abort', cancel);
      if (signal.aborted) return reject(new ToolFailure('REQUEST_CANCELLED'));
      if (failure) return reject(failure);
      if (code !== 0 || !packet) return reject(new ToolFailure('INVALID_PDF'));
      if (!packet.ok) return reject(new ToolFailure(packet.error.code));
      resolve(packet.value);
    });
  });
}
