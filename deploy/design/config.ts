import { z } from 'zod';

export const settingsSchema = z.object({
  userId: z
    .string()
    .regex(/^[a-f0-9]{24}$/)
    .or(z.literal(''))
    .default(''),
  workspaceUrl: z.string().url(),
  downloadUrl: z.string().url().optional(),
  downloadSigningKey: z
    .string()
    .regex(/^[a-f0-9]{64}$/)
    .or(z.literal(''))
    .default(''),
  downloadLinkTtlSeconds: z
    .number()
    .int()
    .min(60)
    .max(7 * 86400)
    .default(86400),
  workspace: z.string().default('/tmp/cad'),
  maxFileBytes: z
    .number()
    .int()
    .positive()
    .max(100 * 1024 * 1024)
    .default(25 * 1024 * 1024),
  maxImageBytes: z
    .number()
    .int()
    .positive()
    .max(10 * 1024 * 1024)
    .default(10 * 1024 * 1024),
  maxPromptLength: z.number().int().positive().default(32000),
  maxImagePixels: z
    .number()
    .int()
    .positive()
    .max(64 * 1024 * 1024)
    .default(32 * 1024 * 1024),
  maxConcurrentJobs: z.number().int().min(1).max(4).default(1),
  requestTimeoutMs: z.number().int().positive().default(180000),
  arkApiKey: z.string().default(''),
  geminiApiKey: z.string().default(''),
  seedreamModel: z
    .string()
    .regex(/^[a-zA-Z0-9._-]+$/)
    .default('doubao-seedream-5-0-flash-260915'),
  geminiModel: z
    .string()
    .regex(/^[a-zA-Z0-9._-]+$/)
    .default('gemini-3.1-flash-image'),
});

export type Settings = z.infer<typeof settingsSchema>;
export type Request = typeof fetch;

export class ToolFailure extends Error {
  readonly code: string;
  constructor(code: string) {
    super(code);
    this.code = code;
  }
}

export async function boundedBody(response: Response, limit: number): Promise<Buffer> {
  if (Number(response.headers.get('content-length')) > limit) {
    await response.body?.cancel();
    throw new ToolFailure('FILE_TOO_LARGE');
  }
  const reader = response.body?.getReader();
  if (!reader) throw new ToolFailure('EMPTY_RESPONSE');
  let bytes = 0;
  const chunks: Uint8Array[] = [];
  try {
    for (;;) {
      const result = await reader.read();
      if (result.done) return Buffer.concat(chunks, bytes);
      bytes += result.value.byteLength;
      if (bytes > limit) {
        await reader.cancel();
        throw new ToolFailure('FILE_TOO_LARGE');
      }
      chunks.push(result.value);
    }
  } finally {
    reader.releaseLock();
  }
}
