import { z } from 'zod';
import path from 'node:path';
import * as fs from 'node:fs/promises';
import { PDFDocument } from 'pdf-lib';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import type { RequestListener } from 'node:http';
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js';
import type { ImageProvider, ImageData } from './images.ts';
import type { Settings } from './config.ts';
import { ToolFailure } from './config.ts';
import { fileType, fileInfo, listWorkspace, readWorkspace, writePdf } from './files.ts';
import { imageInput, compactImage } from './images.ts';
import { drawingPdf, drawingRegion, loadDrawingPdf, pdfPreview } from './pdf.ts';
import { serveDownload } from './download.ts';

function text(value: object): CallToolResult {
  return { content: [{ type: 'text', text: JSON.stringify(value) }] };
}

export function createHandler(settings: Settings, providers: ImageProvider[]): RequestListener {
  let activeJobs = 0;
  return async (req, res) => {
    if (req.url === '/health') {
      res.end('OK');
      return;
    }
    if (req.url?.startsWith('/download/')) {
      if (activeJobs >= settings.maxConcurrentJobs) {
        res.writeHead(503, { 'Cache-Control': 'private, no-store', 'Retry-After': '2' });
        res.end('The workspace is busy. Retry the download.');
        return;
      }
      activeJobs += 1;
      const finished = new Promise<void>((resolve) => {
        function complete() {
          res.off('finish', complete);
          res.off('close', complete);
          resolve();
        }
        res.once('finish', complete);
        res.once('close', complete);
      });
      const timeout = setTimeout(() => res.destroy(), settings.requestTimeoutMs);
      timeout.unref();
      try {
        await serveDownload(settings, req, res);
        // 下载缓冲区只在响应发送完成或断开后才释放并发名额。
        await finished;
      } finally {
        clearTimeout(timeout);
        activeJobs -= 1;
      }
      return;
    }
    if (req.url !== '/mcp') {
      res.writeHead(404);
      res.end();
      return;
    }
    // 用户标识来自 LibreChat 的受控配置；每个服务仅挂载一个用户的目录。
    if (!settings.userId || req.headers['x-librechat-user-id'] !== settings.userId) {
      res.writeHead(403);
      res.end('Forbidden');
      return;
    }
    const controller = new AbortController();
    const signal = AbortSignal.any([
      controller.signal,
      AbortSignal.timeout(settings.requestTimeoutMs),
    ]);
    const server = new McpServer({ name: 'design-workspace', version: '1.0.0' });
    async function run(operation: () => Promise<CallToolResult>): Promise<CallToolResult> {
      if (activeJobs >= settings.maxConcurrentJobs) {
        return { ...text({ ok: false, error: { code: 'DESIGN_BUSY' } }), isError: true };
      }
      activeJobs += 1;
      try {
        return await operation();
      } catch (error) {
        const code =
          error instanceof ToolFailure
            ? error.code
            : signal.aborted
              ? 'REQUEST_CANCELLED'
              : 'DESIGN_TOOL_UNAVAILABLE';
        return { ...text({ ok: false, error: { code } }), isError: true };
      } finally {
        activeJobs -= 1;
      }
    }
    server.registerTool(
      'workspace_files',
      {
        description:
          "List the current private CAD workspace, including CSV cut lists, and download links. Finish and await every save, export, and verification that writes files before calling this tool; never run it in parallel with writers or modify linked files afterward. Include Markdown links using the exact returned download_url in your final chat answer. Signed links expire at expires_at, allow only the exact file version, and need no second login; never expose tickets separately or reuse another file's link. Files above direct_download_limit_bytes remain listed with download_requires_login and an authenticated download URL; explain that these use the user's CAD file-manager credentials. Entries with download_status are changing or crossed a size bound during inspection: wait for the writer to finish and call this tool again before presenting them as completed exports. Request fresh links with this tool when expired or when a file has changed. A cancelled export may have already published a complete output: list files before retrying; publication is retained to preserve concurrent user edits. Use upload_url for manual uploads and archive_url for manual ZIP transfers into LibreChat Files.",
        inputSchema: {},
      },
      () => run(async () => text(await listWorkspace(settings))),
    );
    server.registerTool(
      'export_drawing_pdf',
      {
        description:
          'Export one PNG/JPEG/WebP/SVG in /tmp/cad to PDF with a download link and an independently rendered preview of the finished PDF. SVG geometry and text are written as PDF vectors; raster inputs remain raster previews. For a scaled CAD sheet, use a self-contained UTF-8 SVG with explicit physical dimensions (for example width="420mm" height="297mm" viewBox="0 0 420 297") and BOTH matching page_width_mm and page_height_mm. Keep borders and margins inside the SVG. Missing external resources, DTDs, escaped CSS, mismatched dimensions, and incomplete dimensions are rejected. SVG_ENCODING_UNSUPPORTED means re-export the SVG as UTF-8. Omit both dimensions for an A4 fit-to-page preview, which must not claim a drawing scale. Use assemble_drawing_pdf for an ordered multi-sheet set or existing native CAD PDFs. Inspect the finished pages with inspect_pdf, including small details. Existing files are preserved: FILE_EXISTS means choose a new revision filename. Run design operations sequentially; DESIGN_BUSY means await the active operation. Finish all saves before requesting workspace_files links.',
        inputSchema: {
          source_filename: z.string().min(1),
          pdf_filename: z.string().endsWith('.pdf'),
          page_width_mm: z.number().min(50).max(1200).optional(),
          page_height_mm: z.number().min(50).max(1200).optional(),
        },
      },
      ({ source_filename, pdf_filename, page_width_mm, page_height_mm }) =>
        run(async () => {
          if (!/^image\//.test(fileType(source_filename))) throw new ToolFailure('IMAGE_REQUIRED');
          fileType(pdf_filename);
          if ((page_width_mm == null) !== (page_height_mm == null))
            throw new ToolFailure('PAGE_SIZE_REQUIRED');
          const paper =
            page_width_mm != null && page_height_mm != null
              ? { widthMm: page_width_mm, heightMm: page_height_mm }
              : undefined;
          const bytes = await drawingPdf(
            await readWorkspace(settings, source_filename),
            settings,
            signal,
            paper,
            fileType(source_filename) === 'image/svg+xml',
          );
          if (bytes.length > settings.maxFileBytes) throw new ToolFailure('FILE_TOO_LARGE');
          const { preview } = await pdfPreview(bytes, settings, signal);
          const image = await compactImage(
            { bytes: preview, mimeType: 'image/png' },
            settings.maxImageBytes,
            settings.maxImagePixels,
          );
          await writePdf(settings, pdf_filename, bytes, signal);
          return {
            content: [
              ...text({
                ...fileInfo(settings, pdf_filename, bytes),
                layout: paper ? 'sheet' : 'preview',
                page_size_mm: paper ? [paper.widthMm, paper.heightMm] : undefined,
                preview_source: 'finished_pdf',
              }).content,
              { type: 'image', data: image.bytes.toString('base64'), mimeType: image.mimeType },
            ],
          };
        }),
    );
    server.registerTool(
      'assemble_drawing_pdf',
      {
        description: `Create one ordered drawing-set PDF from self-contained physical UTF-8 SVG sheets and/or existing native CAD PDFs in this user's /tmp/cad workspace. Each SVG requires BOTH matching paper dimensions; existing PDFs retain all pages, vector content, paper sizes, and rotations and must omit the dimensions. PDF_LAYERS_UNSUPPORTED means export a flat vector PDF from CAD with the intended layers visible, then retry; layered PDFs are rejected to prevent hidden layers changing visibility. PDF_FORMS_UNSUPPORTED means flatten form fields in the source producer before exporting. PDF_PAGE_UNSUPPORTED means export a full-sheet page up to 1200 mm per side with matching MediaBox/CropBox/BleedBox/TrimBox/ArtBox and standard units. These native PDFs must be static drawing pages, with no interactive form or differing print/preview area. PDF_ACTIONS_UNSUPPORTED means flatten annotations and remove page actions in the source producer; annotation-bearing pages are rejected. PDF_ATTACHMENTS_UNSUPPORTED means export drawing pages without associated source-file attachments or private editing data. PDF_EXTERNAL_STREAM_UNSUPPORTED means inline all stream resources in the source producer. PDF_COLOR_PROFILE_UNSUPPORTED means export ordinary drawing colors without PDF/X output intents; color-managed PDF/X inputs cannot be combined with this tool. Put sheet IDs, revisions, sheet numbers, scale, and reference callouts in the source drawings; this tool combines pages without inventing details, adding page numbers, or rescaling. Input bytes in total and output bytes must each stay under ${settings.maxFileBytes}; the set is limited to ${settings.maxPdfPages} pages. The response lists every page and previews the first finished page. Call inspect_pdf for EVERY page and important detail region before delivery. Keep assumptions and unperformed checks in the chat, not on drawing sheets. FILE_EXISTS requires a new revision filename. Run design operations sequentially and request workspace_files links after all exports are final.`,
        inputSchema: {
          pdf_filename: z.string().endsWith('.pdf'),
          sheets: z
            .array(
              z.object({
                source_filename: z.string().min(1),
                page_width_mm: z.number().min(50).max(1200).optional(),
                page_height_mm: z.number().min(50).max(1200).optional(),
              }),
            )
            .min(1)
            .max(settings.maxPdfPages),
        },
      },
      ({ pdf_filename, sheets }) =>
        run(async () => {
          fileType(pdf_filename);
          const combined = await PDFDocument.create();
          const pages: { page: number; source_filename: string; source_page: number }[] = [];
          let inputBytes = 0;
          let generatedBytes = 0;
          let normalizedBytes = 0;
          for (const sheet of sheets) {
            signal.throwIfAborted();
            const type = fileType(sheet.source_filename);
            if (type !== 'image/svg+xml' && type !== 'application/pdf')
              throw new ToolFailure('DRAWING_SHEET_REQUIRED');
            const source = await readWorkspace(settings, sheet.source_filename);
            inputBytes += source.length;
            if (inputBytes > settings.maxFileBytes) throw new ToolFailure('FILE_TOO_LARGE');
            let bytes: Buffer;
            if (type === 'image/svg+xml') {
              if (sheet.page_width_mm == null || sheet.page_height_mm == null)
                throw new ToolFailure('PAGE_SIZE_REQUIRED');
              bytes = await drawingPdf(
                source,
                settings,
                signal,
                {
                  widthMm: sheet.page_width_mm,
                  heightMm: sheet.page_height_mm,
                },
                true,
              );
            } else {
              if (sheet.page_width_mm != null || sheet.page_height_mm != null)
                throw new ToolFailure('PDF_PAGE_SIZE_PRESERVED');
              bytes = source;
            }
            generatedBytes += bytes.length;
            if (generatedBytes > settings.maxFileBytes) throw new ToolFailure('FILE_TOO_LARGE');
            const loaded = await loadDrawingPdf(
              bytes,
              settings,
              signal,
              settings.maxFileBytes - normalizedBytes,
            );
            normalizedBytes += loaded.normalizedBytes;
            const pdf = loaded.document;
            if (combined.getPageCount() + pdf.getPageCount() > settings.maxPdfPages)
              throw new ToolFailure('PDF_PAGE_LIMIT');
            const copied = await combined.copyPages(pdf, pdf.getPageIndices());
            for (const [index, page] of copied.entries()) {
              combined.addPage(page);
              pages.push({
                page: pages.length + 1,
                source_filename: sheet.source_filename,
                source_page: index + 1,
              });
            }
          }
          const bytes = Buffer.from(await combined.save());
          if (bytes.length > settings.maxFileBytes) throw new ToolFailure('FILE_TOO_LARGE');
          const { preview } = await pdfPreview(bytes, settings, signal);
          const image = await compactImage(
            { bytes: preview, mimeType: 'image/png' },
            settings.maxImageBytes,
            settings.maxImagePixels,
          );
          await writePdf(settings, pdf_filename, bytes, signal);
          return {
            content: [
              ...text({
                ...fileInfo(settings, pdf_filename, bytes),
                page_count: pages.length,
                pages,
                preview_page: 1,
                preview_source: 'finished_pdf',
              }).content,
              { type: 'image', data: image.bytes.toString('base64'), mimeType: image.mimeType },
            ],
          };
        }),
    );
    server.registerTool(
      'inspect_pdf',
      {
        description:
          'Render a selected page of the finished PDF in the private workspace, independently of its SVG source. Use after export or assembly to check page order, missing geometry, text, lineweights, sections, and construction details. Page numbers start at 1. Omit region for an overview; use a small region in paper millimeters from the upper-left of the visible, rotated page to read fine details. Inspect every page and fix defects before delivering the set. This verifies the rendered drawing, not physical manufacturability. Run design operations sequentially.',
        inputSchema: {
          pdf_filename: z.string().endsWith('.pdf'),
          page: z.number().int().min(1).max(settings.maxPdfPages),
          region: z
            .object({
              left_mm: z.number().min(0),
              top_mm: z.number().min(0),
              width_mm: z.number().min(1).max(1200),
              height_mm: z.number().min(1).max(1200),
            })
            .optional(),
        },
      },
      ({ pdf_filename, page, region }) =>
        run(async () => {
          const { preview, paper, pageCount } = await pdfPreview(
            await readWorkspace(settings, pdf_filename),
            settings,
            signal,
            page,
            region
              ? {
                  leftMm: region.left_mm,
                  topMm: region.top_mm,
                  widthMm: region.width_mm,
                  heightMm: region.height_mm,
                }
              : undefined,
          );
          const image = await compactImage(
            { bytes: preview, mimeType: 'image/png' },
            settings.maxImageBytes,
            settings.maxImagePixels,
          );
          return {
            content: [
              ...text({
                filename: pdf_filename,
                page,
                page_count: pageCount,
                page_size_mm: [paper.widthMm, paper.heightMm],
                region_mm: region,
                preview_source: 'finished_pdf',
              }).content,
              { type: 'image', data: image.bytes.toString('base64'), mimeType: image.mimeType },
            ],
          };
        }),
    );
    server.registerTool(
      'inspect_drawing',
      {
        description:
          'Inspect a selected region of a physical SVG drawing sheet in /tmp/cad at the original render resolution. Run design exports and inspections sequentially; DESIGN_BUSY means await the active operation before retrying. Use this after the overview from export_drawing_pdf to read small annotations and check detail views on large sheets; the overview alone cannot certify every detail. Supply the same paper dimensions as the PDF export. The region is measured in paper millimeters from the upper-left corner. Choose a small region around the text or detail being checked; the returned image is a source-sheet detail, not an independent PDF rendering.',
        inputSchema: {
          source_filename: z.string().endsWith('.svg'),
          page_width_mm: z.number().min(50).max(1200),
          page_height_mm: z.number().min(50).max(1200),
          left_mm: z.number().min(0),
          top_mm: z.number().min(0),
          width_mm: z.number().min(1).max(1200),
          height_mm: z.number().min(1).max(1200),
        },
      },
      (input) =>
        run(async () => {
          const preview = await drawingRegion(
            await readWorkspace(settings, input.source_filename),
            settings,
            signal,
            { widthMm: input.page_width_mm, heightMm: input.page_height_mm },
            {
              leftMm: input.left_mm,
              topMm: input.top_mm,
              widthMm: input.width_mm,
              heightMm: input.height_mm,
            },
          );
          const image = await compactImage(
            { bytes: preview, mimeType: 'image/png' },
            settings.maxImageBytes,
            settings.maxImagePixels,
          );
          return {
            content: [
              ...text({
                source_filename: input.source_filename,
                page_size_mm: [input.page_width_mm, input.page_height_mm],
                region_mm: {
                  left: input.left_mm,
                  top: input.top_mm,
                  width: input.width_mm,
                  height: input.height_mm,
                },
              }).content,
              { type: 'image', data: image.bytes.toString('base64'), mimeType: image.mimeType },
            ],
          };
        }),
    );
    for (const provider of providers) {
      server.registerTool(
        provider.name,
        { description: provider.description, inputSchema: provider.inputSchema },
        (raw) =>
          run(async () => {
            const input = imageInput.parse(raw);
            const references: ImageData[] = [];
            for (const filename of input.reference_filenames) {
              const mimeType = fileType(filename);
              if (!['image/png', 'image/jpeg', 'image/webp'].includes(mimeType))
                throw new ToolFailure('IMAGE_REQUIRED');
              references.push({ bytes: await readWorkspace(settings, filename), mimeType });
            }
            const result = await compactImage(
              await provider.generate(input, references, signal),
              settings.maxImageBytes,
              settings.maxImagePixels,
            );
            // 标准 MCP 图片输出由 LibreChat 自身保存并展示，避免重复上传图片。
            return {
              content: [
                { type: 'image', data: result.bytes.toString('base64'), mimeType: result.mimeType },
              ],
            };
          }),
      );
    }
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    });
    res.on('close', () => {
      controller.abort();
      void server.close().catch(() => console.error('MCP cleanup failed.'));
    });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res);
    } catch {
      console.error('MCP request failed.');
      if (!res.headersSent) {
        res.writeHead(500);
        res.end('Design tool unavailable.');
      }
    }
  };
}
