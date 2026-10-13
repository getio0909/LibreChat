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
import { drawingPdf } from './pdf.ts';
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
      try {
        await serveDownload(settings, req, res);
      } finally {
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
          "List the current private CAD workspace, including CSV cut lists, and direct download links. After every CAD/3D export, call this tool and include Markdown links using the exact returned download_url in your final chat answer. Signed links expire at expires_at, allow only the exact file version, and need no second login; never expose tickets separately or reuse another file's link. Request fresh links with this tool when expired. Use upload_url for manual uploads and archive_url for manual ZIP transfers into LibreChat Files.",
        inputSchema: {},
      },
      () => run(async () => text(await listWorkspace(settings))),
    );
    server.registerTool(
      'export_drawing_pdf',
      {
        description:
          'Convert an exported PNG/JPEG/WebP/SVG drawing or render in /tmp/cad to a real PDF and return its direct download link. When a user requests CAD drawings, create a dimensioned SVG or rendered view, call this tool, and include the exact returned download_url as a Markdown link. A rendered view is a preview, not a dimensioned manufacturing drawing; identify which you exported. Keep the editable CAD model separate.',
        inputSchema: {
          source_filename: z.string().min(1),
          pdf_filename: z.string().endsWith('.pdf'),
        },
      },
      ({ source_filename, pdf_filename }) =>
        run(async () => {
          if (!/^image\//.test(fileType(source_filename))) throw new ToolFailure('IMAGE_REQUIRED');
          fileType(pdf_filename);
          const bytes = await drawingPdf(
            await readWorkspace(settings, source_filename),
            settings.maxImagePixels,
          );
          if (bytes.length > settings.maxFileBytes) throw new ToolFailure('FILE_TOO_LARGE');
          await fs.writeFile(path.join(settings.workspace, pdf_filename), bytes, {
            flag: 'wx',
            mode: 0o644,
          });
          return text(fileInfo(settings, pdf_filename, bytes));
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
