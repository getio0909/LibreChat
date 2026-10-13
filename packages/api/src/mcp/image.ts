import { z } from 'zod';
import sharp from 'sharp';
import yaml from 'js-yaml';
import { configSchema } from 'librechat-data-provider';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js';
import type { RequestListener } from 'node:http';

type Provider = 'seedream' | 'gemini';

interface ImageDeps {
  request: (...args: Parameters<typeof fetch>) => ReturnType<typeof fetch>;
  reportError: (message: string) => void;
  configSource?: string;
}

const inputSchema = {
  aspect_ratio: z.enum(['1:1', '3:2', '2:3', '16:9', '9:16']).default('1:1'),
};

const dimensions: Record<z.infer<typeof inputSchema.aspect_ratio>, [number, number]> = {
  '1:1': [2048, 2048],
  '3:2': [2496, 1664],
  '2:3': [1664, 2496],
  '16:9': [2560, 1440],
  '9:16': [1440, 2560],
};

const seedreamResponse = z.object({
  data: z.array(z.object({ b64_json: z.string().min(1) })).min(1),
});
const geminiResponse = z.object({
  candidates: z.array(
    z.object({
      content: z.object({
        parts: z.array(
          z.object({
            inlineData: z
              .object({
                data: z.string().min(1),
                mimeType: z.enum(['image/png', 'image/jpeg', 'image/webp']),
              })
              .optional(),
          }),
        ),
      }),
    }),
  ),
});

function failure(code: string): CallToolResult {
  const result = { ok: false, error: { code } };
  return {
    isError: true,
    structuredContent: result,
    content: [{ type: 'text', text: JSON.stringify(result) }],
  };
}

/** 将图片接口转换为 MCP 工具，凭据仅存在于当前 HTTP 请求中。 */
export function createImageMCPHandler({
  request,
  reportError,
  configSource = '{}',
}: ImageDeps): RequestListener {
  const { imageTools } = configSchema.pick({ imageTools: true }).parse(yaml.load(configSource));
  async function imageResult(data: string, mimeType: string): Promise<CallToolResult> {
    const bytes = Buffer.from(data, 'base64');
    if (bytes.byteLength <= imageTools.maxImageBytes) {
      return { content: [{ type: 'image', mimeType, data }] };
    }
    const compact = await sharp(bytes).jpeg().toBuffer();
    if (compact.byteLength > imageTools.maxImageBytes) return failure('IMAGE_TOO_LARGE');
    return {
      content: [{ type: 'image', mimeType: 'image/jpeg', data: compact.toString('base64') }],
    };
  }
  return async (req, res) => {
    if (req.url === '/health') {
      res.writeHead(200, { 'Content-Type': 'text/plain' });
      res.end('OK');
      return;
    }
    let provider: Provider;
    if (req.url === '/seedream') {
      provider = 'seedream';
    } else if (req.url === '/gemini') {
      provider = 'gemini';
    } else {
      res.writeHead(404);
      res.end();
      return;
    }
    const model = req.headers['x-image-model'];
    if (typeof model !== 'string' || !/^[a-zA-Z0-9._-]+$/.test(model)) {
      res.writeHead(400);
      res.end('Configure a supported image model in the MCP server headers.');
      return;
    }
    const key = req.headers['x-provider-api-key'];
    const controller = new AbortController();
    const server = new McpServer({ name: `${provider}-images`, version: '1.0.0' });
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined,
      enableJsonResponse: true,
    });
    server.registerTool(
      'generate_image',
      {
        description: `Generate an original image from a text prompt using ${model}.`,
        inputSchema: {
          ...inputSchema,
          prompt: z.string().min(1).max(imageTools.maxPromptLength),
          size:
            provider === 'seedream'
              ? z.literal('2K').default('2K')
              : z.enum(['1K', '2K', '4K']).default('1K'),
        },
      },
      async ({ prompt, aspect_ratio, size }): Promise<CallToolResult> => {
        if (typeof key !== 'string' || key.length === 0) {
          return failure('IMAGE_API_KEY_REQUIRED');
        }
        try {
          const [width, height] = dimensions[aspect_ratio];
          const response = await request(
            provider === 'seedream'
              ? 'https://ark.cn-beijing.volces.com/api/v3/images/generations'
              : `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
            {
              method: 'POST',
              signal: controller.signal,
              headers: {
                'Content-Type': 'application/json',
                ...(provider === 'seedream'
                  ? { Authorization: `Bearer ${key}` }
                  : { 'x-goog-api-key': key }),
              },
              body: JSON.stringify(
                provider === 'seedream'
                  ? {
                      model,
                      prompt,
                      size: `${width}x${height}`,
                      output_format: 'png',
                      response_format: 'b64_json',
                      watermark: true,
                    }
                  : {
                      contents: [{ parts: [{ text: prompt }] }],
                      generationConfig: {
                        responseModalities: ['TEXT', 'IMAGE'],
                        imageConfig: { aspectRatio: aspect_ratio, imageSize: size },
                      },
                    },
              ),
            },
          );
          if (!response.ok) {
            if (response.status === 401 || response.status === 403)
              return failure('IMAGE_PROVIDER_AUTH');
            if (response.status === 429) return failure('IMAGE_PROVIDER_QUOTA');
            return failure('IMAGE_PROVIDER_REJECTED');
          }
          const body: unknown = await response.json();
          if (provider === 'seedream') {
            const parsed = seedreamResponse.safeParse(body);
            if (parsed.success) {
              return await imageResult(parsed.data.data[0].b64_json, 'image/png');
            }
          } else {
            const parsed = geminiResponse.safeParse(body);
            const image = parsed.success
              ? parsed.data.candidates[0]?.content.parts.find((part) => part.inlineData)?.inlineData
              : undefined;
            if (image) {
              return await imageResult(image.data, image.mimeType);
            }
          }
          return failure('IMAGE_PROVIDER_NO_IMAGE');
        } catch {
          reportError('Image provider request failed.');
          return failure('IMAGE_PROVIDER_UNAVAILABLE');
        }
      },
    );
    res.on('close', () => {
      controller.abort();
      void server.close().catch(() => reportError('Image MCP session cleanup failed.'));
    });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res);
    } catch {
      reportError('Image MCP request failed.');
      if (!res.headersSent) {
        res.writeHead(500);
        res.end('The image tool is unavailable. Please retry.');
      }
    }
  };
}
