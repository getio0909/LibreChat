import { z } from 'zod';
import path from 'node:path';
import * as fs from 'node:fs/promises';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import type { RequestListener } from 'node:http';
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js';
import type { ImageProvider, ImageData } from './images.ts';
import type { Settings } from './config.ts';
import { ToolFailure } from './config.ts';
import { fileType, fileInfo, listWorkspace, readWorkspace } from './files.ts';
import { imageInput, compactImage } from './images.ts';
import { drawingPdf, drawingRegion } from './pdf.ts';
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
          "List the current private CAD workspace, including CSV cut lists, and download links. After every CAD/3D export, call this tool and include Markdown links using the exact returned download_url in your final chat answer. Signed links expire at expires_at, allow only the exact file version, and need no second login; never expose tickets separately or reuse another file's link. Files above direct_download_limit_bytes remain listed with download_requires_login and an authenticated download URL; explain that these use the user's CAD file-manager credentials. Entries with download_status are changing or crossed a size bound during inspection: wait for the writer to finish and call this tool again before presenting them as completed exports. Request fresh links with this tool when expired. Use upload_url for manual uploads and archive_url for manual ZIP transfers into LibreChat Files.",
        inputSchema: {},
      },
      () => run(async () => text(await listWorkspace(settings))),
    );
    server.registerTool(
      'export_drawing_pdf',
      {
        description:
          'Convert an exported PNG/JPEG/WebP/SVG in /tmp/cad to a real PDF with a direct download link and a rendered source-sheet preview. Inspect the returned image for overlap, cropping, missing details, and unreadable text. For a scaled CAD sheet, prepare SVG with explicit physical canvas dimensions (for example width="420mm" height="297mm" and viewBox="0 0 420 297"), lay out geometry at the stated drawing scale, and pass BOTH matching page_width_mm and page_height_mm. This preserves the full sheet without adding margins or fitting; mismatched dimensions are rejected. Keep borders and printable margins inside the SVG. Omit both dimensions for an A4 fit-to-page preview, which must not claim a drawing scale. Export each sheet separately when needed, and keep commentary or inspection reports separate from drawings. Return exact download_url links and retain the editable CAD model.',
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
          const { bytes, preview } = await drawingPdf(
            await readWorkspace(settings, source_filename),
            settings.maxImagePixels,
            paper,
          );
          if (bytes.length > settings.maxFileBytes) throw new ToolFailure('FILE_TOO_LARGE');
          const image = await compactImage(
            { bytes: preview, mimeType: 'image/png' },
            settings.maxImageBytes,
            settings.maxImagePixels,
          );
          await fs.writeFile(path.join(settings.workspace, pdf_filename), bytes, {
            flag: 'wx',
            mode: 0o644,
          });
          return {
            content: [
              ...text({
                ...fileInfo(settings, pdf_filename, bytes),
                layout: paper ? 'sheet' : 'preview',
                page_size_mm: paper ? [paper.widthMm, paper.heightMm] : undefined,
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
          'Inspect a selected region of a physical SVG drawing sheet in /tmp/cad at the original render resolution. Use this after the overview from export_drawing_pdf to read small annotations and check detail views on large sheets; the overview alone cannot certify every detail. Supply the same paper dimensions as the PDF export. The region is measured in paper millimeters from the upper-left corner. Choose a small region around the text or detail being checked; the returned image is a source-sheet detail, not an independent PDF rendering.',
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
            settings.maxImagePixels,
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
