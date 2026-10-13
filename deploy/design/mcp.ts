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
          'List the current private CAD workspace and authenticated download links. Use upload_url to upload files manually. Pass returned local paths to FreeCAD/Blender import tools. Users can download exports and upload them into LibreChat Files for reuse.',
        inputSchema: {},
      },
      () => run(async () => text(await listWorkspace(settings))),
    );
    server.registerTool(
      'export_drawing_pdf',
      {
        description:
          'Convert an exported PNG/JPEG/WebP/SVG drawing or render to a PDF and return its authenticated download link. This exports a drawing/view, while the editable model remains a separate CAD file.',
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
          return text(fileInfo(settings, pdf_filename, bytes.length));
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
