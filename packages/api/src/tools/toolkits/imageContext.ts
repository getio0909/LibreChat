import fs from 'node:fs';
import sharp from 'sharp';
import path from 'node:path';
import { nanoid } from 'nanoid';
import crypto from 'node:crypto';
import { GoogleGenAI } from '@google/genai';
import { logger as defaultLogger } from '@librechat/data-schemas';
import {
  Constants,
  FileContext,
  ContentTypes,
  EImageOutputType,
  resolveImageReferencesConfig,
} from 'librechat-data-provider';
import type { TImageReferencesConfig } from 'librechat-data-provider';
import type { AppConfig } from '@librechat/data-schemas';
import type { FormatEnum } from 'sharp';
import { getBalanceConfig, getTransactionsConfig } from '~/app/config';
import { getEnvProxyDispatcher } from '~/utils/proxy';
import { loadServiceKey } from '~/utils/key';
import { geminiToolkit } from './gemini';

// 限制单次注入到 MCP 上下文中的最大历史图像数量以控制提示词体积
export const MAX_MCP_CONTEXT_IMAGES = 20;

export interface MCPImageContextFile {
  file_id: string;
  filename?: string;
  kind?: 'upload' | 'generated';
  turn?: number | string;
  context?: string;
}

export interface MCPBranchMessage {
  messageId: string;
  parentMessageId?: string | Constants | null;
  isCreatedByUser?: boolean;
  user?: string | { toString(): string };
  files?: Array<{ file_id?: string; filename?: string }>;
  attachments?: Array<{ file_id?: string; filename?: string; type?: string; context?: string }>;
}

export interface MCPImageCandidateRecord {
  file_id?: string;
  filename?: string;
  type?: string;
  user?: string | { toString(): string };
  tenantId?: string | null;
  context?: string;
}

// 沿当前选中的对话分支从叶子消息向上回溯并以时间正序返回分支消息
function walkSelectedConversationBranch(
  messages: MCPBranchMessage[] | null | undefined,
  parentMessageId: string | null | undefined,
  userId?: string,
): MCPBranchMessage[] {
  if (
    !messages ||
    messages.length === 0 ||
    !parentMessageId ||
    parentMessageId === Constants.NO_PARENT
  ) {
    return [];
  }

  const messageMap = new Map<string, MCPBranchMessage>();
  for (const msg of messages) {
    if (!msg?.messageId) {
      continue;
    }
    if (userId != null && msg.user != null && String(msg.user) !== String(userId)) {
      continue;
    }
    messageMap.set(msg.messageId, msg);
  }

  const reversedBranch: MCPBranchMessage[] = [];
  const visitedIds = new Set<string>();
  let currentId: string | null | undefined = parentMessageId;

  while (currentId && currentId !== Constants.NO_PARENT) {
    if (visitedIds.has(currentId)) {
      break;
    }
    visitedIds.add(currentId);
    const msg = messageMap.get(currentId);
    if (!msg) {
      break;
    }
    reversedBranch.push(msg);
    currentId =
      msg.parentMessageId != null && msg.parentMessageId !== Constants.NO_PARENT
        ? String(msg.parentMessageId)
        : null;
  }

  return reversedBranch.reverse();
}

// 收集选中对话分支中历史上传与工具生成附件的候选文件 ID
export function collectBranchImageCandidateIds({
  messages,
  parentMessageId,
  userId,
}: {
  messages?: MCPBranchMessage[] | null;
  parentMessageId?: string | null;
  userId?: string;
}): string[] {
  const orderedBranch = walkSelectedConversationBranch(messages, parentMessageId, userId);
  const fileIds = new Set<string>();

  for (const msg of orderedBranch) {
    if (Array.isArray(msg.files)) {
      for (const file of msg.files) {
        if (typeof file?.file_id === 'string' && file.file_id.length > 0) {
          fileIds.add(file.file_id);
        }
      }
    }
    if (Array.isArray(msg.attachments)) {
      for (const attachment of msg.attachments) {
        if (typeof attachment?.file_id === 'string' && attachment.file_id.length > 0) {
          fileIds.add(attachment.file_id);
        }
      }
    }
  }

  return Array.from(fileIds);
}

// 从选中的对话分支与当前请求附件中提取属于当前用户的图像元数据列表并截断到最新上限
export function collectConversationBranchImageFiles({
  messages,
  parentMessageId,
  requestFiles = [],
  hydratedFilesById,
  userId,
  tenantId,
  maxImages = MAX_MCP_CONTEXT_IMAGES,
}: {
  messages?: MCPBranchMessage[] | null;
  parentMessageId?: string | null;
  requestFiles?: MCPImageCandidateRecord[];
  hydratedFilesById?: ReadonlyMap<string, MCPImageCandidateRecord>;
  userId?: string;
  tenantId?: string | null;
  maxImages?: number;
}): MCPImageContextFile[] {
  const orderedBranch = walkSelectedConversationBranch(messages, parentMessageId, userId);
  const branchRefs: Array<{
    file_id: string;
    filename?: string;
    kind: 'upload' | 'generated';
    turn: number;
  }> = [];

  let turn = 0;
  for (const msg of orderedBranch) {
    if (msg.isCreatedByUser === true) {
      turn += 1;
    } else if (msg.isCreatedByUser === undefined) {
      const hasFiles = Array.isArray(msg.files) && msg.files.length > 0;
      const hasAttachments = Array.isArray(msg.attachments) && msg.attachments.length > 0;
      if (hasFiles || (!hasAttachments && turn === 0)) {
        turn += 1;
      } else if (turn === 0) {
        turn = 1;
      }
    } else if (turn === 0) {
      turn = 1;
    }

    if (Array.isArray(msg.files)) {
      for (const file of msg.files) {
        if (typeof file?.file_id === 'string' && file.file_id.length > 0) {
          branchRefs.push({
            file_id: file.file_id,
            filename: file.filename,
            kind: 'upload',
            turn,
          });
        }
      }
    }

    if (Array.isArray(msg.attachments)) {
      for (const attachment of msg.attachments) {
        if (typeof attachment?.file_id === 'string' && attachment.file_id.length > 0) {
          branchRefs.push({
            file_id: attachment.file_id,
            filename: attachment.filename,
            kind: 'generated',
            turn,
          });
        }
      }
    }
  }

  const currentTurn = turn + 1;
  for (const reqFile of requestFiles ?? []) {
    if (typeof reqFile?.file_id === 'string' && reqFile.file_id.length > 0) {
      branchRefs.push({
        file_id: reqFile.file_id,
        filename: reqFile.filename,
        kind: reqFile.context === FileContext.image_generation ? 'generated' : 'upload',
        turn: currentTurn,
      });
    }
  }

  const requestFilesById = new Map<string, MCPImageCandidateRecord>();
  for (const reqFile of requestFiles ?? []) {
    if (typeof reqFile?.file_id === 'string' && reqFile.file_id.length > 0) {
      requestFilesById.set(reqFile.file_id, reqFile);
    }
  }

  const seenFileIds = new Set<string>();
  const resolved: MCPImageContextFile[] = [];

  for (const ref of branchRefs) {
    if (seenFileIds.has(ref.file_id)) {
      continue;
    }
    const hydrated = hydratedFilesById
      ? hydratedFilesById.get(ref.file_id)
      : requestFilesById.get(ref.file_id);
    if (!hydrated) {
      continue;
    }
    if (userId != null && hydrated.user != null && String(hydrated.user) !== String(userId)) {
      continue;
    }
    if (
      tenantId !== undefined &&
      hydrated.tenantId !== undefined &&
      (hydrated.tenantId ?? null) !== (tenantId ?? null)
    ) {
      continue;
    }
    if (typeof hydrated.type !== 'string' || !hydrated.type.startsWith('image')) {
      continue;
    }

    seenFileIds.add(ref.file_id);
    const kind: 'upload' | 'generated' =
      hydrated.context === FileContext.image_generation || ref.kind === 'generated'
        ? 'generated'
        : 'upload';
    resolved.push({
      file_id: ref.file_id,
      filename: hydrated.filename || ref.filename || 'unnamed',
      kind,
      turn: ref.turn,
      context: hydrated.context,
    });
  }

  return maxImages > 0 && resolved.length > maxImages ? resolved.slice(-maxImages) : resolved;
}

/**
 * Builds tool context string for image generation tools based on available image files.
 * @param params - The parameters for building image context
 * @param params.imageFiles - Array of image file objects with file_id property
 * @param params.toolName - The name of the tool (e.g., 'gemini_image_gen', 'image_edit_oai')
 * @param params.contextDescription - Description of what the images are for (e.g., 'image context', 'image editing')
 * @returns The tool context string or empty string if no images
 */
export function buildImageToolContext({
  imageFiles,
  toolName,
  contextDescription = 'image context',
}: {
  imageFiles: Array<{ file_id: string; filename?: string }>;
  toolName: string;
  contextDescription?: string;
}): string {
  if (!imageFiles || imageFiles.length === 0) {
    return '';
  }

  let toolContext = '';
  for (let i = 0; i < imageFiles.length; i++) {
    const file = imageFiles[i];
    if (!file) {
      continue;
    }
    if (i === 0) {
      toolContext = `Image files provided in this request (their image IDs listed in order of appearance) available for ${contextDescription}:`;
    }
    // 附带文件名以便模型准确识别并引用会话中的图片
    const filenameSuffix = file.filename ? ` (${file.filename})` : '';
    toolContext += `\n\t- ${file.file_id}${filenameSuffix}`;
    if (i === imageFiles.length - 1) {
      toolContext += `\n\nInclude any you need in the \`image_ids\` array when calling \`${toolName}\` to use them as visual context for generation. You may also include previously referenced or generated image IDs.`;
    }
  }
  return toolContext;
}

// 构建供 MCP 图像工具使用的中立图像列表与契约真实参数说明
export function buildMCPImageToolContext({
  imageFiles,
  maxImages = MAX_MCP_CONTEXT_IMAGES,
}: {
  imageFiles: MCPImageContextFile[];
  maxImages?: number;
}): string {
  if (!imageFiles || imageFiles.length === 0) {
    return '';
  }

  const validFiles = imageFiles.filter((file): file is MCPImageContextFile =>
    Boolean(file?.file_id),
  );
  if (validFiles.length === 0) {
    return '';
  }

  const boundedFiles =
    maxImages > 0 && validFiles.length > maxImages ? validFiles.slice(-maxImages) : validFiles;
  const lines = boundedFiles.map((file) => {
    const filename = file.filename || 'unnamed';
    const kind =
      file.kind ?? (file.context === FileContext.image_generation ? 'generated' : 'upload');
    const turnLabel = typeof file.turn === 'number' ? `turn ${file.turn}` : file.turn || 'turn 1';
    return `\t- ${file.file_id} (${filename}, ${kind}, ${turnLabel})`;
  });

  return [
    'Conversation images available for MCP tools:',
    ...lines,
    '',
    'Pass image IDs using `gemini_image_gen.image_ids`, `select_region.image_id`, `edit_region.base_id`, `compare_materials.base_id`, `references[].id` with `references[].use`, or `create_3d_model.image_id`.',
  ].join('\n');
}

const STUDIO_KB_ID_RE = /^kb_([0-9a-f]{16})$/i;
const STUDIO_IMG_ID_RE = /^img_[0-9a-f]{32}$/i;
const STUDIO_USER_ID_RE = /^[a-f0-9]{24}$/i;
const ENV_TEMPLATE_RE = /^\$\{([A-Za-z0-9_]+)\}$/;
const ALLOWED_STUDIO_ORIGINAL_FILES = new Set(['original.png', 'original.jpg', 'original.webp']);

export interface GeminiReferenceFileRecord {
  file_id: string;
  filepath: string;
  type?: string;
  source?: string;
  filename?: string;
  width?: number;
  height?: number;
  bytes?: number;
  user?: string | { toString(): string };
}

export interface GeminiInlineImagePart {
  inlineData: {
    mimeType: string;
    data: string;
  };
}

export type ResolveGeminiReferenceErrorCode =
  | 'UNRESOLVED_IMAGE_REFERENCES'
  | 'TOO_MANY_IMAGE_REFERENCES'
  | 'IMAGE_REFERENCE_TOO_LARGE';

export type ResolveGeminiReferencesResult =
  | {
      ok: true;
      value: GeminiInlineImagePart[];
    }
  | {
      ok: false;
      error: {
        code: ResolveGeminiReferenceErrorCode;
        unresolvedIds: string[];
        message: string;
      };
    };

export interface GeminiReferenceRequestContext {
  user?: {
    id?: string;
    tenantId?: string | null;
  };
  config?: {
    imageReferences?: Partial<TImageReferencesConfig>;
    mcpServers?: Record<
      string,
      | {
          url?: string;
          headers?: Record<string, string>;
        }
      | undefined
    >;
  } & Partial<AppConfig>;
}

export interface ResolveGeminiReferenceImagesParams {
  imageFiles?: GeminiReferenceFileRecord[];
  image_ids?: string[];
  req?: GeminiReferenceRequestContext;
  userId?: string;
  fileStrategy?: string;
  kbRoot?: string;
  dataDir?: string;
  settingsFile?: string;
  limits?: Partial<TImageReferencesConfig>;
  getFiles?: (
    filter: Record<string, unknown>,
    sortOptions?: Record<string, unknown>,
    selectFields?: Record<string, unknown>,
  ) => Promise<GeminiReferenceFileRecord[] | null | undefined>;
  createFile?: (
    data: Record<string, unknown>,
    disableTTL?: boolean,
  ) => Promise<GeminiReferenceFileRecord | null | undefined>;
  saveBase64Image?: (
    url: string,
    options: {
      req?: GeminiReferenceRequestContext;
      file_id: string;
      filename: string;
      endpoint: string;
      context: string;
    },
  ) => Promise<GeminiReferenceFileRecord | null | undefined>;
  getStrategyFunctions?: (source: string) => {
    saveBuffer?: (params: {
      userId: string;
      fileName: string;
      buffer: Buffer;
      tenantId?: string | null;
    }) => Promise<string>;
    getDownloadStream?: (
      req: GeminiReferenceRequestContext | undefined,
      filepath: string,
    ) => Promise<AsyncIterable<Buffer | Uint8Array | string> | null | undefined>;
  };
  fetchImpl?: typeof fetch;
}

// 判断给定引用编号是否为工作台知识库资产（kb_<16位hex>）或专精工具产物（img_<32位hex>）
export function isStudioReferenceId(id: string): boolean {
  if (typeof id !== 'string') {
    return false;
  }
  const trimmed = id.trim();
  return STUDIO_KB_ID_RE.test(trimmed) || STUDIO_IMG_ID_RE.test(trimmed);
}

// 为特定用户与工作台引用编号派生确定性 UUID，确保多用户桥接同一 kb_ 资产时各自拥有独立的授权 File 记录
export function deriveStudioBridgeFileId(userId: string, studioRefId: string): string {
  const digest = crypto
    .createHash('sha256')
    .update(`studio-bridge:${String(userId).trim()}:${String(studioRefId).trim().toLowerCase()}`)
    .digest('hex');
  return [
    digest.slice(0, 8),
    digest.slice(8, 12),
    `5${digest.slice(13, 16)}`,
    `8${digest.slice(17, 20)}`,
    digest.slice(20, 32),
  ].join('-');
}

// 从图像字节读取真实宽高尺寸，当非标准测试数据无法解析时回退至默认尺寸
export async function readImageDimensions(
  buffer: Buffer,
): Promise<{ width: number; height: number }> {
  try {
    const metadata = await sharp(buffer).rotate().metadata();
    if (
      typeof metadata.width === 'number' &&
      metadata.width > 0 &&
      typeof metadata.height === 'number' &&
      metadata.height > 0
    ) {
      return { width: metadata.width, height: metadata.height };
    }
  } catch {
    // 合成测试字节非有效图像容器时回退默认尺寸
  }
  return { width: 1024, height: 1024 };
}

type SafeReadResult =
  | { status: 'ok'; buffer: Buffer }
  | { status: 'too_large'; size: number }
  | { status: 'missing' };

// 校验候选文件路径不包含符号链接且严格位于允许根目录内，并在读取前检查文件大小上限
function readSafeFileUnderRootBounded(
  filePath: string,
  rootDir: string,
  maxBytes?: number,
): SafeReadResult {
  try {
    const rootStat = fs.lstatSync(rootDir);
    if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
      return { status: 'missing' };
    }
    const resolvedRoot = fs.realpathSync(rootDir);
    const fileStat = fs.lstatSync(filePath);
    if (!fileStat.isFile() || fileStat.isSymbolicLink()) {
      return { status: 'missing' };
    }
    let current = path.dirname(filePath);
    while (current !== rootDir && current !== path.dirname(current)) {
      const dirStat = fs.lstatSync(current);
      if (dirStat.isSymbolicLink()) {
        return { status: 'missing' };
      }
      current = path.dirname(current);
    }
    const resolvedFile = fs.realpathSync(filePath);
    const rel = path.relative(resolvedRoot, resolvedFile);
    if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) {
      return { status: 'missing' };
    }
    if (typeof maxBytes === 'number' && maxBytes > 0 && fileStat.size > maxBytes) {
      return { status: 'too_large', size: fileStat.size };
    }
    return { status: 'ok', buffer: fs.readFileSync(resolvedFile) };
  } catch {
    return { status: 'missing' };
  }
}

// 读取可选 studio.json 配置文件中的目录与共享令牌设置
function readStudioSettingsJson(settingsFile?: string): {
  kb_root?: string;
  data_dir?: string;
  shared_token?: string;
} {
  const candidate = settingsFile || process.env.STUDIO_SETTINGS_FILE || '/run/secrets/studio.json';
  try {
    const stat = fs.lstatSync(candidate);
    if (!stat.isFile() || stat.isSymbolicLink()) {
      return {};
    }
    const parsed = JSON.parse(fs.readFileSync(candidate, 'utf8')) as Record<string, unknown>;
    return {
      kb_root: typeof parsed.kb_root === 'string' ? parsed.kb_root : undefined,
      data_dir: typeof parsed.data_dir === 'string' ? parsed.data_dir : undefined,
      shared_token: typeof parsed.shared_token === 'string' ? parsed.shared_token : undefined,
    };
  } catch {
    return {};
  }
}

// 解析可能带有 ${ENV_VAR} 占位符的环境变量值
function resolveTemplateEnvValue(rawValue: string | undefined): string {
  if (!rawValue) {
    return '';
  }
  const trimmed = rawValue.trim();
  const match = ENV_TEMPLATE_RE.exec(trimmed);
  if (match) {
    return (process.env[match[1]] || '').trim();
  }
  return trimmed;
}

type StudioReferenceLoadResult =
  | { status: 'ok'; buffer: Buffer; mimeType: string }
  | { status: 'too_large' }
  | { status: 'missing' };

// 按当前用户权限与字节/超时上限加载 kb_ 或 img_ 原始图像字节（优先本地安全挂载目录，回退至内部认证的 Studio 文件接口）
async function loadStudioReferenceBytes({
  refId,
  userId,
  req,
  kbRoot,
  dataDir,
  settingsFile,
  maxImageBytes,
  remoteTimeoutMs,
  fetchImpl = globalThis.fetch,
}: {
  refId: string;
  userId: string;
  req?: GeminiReferenceRequestContext;
  kbRoot?: string;
  dataDir?: string;
  settingsFile?: string;
  maxImageBytes?: number;
  remoteTimeoutMs?: number;
  fetchImpl?: typeof fetch;
}): Promise<StudioReferenceLoadResult> {
  const normalizedId = refId.trim().toLowerCase();
  const normalizedUserId = userId.trim().toLowerCase();
  if (!STUDIO_USER_ID_RE.test(normalizedUserId)) {
    return { status: 'missing' };
  }

  const settings = readStudioSettingsJson(settingsFile);
  const effectiveKbRoot = kbRoot || process.env.STUDIO_KB_ROOT || settings.kb_root || '/kb';
  const effectiveDataDir = dataDir || process.env.STUDIO_DATA_DIR || settings.data_dir || '/data';

  const kbMatch = STUDIO_KB_ID_RE.exec(normalizedId);
  if (kbMatch) {
    const assetHex = kbMatch[1].toLowerCase();
    const recordsDir = path.join(effectiveKbRoot, 'records');
    let recordAllowed = true;
    try {
      if (fs.existsSync(recordsDir) && fs.lstatSync(recordsDir).isDirectory()) {
        const recordPath = path.join(recordsDir, `${assetHex}.yaml`);
        recordAllowed = readSafeFileUnderRootBounded(recordPath, effectiveKbRoot).status === 'ok';
      }
    } catch {
      recordAllowed = false;
    }
    if (recordAllowed) {
      const refPath = path.join(effectiveKbRoot, 'assets', 'ref2048', `${assetHex}.jpg`);
      const readRes = readSafeFileUnderRootBounded(refPath, effectiveKbRoot, maxImageBytes);
      if (readRes.status === 'too_large') {
        return { status: 'too_large' };
      }
      if (readRes.status === 'ok' && readRes.buffer.length > 0) {
        return { status: 'ok', buffer: readRes.buffer, mimeType: 'image/jpeg' };
      }
    }
  } else if (STUDIO_IMG_ID_RE.test(normalizedId)) {
    const userRoot = path.join(effectiveDataDir, 'users', normalizedUserId);
    const jobDir = path.join(userRoot, 'jobs', normalizedId);
    const receiptRes = readSafeFileUnderRootBounded(path.join(jobDir, 'receipt.json'), userRoot);
    if (receiptRes.status === 'ok') {
      try {
        const receipt = JSON.parse(receiptRes.buffer.toString('utf8')) as Record<string, unknown>;
        const fileField = typeof receipt.file === 'string' ? receipt.file : '';
        if (receipt.state === 'saved' && ALLOWED_STUDIO_ORIGINAL_FILES.has(fileField)) {
          const origRes = readSafeFileUnderRootBounded(
            path.join(jobDir, fileField),
            userRoot,
            maxImageBytes,
          );
          if (origRes.status === 'too_large') {
            return { status: 'too_large' };
          }
          if (origRes.status === 'ok' && origRes.buffer.length > 0) {
            let mimeType = 'image/jpeg';
            if (fileField === 'original.png') {
              mimeType = 'image/png';
            } else if (fileField === 'original.webp') {
              mimeType = 'image/webp';
            }
            return { status: 'ok', buffer: origRes.buffer, mimeType };
          }
        }
      } catch {
        // receipt 解析失败时继续尝试远端或最终按失败关闭处理
      }
    }
  } else {
    return { status: 'missing' };
  }

  // 当 api 与 studio 分处不同容器时，通过配置的 zyb_studio 内部地址与共享令牌按用户身份拉取原图字节
  const studioMcpConfig = req?.config?.mcpServers?.zyb_studio;
  const rawMcpUrl =
    studioMcpConfig?.url || process.env.STUDIO_MCP_URL || process.env.STUDIO_URL || '';
  const sharedToken =
    resolveTemplateEnvValue(studioMcpConfig?.headers?.['X-Studio-Token']) ||
    (process.env.STUDIO_SHARED_TOKEN || '').trim() ||
    (settings.shared_token || '').trim();

  if (!rawMcpUrl || !sharedToken || typeof fetchImpl !== 'function') {
    return { status: 'missing' };
  }

  const baseStudioUrl = rawMcpUrl.replace(/\/mcp\/?$/, '').replace(/\/+$/, '');
  if (!baseStudioUrl) {
    return { status: 'missing' };
  }

  const controller = new AbortController();
  const timeoutMs =
    typeof remoteTimeoutMs === 'number' && remoteTimeoutMs > 0 ? remoteTimeoutMs : 10_000;
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetchImpl(`${baseStudioUrl}/files/${encodeURIComponent(normalizedId)}`, {
      method: 'GET',
      headers: {
        'X-Studio-Token': sharedToken,
        'X-LibreChat-User-ID': normalizedUserId,
      },
      signal: controller.signal,
    });
    if (!response.ok) {
      return { status: 'missing' };
    }
    const contentLengthHeader = response.headers?.get?.('content-length');
    if (
      contentLengthHeader &&
      typeof maxImageBytes === 'number' &&
      maxImageBytes > 0 &&
      Number(contentLengthHeader) > maxImageBytes
    ) {
      controller.abort();
      return { status: 'too_large' };
    }
    const contentType = (response.headers?.get?.('content-type') || '')
      .split(';')[0]
      .trim()
      .toLowerCase();
    let mimeType = normalizedId.startsWith('kb_') ? 'image/jpeg' : 'image/png';
    if (
      contentType === 'image/png' ||
      contentType === 'image/webp' ||
      contentType === 'image/jpeg'
    ) {
      mimeType = contentType;
    }

    let buffer: Buffer;
    const reader = response.body?.getReader?.();
    if (reader) {
      const chunks: Buffer[] = [];
      let total = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        if (value) {
          const bufChunk = Buffer.from(value);
          total += bufChunk.length;
          if (typeof maxImageBytes === 'number' && maxImageBytes > 0 && total > maxImageBytes) {
            await reader.cancel().catch(() => undefined);
            controller.abort();
            return { status: 'too_large' };
          }
          chunks.push(bufChunk);
        }
      }
      buffer = Buffer.concat(chunks);
    } else {
      const arrayBuf = await response.arrayBuffer();
      buffer = Buffer.from(arrayBuf);
      if (typeof maxImageBytes === 'number' && maxImageBytes > 0 && buffer.length > maxImageBytes) {
        return { status: 'too_large' };
      }
    }

    if (buffer.length === 0) {
      return { status: 'missing' };
    }
    return { status: 'ok', buffer, mimeType };
  } catch {
    return { status: 'missing' };
  } finally {
    clearTimeout(timer);
  }
}

// 将 gemini_image_gen 请求的 image_ids（含 kb_ 与 img_ 引用）解析并桥接为当前用户的授权 LibreChat File 记录；实施数量与字节上限并对重复引用去重
export async function resolveGeminiReferenceImages({
  imageFiles = [],
  image_ids = [],
  req,
  userId,
  fileStrategy,
  kbRoot,
  dataDir,
  settingsFile,
  limits,
  getFiles,
  createFile,
  saveBase64Image,
  getStrategyFunctions,
  fetchImpl,
}: ResolveGeminiReferenceImagesParams): Promise<ResolveGeminiReferencesResult> {
  if (!image_ids || image_ids.length === 0) {
    return { ok: true, value: [] };
  }

  const effectiveLimits = resolveImageReferencesConfig(limits ?? req?.config?.imageReferences);
  if (image_ids.length > effectiveLimits.maxItems) {
    return {
      ok: false,
      error: {
        code: 'TOO_MANY_IMAGE_REFERENCES',
        unresolvedIds: [],
        message: `Too many reference image IDs (${image_ids.length}); maximum allowed is ${effectiveLimits.maxItems} (code: TOO_MANY_IMAGE_REFERENCES).`,
      },
    };
  }

  const effectiveUserId = String(userId || req?.user?.id || '').trim();

  // 按输入顺序去重（包括相同 ID 以及同一 Studio 资产的原始 ID 与派生桥接 UUID）
  const uniqueImageIds: string[] = [];
  const seenInputKeys = new Set<string>();
  for (let i = 0; i < image_ids.length; i++) {
    const rawId = image_ids[i];
    const trimmed = typeof rawId === 'string' ? rawId.trim() : '';
    if (!trimmed) {
      uniqueImageIds.push('');
      continue;
    }
    const canonicalKey =
      effectiveUserId && isStudioReferenceId(trimmed)
        ? deriveStudioBridgeFileId(effectiveUserId, trimmed).toLowerCase()
        : trimmed.toLowerCase();
    if (seenInputKeys.has(canonicalKey)) {
      continue;
    }
    seenInputKeys.add(canonicalKey);
    uniqueImageIds.push(trimmed);
  }

  const requestFilesMap = new Map<string, GeminiReferenceFileRecord>();
  for (const file of imageFiles) {
    if (file?.file_id) {
      requestFilesMap.set(file.file_id, { ...file });
    }
  }

  const orderedFiles: Array<GeminiReferenceFileRecord | undefined> = new Array(
    uniqueImageIds.length,
  );
  const idsToFetch = new Set<string>();
  const missingIndices: number[] = [];

  for (let i = 0; i < uniqueImageIds.length; i++) {
    const id = uniqueImageIds[i];
    if (!id) {
      continue;
    }
    const directFile = requestFilesMap.get(id);
    if (directFile) {
      orderedFiles[i] = directFile;
      continue;
    }
    if (effectiveUserId && isStudioReferenceId(id)) {
      const bridgedId = deriveStudioBridgeFileId(effectiveUserId, id);
      const bridgedRequestFile = requestFilesMap.get(bridgedId);
      if (bridgedRequestFile) {
        orderedFiles[i] = bridgedRequestFile;
        continue;
      }
      idsToFetch.add(bridgedId);
    }
    idsToFetch.add(id);
    missingIndices.push(i);
  }

  if (idsToFetch.size > 0 && effectiveUserId && typeof getFiles === 'function') {
    const fetchedFiles =
      (await getFiles(
        {
          user: effectiveUserId,
          file_id: { $in: Array.from(idsToFetch) },
          height: { $exists: true },
          width: { $exists: true },
        },
        {},
        {},
      )) ?? [];

    const fetchedById = new Map<string, GeminiReferenceFileRecord>();
    for (const file of fetchedFiles) {
      if (file?.file_id) {
        fetchedById.set(file.file_id, file);
      }
    }

    for (const idx of missingIndices) {
      const id = uniqueImageIds[idx];
      const matched =
        fetchedById.get(id) ??
        (isStudioReferenceId(id)
          ? fetchedById.get(deriveStudioBridgeFileId(effectiveUserId, id))
          : undefined);
      if (matched) {
        orderedFiles[idx] = matched;
      }
    }
  }

  // 对尚未存在 File 记录的 kb_ 与 img_ 引用，按当前用户权限加载原图并测量真实尺寸后创建正式 File 记录
  const bridgedCache = new Map<string, GeminiReferenceFileRecord>();
  for (let i = 0; i < uniqueImageIds.length; i++) {
    if (orderedFiles[i]) {
      continue;
    }
    const id = uniqueImageIds[i];
    if (!id || !effectiveUserId || !isStudioReferenceId(id)) {
      continue;
    }
    const normalizedId = id.toLowerCase();
    const cached = bridgedCache.get(normalizedId);
    if (cached) {
      orderedFiles[i] = cached;
      continue;
    }
    const canSaveViaBase64 = typeof saveBase64Image === 'function';
    const canSaveViaStrategy =
      typeof createFile === 'function' && typeof getStrategyFunctions === 'function';
    if (!canSaveViaBase64 && !canSaveViaStrategy) {
      continue;
    }
    const loaded = await loadStudioReferenceBytes({
      refId: normalizedId,
      userId: effectiveUserId,
      req,
      kbRoot,
      dataDir,
      settingsFile,
      maxImageBytes: effectiveLimits.maxImageBytes,
      remoteTimeoutMs: effectiveLimits.remoteTimeoutMs,
      fetchImpl,
    });
    if (loaded.status === 'too_large') {
      return {
        ok: false,
        error: {
          code: 'IMAGE_REFERENCE_TOO_LARGE',
          unresolvedIds: [id],
          message: `Reference image "${id}" exceeds maximum allowed size of ${effectiveLimits.maxImageBytes} bytes (code: IMAGE_REFERENCE_TOO_LARGE).`,
        },
      };
    }
    if (loaded.status !== 'ok') {
      continue;
    }
    const bridgedFileId = deriveStudioBridgeFileId(effectiveUserId, normalizedId);
    const baseFilename = `studio_ref_${normalizedId}_img_${bridgedFileId.slice(0, 8)}`;
    const effectiveReq: GeminiReferenceRequestContext = {
      ...req,
      user: {
        ...req?.user,
        id: effectiveUserId,
      },
      config: req?.config ?? {},
    };
    try {
      let savedFile: GeminiReferenceFileRecord | null | undefined;
      if (canSaveViaStrategy && createFile && getStrategyFunctions) {
        const source = fileStrategy || 'local';
        const { saveBuffer } = getStrategyFunctions(source);
        if (typeof saveBuffer === 'function') {
          let ext = 'png';
          if (loaded.mimeType === 'image/jpeg') {
            ext = 'jpg';
          } else if (loaded.mimeType === 'image/webp') {
            ext = 'webp';
          }
          const fullFilename = `${bridgedFileId}-${baseFilename}.${ext}`;
          const { width, height } = await readImageDimensions(loaded.buffer);
          const filepath = await saveBuffer({
            userId: effectiveUserId,
            fileName: fullFilename,
            buffer: loaded.buffer,
            tenantId: req?.user?.tenantId,
          });
          if (filepath) {
            savedFile = await createFile(
              {
                type: loaded.mimeType,
                source,
                context: FileContext.image_generation,
                file_id: bridgedFileId,
                filepath,
                filename: fullFilename,
                user: effectiveUserId,
                bytes: loaded.buffer.length,
                width,
                height,
                tenantId: req?.user?.tenantId,
              },
              true,
            );
          }
        }
      } else if (canSaveViaBase64 && saveBase64Image) {
        const dataUrl = `data:${loaded.mimeType};base64,${loaded.buffer.toString('base64')}`;
        savedFile = await saveBase64Image(dataUrl, {
          req: effectiveReq,
          file_id: bridgedFileId,
          filename: baseFilename,
          endpoint: 'agents',
          context: FileContext.image_generation,
        });
      }
      if (savedFile?.filepath) {
        orderedFiles[i] = savedFile;
        bridgedCache.set(normalizedId, savedFile);
      }
    } catch {
      // 保存失败则保持未解析状态并触发失败关闭
    }
  }

  const streamMethods: Record<
    string,
    | ((
        req: GeminiReferenceRequestContext | undefined,
        filepath: string,
      ) => Promise<AsyncIterable<Buffer | Uint8Array | string> | null | undefined>)
    | undefined
  > = {};
  const inlineDataArray: GeminiInlineImagePart[] = [];
  const unresolvedIds: string[] = [];
  const encodedFileIds = new Set<string>();
  let totalBytes = 0;

  const streamReq: GeminiReferenceRequestContext | undefined = effectiveUserId
    ? {
        ...req,
        user: {
          ...req?.user,
          id: effectiveUserId,
        },
        config: req?.config ?? {},
      }
    : req;

  for (let i = 0; i < uniqueImageIds.length; i++) {
    const requestedId = uniqueImageIds[i] || `<empty_at_${i}>`;
    const imageFile = orderedFiles[i];
    if (!imageFile) {
      unresolvedIds.push(requestedId);
      continue;
    }

    if (imageFile.file_id && encodedFileIds.has(imageFile.file_id)) {
      continue;
    }

    if (
      typeof imageFile.bytes === 'number' &&
      (imageFile.bytes > effectiveLimits.maxImageBytes ||
        totalBytes + imageFile.bytes > effectiveLimits.maxTotalBytes)
    ) {
      return {
        ok: false,
        error: {
          code: 'IMAGE_REFERENCE_TOO_LARGE',
          unresolvedIds: [requestedId],
          message: `Reference image "${requestedId}" exceeds configured byte limits (code: IMAGE_REFERENCE_TOO_LARGE).`,
        },
      };
    }

    const source = imageFile.source || fileStrategy;
    if (!source || typeof getStrategyFunctions !== 'function') {
      unresolvedIds.push(requestedId);
      continue;
    }

    try {
      let getDownloadStream = streamMethods[source];
      if (!getDownloadStream) {
        ({ getDownloadStream } = getStrategyFunctions(source));
        streamMethods[source] = getDownloadStream;
      }
      if (!getDownloadStream) {
        unresolvedIds.push(requestedId);
        continue;
      }

      const stream = await getDownloadStream(streamReq, imageFile.filepath);
      if (!stream) {
        unresolvedIds.push(requestedId);
        continue;
      }

      const chunks: Buffer[] = [];
      let imageBytes = 0;
      for await (const chunk of stream) {
        const bufChunk = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        imageBytes += bufChunk.length;
        if (
          imageBytes > effectiveLimits.maxImageBytes ||
          totalBytes + imageBytes > effectiveLimits.maxTotalBytes
        ) {
          return {
            ok: false,
            error: {
              code: 'IMAGE_REFERENCE_TOO_LARGE',
              unresolvedIds: [requestedId],
              message: `Reference image "${requestedId}" exceeds configured byte limits (code: IMAGE_REFERENCE_TOO_LARGE).`,
            },
          };
        }
        chunks.push(bufChunk);
      }
      const buffer = Buffer.concat(chunks);
      if (buffer.length === 0) {
        unresolvedIds.push(requestedId);
        continue;
      }
      totalBytes += buffer.length;
      if (imageFile.file_id) {
        encodedFileIds.add(imageFile.file_id);
      }
      const base64Data = buffer.toString('base64');
      const mimeType = imageFile.type || 'image/png';
      inlineDataArray.push({
        inlineData: { mimeType, data: base64Data },
      });
    } catch {
      unresolvedIds.push(requestedId);
    }
  }

  if (unresolvedIds.length > 0) {
    return {
      ok: false,
      error: {
        code: 'UNRESOLVED_IMAGE_REFERENCES',
        unresolvedIds,
        message: `Referenced image ID(s) could not be resolved or accessed: ${unresolvedIds.join(', ')}`,
      },
    };
  }

  return { ok: true, value: inlineDataArray };
}

// 从 MCP 工具输出文本中提取 Studio 专精工具（edit_region、finish_region_edit、compare_materials）产出的规范 img_<32位hex> 列表
export function extractStudioArtifactImageIds(output: {
  name?: string;
  content?: unknown;
}): string[] {
  const texts: string[] = [];
  if (typeof output?.content === 'string') {
    texts.push(output.content);
  } else if (Array.isArray(output?.content)) {
    for (const part of output.content) {
      if (typeof part === 'string') {
        texts.push(part);
      } else if (
        part &&
        typeof part === 'object' &&
        typeof (part as { text?: unknown }).text === 'string'
      ) {
        texts.push((part as { text: string }).text);
      }
    }
  }

  const foundIds: string[] = [];
  for (const text of texts) {
    const trimmed = text.trim();
    if (!trimmed.startsWith('{')) {
      continue;
    }
    try {
      const parsed = JSON.parse(trimmed) as Record<string, unknown>;
      const readCandidateId = (record: Record<string, unknown>): string => {
        if (typeof record.image_id === 'string') {
          return record.image_id;
        }
        if (typeof record.id === 'string') {
          return record.id;
        }
        return '';
      };
      if (Array.isArray(parsed.variants)) {
        for (const variant of parsed.variants) {
          if (!variant || typeof variant !== 'object') {
            continue;
          }
          const candidate = readCandidateId(variant as Record<string, unknown>);
          if (STUDIO_IMG_ID_RE.test(candidate.trim())) {
            foundIds.push(candidate.trim().toLowerCase());
          }
        }
      } else {
        const candidate = readCandidateId(parsed);
        if (STUDIO_IMG_ID_RE.test(candidate.trim())) {
          foundIds.push(candidate.trim().toLowerCase());
        }
      }
    } catch {
      // 非 JSON 文本块忽略
    }
  }
  return foundIds;
}

const PERSISTENCE_ERROR_TEXT =
  'Image persistence failed (code: IMAGE_PERSISTENCE_FAILED). The generated image could not be saved.';

// 当图像落盘失败时清除对外暴露的 generated_image_id 与 artifact.file_ids，防止后续工具引用不存在的文件 ID
function scrubUnpersistedImageOutput(output: {
  content?: unknown;
  artifact?: {
    file_ids?: string[];
    content?: unknown[];
    files?: unknown[];
  };
}): void {
  if (output.artifact) {
    output.artifact.file_ids = [];
    output.artifact.content = [];
    output.artifact.files = [];
  }
  if (typeof output.content === 'string') {
    const scrubbed = output.content
      .replace(/\n*generated_image_id:\s*"[^"]*"/g, '')
      .replace(/\n*referenced_image_ids:\s*\[[^\]]*\]/g, '')
      .trim();
    output.content = scrubbed ? `${scrubbed}\n\n${PERSISTENCE_ERROR_TEXT}` : PERSISTENCE_ERROR_TEXT;
  } else if (Array.isArray(output.content)) {
    let updated = false;
    output.content = output.content.map((part) => {
      if (
        part &&
        typeof part === 'object' &&
        typeof (part as { text?: unknown }).text === 'string'
      ) {
        const rawText = (part as { text: string }).text;
        if (rawText.includes('generated_image_id:')) {
          updated = true;
          return {
            ...(part as Record<string, unknown>),
            text: PERSISTENCE_ERROR_TEXT,
          };
        }
      }
      return part;
    });
    if (!updated) {
      output.content = [{ type: ContentTypes.TEXT, text: PERSISTENCE_ERROR_TEXT }];
    }
  } else {
    output.content = [{ type: ContentTypes.TEXT, text: PERSISTENCE_ERROR_TEXT }];
  }
}

export interface ProcessToolImageArtifactsParams {
  output: {
    name?: string;
    tool_call_id?: string;
    content?: unknown;
    artifact?: {
      file_ids?: string[];
      files?: Array<GeminiReferenceFileRecord | null | undefined>;
      content?: Array<{
        type?: string;
        image_url?: { url?: string };
      } | null>;
    };
  };
  req?: GeminiReferenceRequestContext;
  metadata?: {
    run_id?: string;
    thread_id?: string;
    provider?: string;
    user_id?: string;
  };
  artifactPromises: Array<Promise<Record<string, unknown> | null>>;
  saveBase64Image?: (
    url: string,
    options: {
      req?: GeminiReferenceRequestContext;
      file_id?: string;
      filename: string;
      endpoint?: string;
      context: string;
    },
  ) => Promise<Record<string, unknown> | null | undefined>;
  createFile?: (
    data: Record<string, unknown>,
    disableTTL?: boolean,
  ) => Promise<GeminiReferenceFileRecord | null | undefined>;
  getStrategyFunctions?: (source: string) => {
    saveBuffer?: (params: {
      userId: string;
      fileName: string;
      buffer: Buffer;
      tenantId?: string | null;
    }) => Promise<string>;
  };
  kbRoot?: string;
  dataDir?: string;
  settingsFile?: string;
  fileStrategy?: string;
  fetchImpl?: typeof fetch;
  logger?: {
    error?: (...args: unknown[]) => void;
    warn?: (...args: unknown[]) => void;
  };
  buildMetadata: (file: Record<string, unknown>) => Record<string, unknown> | null;
  onFileSaved: (fileMetadata: Record<string, unknown>) => void;
}

// 统一处理工具图像产物的持久化屏障：对 Studio 专精工具优先拉取规范无损原图直接落盘，并在任何保存失败时抹除未发布的图像 ID
export async function processToolImageArtifacts({
  output,
  req,
  metadata,
  artifactPromises,
  saveBase64Image,
  createFile,
  getStrategyFunctions,
  kbRoot,
  dataDir,
  settingsFile,
  fileStrategy,
  fetchImpl,
  logger = defaultLogger,
  buildMetadata,
  onFileSaved,
}: ProcessToolImageArtifactsParams): Promise<void> {
  const content = output?.artifact?.content;
  if (!Array.isArray(content) || content.length === 0) {
    return;
  }

  const studioImgIds = extractStudioArtifactImageIds(output);
  const effectiveUserId = String(req?.user?.id || metadata?.user_id || '').trim();
  const effectiveLimits = resolveImageReferencesConfig(req?.config?.imageReferences);
  const imageArtifactPromises: Array<Promise<Record<string, unknown> | null>> = [];
  let hasPersistenceFailure = false;

  for (let i = 0; i < content.length; i++) {
    const part = content[i];
    if (!part || part.type !== 'image_url') {
      continue;
    }
    const url = part.image_url?.url;
    if (!url) {
      continue;
    }

    const prePersisted = output.artifact?.files?.[i];
    const studioImgId = studioImgIds[i];

    const imagePromise = (async (): Promise<Record<string, unknown> | null> => {
      let file: Record<string, unknown> | null | undefined;

      if (prePersisted && prePersisted.file_id && prePersisted.filepath) {
        file = { ...prePersisted };
      } else if (studioImgId && effectiveUserId) {
        const loaded = await loadStudioReferenceBytes({
          refId: studioImgId,
          userId: effectiveUserId,
          req,
          kbRoot,
          dataDir,
          settingsFile,
          maxImageBytes: effectiveLimits.maxImageBytes,
          remoteTimeoutMs: effectiveLimits.remoteTimeoutMs,
          fetchImpl,
        });
        if (loaded.status === 'ok') {
          const file_id =
            output.artifact?.file_ids?.[i] ||
            deriveStudioBridgeFileId(effectiveUserId, studioImgId);
          const baseFilename = `${output.name || 'studio'}_img_${studioImgId}`;
          if (typeof createFile === 'function' && typeof getStrategyFunctions === 'function') {
            const source =
              fileStrategy || (req?.config?.fileStrategy as string | undefined) || 'local';
            const { saveBuffer } = getStrategyFunctions(source);
            if (typeof saveBuffer === 'function') {
              let ext = 'png';
              if (loaded.mimeType === 'image/jpeg') {
                ext = 'jpg';
              } else if (loaded.mimeType === 'image/webp') {
                ext = 'webp';
              }
              const fullFilename = `${file_id}-${baseFilename}.${ext}`;
              const { width, height } = await readImageDimensions(loaded.buffer);
              const filepath = await saveBuffer({
                userId: effectiveUserId,
                fileName: fullFilename,
                buffer: loaded.buffer,
                tenantId: req?.user?.tenantId,
              });
              if (filepath) {
                const created = await createFile(
                  {
                    type: loaded.mimeType,
                    source,
                    context: FileContext.image_generation,
                    file_id,
                    filepath,
                    filename: fullFilename,
                    user: effectiveUserId,
                    bytes: loaded.buffer.length,
                    width,
                    height,
                    tenantId: req?.user?.tenantId,
                  },
                  true,
                );
                if (created) {
                  file = { ...created };
                }
              }
            }
          } else if (typeof saveBase64Image === 'function') {
            const canonicalUrl = `data:${loaded.mimeType};base64,${loaded.buffer.toString('base64')}`;
            file = await saveBase64Image(canonicalUrl, {
              req,
              file_id,
              filename: baseFilename,
              endpoint: metadata?.provider,
              context: FileContext.image_generation,
            });
          }
          if (file && output.artifact) {
            output.artifact.file_ids = output.artifact.file_ids ?? [];
            output.artifact.file_ids[i] = file_id;
          }
        }
      } else if (typeof saveBase64Image === 'function') {
        const filename = `${output.name}_img_${nanoid()}`;
        const file_id = output.artifact?.file_ids?.[i];
        file = await saveBase64Image(url, {
          req,
          file_id,
          filename,
          endpoint: metadata?.provider,
          context: FileContext.image_generation,
        });
      }

      if (!file || typeof file.file_id !== 'string' || typeof file.filepath !== 'string') {
        hasPersistenceFailure = true;
        return null;
      }

      const fileMetadata = buildMetadata(file);
      if (!fileMetadata) {
        hasPersistenceFailure = true;
        return null;
      }

      onFileSaved(fileMetadata);
      return fileMetadata;
    })().catch((error) => {
      hasPersistenceFailure = true;
      logger?.error?.('Error processing artifact content:', error);
      return null;
    });

    artifactPromises.push(imagePromise);
    imageArtifactPromises.push(imagePromise);
  }

  await Promise.all(imageArtifactPromises);

  if (hasPersistenceFailure) {
    scrubUnpersistedImageOutput(output);
  }
}

// 为 googleapis.com 域名请求按需挂载代理分发器
export function configureGoogleApiProxyFetch(): void {
  const googleApiProxyDispatcher = getEnvProxyDispatcher();
  if (!googleApiProxyDispatcher) {
    return;
  }
  const originalFetch = globalThis.fetch;
  globalThis.fetch = Object.assign(function (
    this: unknown,
    url: Parameters<typeof fetch>[0],
    options: Parameters<typeof fetch>[1] = {},
  ) {
    const urlString = url.toString();
    if (urlString.includes('googleapis.com')) {
      options = {
        ...options,
        dispatcher: googleApiProxyDispatcher,
      } as Parameters<typeof fetch>[1];
    }
    return originalFetch.call(this, url, options);
  }, originalFetch);
}

function getDefaultServiceKeyPath(): string {
  return (
    process.env.GOOGLE_SERVICE_KEY_FILE || path.join(process.cwd(), 'api', 'data', 'auth.json')
  );
}

const GEMINI_DISPLAY_MESSAGE =
  "Gemini displayed an image. All generated images are already plainly visible, so don't repeat the descriptions in detail. Do not list download links as they are available in the UI already. The user may download the images by clicking on them, but do not mention anything about downloading to the user.";

function replaceUnwantedChars(inputString?: string): string {
  return (
    inputString
      ?.replace(/\r\n|\r|\n/g, ' ')
      .replace(/"/g, '')
      .trim() || ''
  );
}

async function convertImageFormat(
  inputBuffer: Buffer,
  targetFormat: string,
): Promise<{ buffer: Buffer; format: string }> {
  const metadata = await sharp(inputBuffer).metadata();
  const currentFormat: string | undefined = metadata.format;
  const normalizedTarget = targetFormat === 'jpg' ? 'jpeg' : targetFormat.toLowerCase();
  const normalizedCurrent = currentFormat === 'jpg' ? 'jpeg' : currentFormat;
  if (normalizedCurrent === normalizedTarget) {
    return { buffer: inputBuffer, format: normalizedTarget };
  }
  const convertedBuffer = await sharp(inputBuffer)
    .toFormat(normalizedTarget as keyof FormatEnum)
    .toBuffer();
  return { buffer: convertedBuffer, format: normalizedTarget };
}

interface GeminiClientOptions {
  GEMINI_API_KEY?: string;
  GOOGLE_KEY?: string;
  GoogleGenAI?: typeof GoogleGenAI;
  logger?: typeof defaultLogger;
}

async function initializeGeminiClient(options: GeminiClientOptions = {}): Promise<GoogleGenAI> {
  const GenAIConstructor = options.GoogleGenAI ?? GoogleGenAI;
  const log = options.logger ?? defaultLogger;

  if (options.GEMINI_API_KEY) {
    log.debug('[GeminiImageGen] Using Gemini API with GEMINI_API_KEY');
    return new GenAIConstructor({ apiKey: options.GEMINI_API_KEY });
  }

  if (options.GOOGLE_KEY) {
    log.debug('[GeminiImageGen] Using Gemini API with GOOGLE_KEY');
    return new GenAIConstructor({ apiKey: options.GOOGLE_KEY });
  }

  log.debug('[GeminiImageGen] Using Vertex AI with service account');
  const credentialsPath = getDefaultServiceKeyPath();
  const serviceKey = await loadServiceKey(credentialsPath);

  if (!serviceKey || !serviceKey.project_id) {
    throw new Error(
      'Gemini Image Generation requires one of: user-provided API key, GEMINI_API_KEY or GOOGLE_KEY env var, or a valid Google service account. ' +
        `Service account file not found or invalid at: ${credentialsPath}`,
    );
  }

  return new GenAIConstructor({
    vertexai: true,
    project: serviceKey.project_id,
    location: process.env.GOOGLE_CLOUD_LOCATION || process.env.GOOGLE_LOC || 'global',
    googleAuthOptions: { credentials: serviceKey },
  });
}

interface GeminiCandidateResponse {
  candidates?: Array<{
    finishReason?: string;
    safetyRatings?: Array<{
      probability?: string;
      blocked?: boolean;
      category?: string;
    }>;
    content?: {
      parts?: Array<{
        inlineData?: {
          data?: string;
          mimeType?: string;
        };
      }>;
    };
  }>;
  usageMetadata?: {
    prompt_token_count?: number;
    promptTokenCount?: number;
    candidates_token_count?: number;
    candidatesTokenCount?: number;
  };
}

function checkForSafetyBlock(
  response?: GeminiCandidateResponse | null,
): { reason: string; message: string; category?: string } | null {
  if (!response?.candidates?.length) {
    return { reason: 'NO_CANDIDATES', message: 'No candidates returned' };
  }

  const candidate = response.candidates[0];
  const finishReason = candidate.finishReason;

  if (finishReason === 'SAFETY' || finishReason === 'PROHIBITED_CONTENT') {
    return { reason: finishReason, message: 'Content blocked by safety filters' };
  }

  if (finishReason === 'RECITATION') {
    return { reason: finishReason, message: 'Content blocked due to recitation concerns' };
  }

  if (candidate.safetyRatings) {
    for (const rating of candidate.safetyRatings) {
      if (rating.probability === 'HIGH' || rating.blocked === true) {
        return {
          reason: 'SAFETY_RATING',
          message: `Blocked due to ${rating.category}`,
          category: rating.category,
        };
      }
    }
  }

  return null;
}

export interface CreateGeminiImageToolFields {
  override?: boolean;
  isAgent?: boolean;
  req?: GeminiReferenceRequestContext;
  imageFiles?: GeminiReferenceFileRecord[];
  userId?: string;
  fileStrategy?: string;
  imageOutputType?: string;
  GEMINI_API_KEY?: string;
  GOOGLE_KEY?: string;
  GoogleGenAI?: typeof GoogleGenAI;
  tool?: <TInput, TResult>(
    fn: (input: TInput, config?: Record<string, unknown>) => Promise<TResult>,
    fields: Record<string, unknown>,
  ) => unknown;
  logger?: typeof defaultLogger;
  spendTokens?: (
    txData: Record<string, unknown>,
    tokenUsage: { promptTokens: number; completionTokens: number },
  ) => Promise<unknown>;
  getFiles?: ResolveGeminiReferenceImagesParams['getFiles'];
  createFile?: ResolveGeminiReferenceImagesParams['createFile'];
  saveBase64Image?: ResolveGeminiReferenceImagesParams['saveBase64Image'];
  getStrategyFunctions?: ResolveGeminiReferenceImagesParams['getStrategyFunctions'];
}

// 创建 Gemini 图像生成工具并在返回 generated_image_id 前完成图像落盘校验，任何落盘失败均以安全错误码失败关闭
export function createGeminiImageTool(fields: CreateGeminiImageToolFields = {}): unknown {
  const override = fields.override ?? false;
  if (!override && !fields.isAgent) {
    throw new Error('This tool is only available for agents.');
  }

  const {
    req,
    imageFiles = [],
    userId,
    fileStrategy,
    GEMINI_API_KEY,
    GOOGLE_KEY,
    GoogleGenAI: GenAIConstructor,
    tool: toolFactory,
    logger = defaultLogger,
    spendTokens,
    getFiles,
    createFile,
    saveBase64Image,
    getStrategyFunctions,
  } = fields;

  if (typeof toolFactory !== 'function') {
    throw new Error('Missing required tool factory dependency.');
  }

  const imageOutputType = fields.imageOutputType || EImageOutputType.PNG;

  return toolFactory(
    async (
      {
        prompt,
        image_ids,
        aspectRatio,
        imageSize,
      }: {
        prompt?: string;
        image_ids?: string[];
        aspectRatio?: string;
        imageSize?: string;
      },
      runnableConfig?: {
        signal?: AbortSignal;
        configurable?: {
          thread_id?: string;
          run_id?: string;
          requestBody?: { messageId?: string };
        };
      },
    ) => {
      if (!prompt) {
        throw new Error('Missing required field: prompt');
      }

      logger.debug('[GeminiImageGen] Generating image', { aspectRatio, imageSize });

      let ai: GoogleGenAI;
      try {
        ai = await initializeGeminiClient({
          GEMINI_API_KEY,
          GOOGLE_KEY,
          GoogleGenAI: GenAIConstructor,
          logger,
        });
      } catch (error) {
        logger.error('[GeminiImageGen] Failed to initialize client:', error);
        return [
          [
            {
              type: ContentTypes.TEXT,
              text: 'Failed to initialize Gemini image service (code: GEMINI_INIT_FAILED).',
            },
          ],
          { content: [], file_ids: [] },
        ];
      }

      const contents: Array<{ text: string } | GeminiInlineImagePart> = [
        { text: replaceUnwantedChars(prompt) },
      ];

      if (image_ids && image_ids.length > 0) {
        const referenceResult = await resolveGeminiReferenceImages({
          imageFiles,
          image_ids,
          req,
          userId,
          fileStrategy,
          getFiles,
          createFile,
          saveBase64Image,
          getStrategyFunctions,
        });
        if (!referenceResult.ok) {
          logger.warn('[GeminiImageGen] Failed to resolve reference image IDs:', {
            code: referenceResult.error.code,
            unresolvedIds: referenceResult.error.unresolvedIds,
          });
          return [
            [{ type: ContentTypes.TEXT, text: referenceResult.error.message }],
            { content: [], file_ids: [] },
          ];
        }
        contents.push(...referenceResult.value);
        logger.debug('[GeminiImageGen] Added', referenceResult.value.length, 'context images');
      }

      let apiResponse: GeminiCandidateResponse;
      const geminiModel = process.env.GEMINI_IMAGE_MODEL || 'gemini-2.5-flash-image';
      const config: {
        responseModalities: string[];
        imageConfig?: { aspectRatio?: string; imageSize?: string };
        abortSignal?: AbortSignal;
      } = {
        responseModalities: ['TEXT', 'IMAGE'],
      };

      const supportsImageSize = !geminiModel.includes('gemini-2.5-flash-image');
      if (aspectRatio || (imageSize && supportsImageSize)) {
        config.imageConfig = {};
        if (aspectRatio) {
          config.imageConfig.aspectRatio = aspectRatio;
        }
        if (imageSize && supportsImageSize) {
          config.imageConfig.imageSize = imageSize;
        }
      }

      let derivedSignal: AbortSignal | null = null;
      let abortHandler: (() => void) | null = null;

      if (runnableConfig?.signal) {
        derivedSignal = AbortSignal.any([runnableConfig.signal]);
        abortHandler = () => logger.debug('[GeminiImageGen] Image generation aborted');
        derivedSignal.addEventListener('abort', abortHandler, { once: true });
        config.abortSignal = derivedSignal;
      }

      try {
        apiResponse = (await ai.models.generateContent({
          model: geminiModel,
          contents,
          config,
        })) as GeminiCandidateResponse;
      } catch (error) {
        logger.error('[GeminiImageGen] API error:', error);
        return [
          [
            {
              type: ContentTypes.TEXT,
              text: 'Image generation failed due to an upstream provider error (code: GEMINI_GENERATION_FAILED).',
            },
          ],
          { content: [], file_ids: [] },
        ];
      } finally {
        if (abortHandler && derivedSignal) {
          derivedSignal.removeEventListener('abort', abortHandler);
        }
      }

      const safetyBlock = checkForSafetyBlock(apiResponse);
      if (safetyBlock) {
        logger.warn('[GeminiImageGen] Safety block:', safetyBlock);
        const errorMsg = 'Image blocked by content safety filters. Please try different content.';
        return [[{ type: ContentTypes.TEXT, text: errorMsg }], { content: [], file_ids: [] }];
      }

      const rawImageData = apiResponse.candidates?.[0]?.content?.parts?.find((p) => p.inlineData)
        ?.inlineData?.data;

      if (!rawImageData) {
        logger.warn('[GeminiImageGen] No image data in response');
        return [
          [{ type: ContentTypes.TEXT, text: 'No image was generated. Please try again.' }],
          { content: [], file_ids: [] },
        ];
      }

      const rawBuffer = Buffer.from(rawImageData, 'base64');
      const { buffer: convertedBuffer, format: outputFormat } = await convertImageFormat(
        rawBuffer,
        imageOutputType,
      );
      const imageData = convertedBuffer.toString('base64');
      const mimeType = outputFormat === 'jpeg' ? 'image/jpeg' : `image/${outputFormat}`;
      const dataUrl = `data:${mimeType};base64,${imageData}`;

      const generatedFileId = crypto.randomUUID();
      const effectiveUserId = String(userId || req?.user?.id || '').trim();
      const canPersistViaBase64 = typeof saveBase64Image === 'function';
      const canPersistViaStrategy =
        Boolean(effectiveUserId) &&
        typeof createFile === 'function' &&
        typeof getStrategyFunctions === 'function';

      let persistedFile: GeminiReferenceFileRecord | null | undefined;
      if (canPersistViaBase64 || canPersistViaStrategy) {
        try {
          if (canPersistViaBase64 && saveBase64Image) {
            persistedFile = await saveBase64Image(dataUrl, {
              req,
              file_id: generatedFileId,
              filename: `gemini_image_gen_img_${nanoid()}`,
              endpoint: 'google',
              context: FileContext.image_generation,
            });
          } else if (canPersistViaStrategy && createFile && getStrategyFunctions) {
            const source = fileStrategy || 'local';
            const { saveBuffer } = getStrategyFunctions(source);
            if (typeof saveBuffer === 'function') {
              const ext = outputFormat === 'jpeg' ? 'jpg' : outputFormat;
              const fullFilename = `${generatedFileId}-gemini_image_gen_img_${nanoid()}.${ext}`;
              const { width, height } = await readImageDimensions(convertedBuffer);
              const filepath = await saveBuffer({
                userId: effectiveUserId,
                fileName: fullFilename,
                buffer: convertedBuffer,
                tenantId: req?.user?.tenantId,
              });
              if (filepath) {
                persistedFile = await createFile(
                  {
                    type: mimeType,
                    source,
                    context: FileContext.image_generation,
                    file_id: generatedFileId,
                    filepath,
                    filename: fullFilename,
                    user: effectiveUserId,
                    bytes: convertedBuffer.length,
                    width,
                    height,
                    tenantId: req?.user?.tenantId,
                  },
                  true,
                );
              }
            }
          }
        } catch (error) {
          logger.error('[GeminiImageGen] Failed to persist generated image:', error);
          persistedFile = null;
        }

        if (!persistedFile || !persistedFile.file_id || !persistedFile.filepath) {
          return [
            [{ type: ContentTypes.TEXT, text: PERSISTENCE_ERROR_TEXT }],
            { content: [], file_ids: [] },
          ];
        }
      }

      const finalFileId = persistedFile?.file_id || generatedFileId;
      const file_ids = [finalFileId];
      const content = [
        {
          type: ContentTypes.IMAGE_URL,
          image_url: { url: dataUrl },
        },
      ];

      const textResponse = [
        {
          type: ContentTypes.TEXT,
          text:
            GEMINI_DISPLAY_MESSAGE +
            `\n\ngenerated_image_id: "${finalFileId}"` +
            (image_ids && image_ids.length > 0
              ? `\nreferenced_image_ids: ["${image_ids.join('", "')}"]`
              : ''),
        },
      ];

      const conversationId = runnableConfig?.configurable?.thread_id;
      const messageId =
        runnableConfig?.configurable?.run_id ??
        runnableConfig?.configurable?.requestBody?.messageId;

      if (apiResponse.usageMetadata && typeof spendTokens === 'function') {
        const appConfig = req?.config as AppConfig | undefined;
        const balance = getBalanceConfig(appConfig);
        const transactions = getTransactionsConfig(appConfig);
        if (balance?.enabled || transactions?.enabled !== false) {
          const promptTokens =
            apiResponse.usageMetadata.prompt_token_count ||
            apiResponse.usageMetadata.promptTokenCount ||
            0;
          const completionTokens =
            apiResponse.usageMetadata.candidates_token_count ||
            apiResponse.usageMetadata.candidatesTokenCount ||
            0;
          if (promptTokens > 0 || completionTokens > 0) {
            spendTokens(
              {
                user: userId,
                model: geminiModel,
                messageId,
                conversationId,
                context: 'image_generation',
                balance,
                transactions,
              },
              {
                promptTokens,
                completionTokens,
              },
            ).catch((error) => {
              logger.error('[GeminiImageGen] Failed to record token usage:', error);
            });
          }
        }
      }

      return [
        textResponse,
        persistedFile ? { content, file_ids, files: [persistedFile] } : { content, file_ids },
      ];
    },
    {
      ...geminiToolkit.gemini_image_gen,
      responseFormat: 'content_and_artifact',
    },
  );
}
