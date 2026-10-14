import { z } from 'zod';
import sharp from 'sharp';
import type { Settings, Request } from './config.ts';
import { ToolFailure, boundedBody } from './config.ts';

export const imageInput = z.object({
  prompt: z.string().min(1),
  aspect_ratio: z.enum(['1:1', '3:2', '2:3', '16:9', '9:16']).default('1:1'),
  size: z.enum(['1K', '2K', '4K']).default('2K'),
  reference_filenames: z.array(z.string()).max(4).default([]),
  save_filename: z
    .string()
    .regex(/\.(png|jpg|jpeg|webp)$/i)
    .optional(),
});
type ImageInput = z.infer<typeof imageInput>;
export interface ImageData {
  bytes: Buffer;
  mimeType: string;
}
export interface ImageProvider {
  name: string;
  description: string;
  inputSchema: z.ZodRawShape;
  generate: (input: ImageInput, references: ImageData[], signal: AbortSignal) => Promise<ImageData>;
}

async function result(response: Response, settings: Settings): Promise<unknown> {
  if (!response.ok) {
    await response.body?.cancel();
    if (response.status === 401 || response.status === 403)
      throw new ToolFailure('IMAGE_PROVIDER_AUTH');
    if (response.status === 429) throw new ToolFailure('IMAGE_PROVIDER_QUOTA');
    throw new ToolFailure('IMAGE_PROVIDER_REJECTED');
  }
  return JSON.parse((await boundedBody(response, settings.maxImageBytes * 4)).toString('utf8'));
}

export async function compactImage(
  image: ImageData,
  limit: number,
  maxImagePixels: number,
): Promise<ImageData> {
  if (!image.bytes.length) throw new ToolFailure('IMAGE_PROVIDER_NO_IMAGE');
  const decoded = sharp(image.bytes, { limitInputPixels: maxImagePixels });
  await decoded.metadata();
  if (image.bytes.length <= limit) return image;
  const bytes = await decoded.jpeg().toBuffer();
  if (bytes.length > limit) throw new ToolFailure('IMAGE_TOO_LARGE');
  return { bytes, mimeType: 'image/jpeg' };
}

export function imageProviders(settings: Settings, request: Request = fetch): ImageProvider[] {
  const common = { ...imageInput.shape, prompt: z.string().min(1).max(settings.maxPromptLength) };
  const sizes = {
    '1:1': '2048x2048',
    '3:2': '2496x1664',
    '2:3': '1664x2496',
    '16:9': '2560x1440',
    '9:16': '1440x2560',
  };
  return [
    {
      name: 'seedream_image',
      description:
        'Generate or edit an image with Seedream Flash. Prefer this provider unless the user requests Gemini. Reference filenames must exist in the current CAD workspace.',
      inputSchema: { ...common, size: z.literal('2K').default('2K') },
      generate: async (input, references, signal) => {
        if (!settings.arkApiKey) throw new ToolFailure('IMAGE_API_KEY_REQUIRED');
        const response = await request(
          'https://ark.cn-beijing.volces.com/api/v3/images/generations',
          {
            method: 'POST',
            signal,
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${settings.arkApiKey}`,
            },
            body: JSON.stringify({
              model: settings.seedreamModel,
              prompt: input.prompt,
              size: sizes[input.aspect_ratio],
              image: references.length
                ? references.map(
                    (image) => `data:${image.mimeType};base64,${image.bytes.toString('base64')}`,
                  )
                : undefined,
              response_format: 'b64_json',
              output_format: 'png',
              watermark: true,
              sequential_image_generation: 'disabled',
            }),
          },
        );
        const body = z
          .object({ data: z.array(z.object({ b64_json: z.string().min(1) })).min(1) })
          .safeParse(await result(response, settings));
        if (!body.success) throw new ToolFailure('IMAGE_PROVIDER_NO_IMAGE');
        return { bytes: Buffer.from(body.data.data[0].b64_json, 'base64'), mimeType: 'image/png' };
      },
    },
    {
      name: 'gemini_image',
      description:
        'Generate or edit an image with Gemini when requested. Reference filenames must exist in the current CAD workspace.',
      inputSchema: { ...common, size: z.enum(['1K', '2K', '4K']).default('1K') },
      generate: async (input, references, signal) => {
        if (!settings.geminiApiKey) throw new ToolFailure('IMAGE_API_KEY_REQUIRED');
        const response = await request(
          `https://generativelanguage.googleapis.com/v1beta/models/${settings.geminiModel}:generateContent`,
          {
            method: 'POST',
            signal,
            headers: {
              'Content-Type': 'application/json',
              'x-goog-api-key': settings.geminiApiKey,
            },
            body: JSON.stringify({
              contents: [
                {
                  parts: [
                    { text: input.prompt },
                    ...references.map((image) => ({
                      inlineData: {
                        mimeType: image.mimeType,
                        data: image.bytes.toString('base64'),
                      },
                    })),
                  ],
                },
              ],
              generationConfig: {
                responseModalities: ['TEXT', 'IMAGE'],
                imageConfig: { aspectRatio: input.aspect_ratio, imageSize: input.size },
              },
            }),
          },
        );
        const body = z
          .object({
            candidates: z.array(
              z.object({
                content: z.object({
                  parts: z.array(
                    z.object({
                      inlineData: z
                        .object({
                          data: z.string(),
                          mimeType: z.enum(['image/png', 'image/jpeg', 'image/webp']),
                        })
                        .optional(),
                    }),
                  ),
                }),
              }),
            ),
          })
          .safeParse(await result(response, settings));
        const image = body.success
          ? body.data.candidates[0]?.content.parts.find((part) => part.inlineData)?.inlineData
          : undefined;
        if (!image) throw new ToolFailure('IMAGE_PROVIDER_NO_IMAGE');
        return { bytes: Buffer.from(image.data, 'base64'), mimeType: image.mimeType };
      },
    },
  ];
}
