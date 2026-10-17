import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { Constants, FileContext } from 'librechat-data-provider';

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

export type ResolveGeminiReferencesResult =
  | {
      ok: true;
      value: GeminiInlineImagePart[];
    }
  | {
      ok: false;
      error: {
        code: 'UNRESOLVED_IMAGE_REFERENCES';
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
    mcpServers?: Record<
      string,
      | {
          url?: string;
          headers?: Record<string, string>;
        }
      | undefined
    >;
  };
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

// 校验候选文件路径不包含符号链接且严格位于允许根目录内
function readSafeFileUnderRoot(filePath: string, rootDir: string): Buffer | null {
  try {
    const rootStat = fs.lstatSync(rootDir);
    if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
      return null;
    }
    const resolvedRoot = fs.realpathSync(rootDir);
    const fileStat = fs.lstatSync(filePath);
    if (!fileStat.isFile() || fileStat.isSymbolicLink()) {
      return null;
    }
    let current = path.dirname(filePath);
    while (current !== rootDir && current !== path.dirname(current)) {
      const dirStat = fs.lstatSync(current);
      if (dirStat.isSymbolicLink()) {
        return null;
      }
      current = path.dirname(current);
    }
    const resolvedFile = fs.realpathSync(filePath);
    const rel = path.relative(resolvedRoot, resolvedFile);
    if (!rel || rel.startsWith('..') || path.isAbsolute(rel)) {
      return null;
    }
    return fs.readFileSync(resolvedFile);
  } catch {
    return null;
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

// 按当前用户权限加载 kb_ 或 img_ 原始图像字节（优先本地安全挂载目录，回退至内部认证的 Studio 文件接口）
async function loadStudioReferenceBytes({
  refId,
  userId,
  req,
  kbRoot,
  dataDir,
  settingsFile,
  fetchImpl = globalThis.fetch,
}: {
  refId: string;
  userId: string;
  req?: GeminiReferenceRequestContext;
  kbRoot?: string;
  dataDir?: string;
  settingsFile?: string;
  fetchImpl?: typeof fetch;
}): Promise<{ buffer: Buffer; mimeType: string } | null> {
  const normalizedId = refId.trim().toLowerCase();
  const normalizedUserId = userId.trim().toLowerCase();
  if (!STUDIO_USER_ID_RE.test(normalizedUserId)) {
    return null;
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
        recordAllowed = readSafeFileUnderRoot(recordPath, effectiveKbRoot) !== null;
      }
    } catch {
      recordAllowed = false;
    }
    if (recordAllowed) {
      const refPath = path.join(effectiveKbRoot, 'assets', 'ref2048', `${assetHex}.jpg`);
      const buf = readSafeFileUnderRoot(refPath, effectiveKbRoot);
      if (buf && buf.length > 0) {
        return { buffer: buf, mimeType: 'image/jpeg' };
      }
    }
  } else if (STUDIO_IMG_ID_RE.test(normalizedId)) {
    const userRoot = path.join(effectiveDataDir, 'users', normalizedUserId);
    const jobDir = path.join(userRoot, 'jobs', normalizedId);
    const receiptBuf = readSafeFileUnderRoot(path.join(jobDir, 'receipt.json'), userRoot);
    if (receiptBuf) {
      try {
        const receipt = JSON.parse(receiptBuf.toString('utf8')) as Record<string, unknown>;
        const fileField = typeof receipt.file === 'string' ? receipt.file : '';
        if (receipt.state === 'saved' && ALLOWED_STUDIO_ORIGINAL_FILES.has(fileField)) {
          const origBuf = readSafeFileUnderRoot(path.join(jobDir, fileField), userRoot);
          if (origBuf && origBuf.length > 0) {
            let mimeType = 'image/jpeg';
            if (fileField === 'original.png') {
              mimeType = 'image/png';
            } else if (fileField === 'original.webp') {
              mimeType = 'image/webp';
            }
            return { buffer: origBuf, mimeType };
          }
        }
      } catch {
        // receipt 解析失败时继续尝试远端或最终按失败关闭处理
      }
    }
  } else {
    return null;
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
    return null;
  }

  const baseStudioUrl = rawMcpUrl.replace(/\/mcp\/?$/, '').replace(/\/+$/, '');
  if (!baseStudioUrl) {
    return null;
  }

  try {
    const response = await fetchImpl(`${baseStudioUrl}/files/${encodeURIComponent(normalizedId)}`, {
      method: 'GET',
      headers: {
        'X-Studio-Token': sharedToken,
        'X-LibreChat-User-ID': normalizedUserId,
      },
    });
    if (!response.ok) {
      return null;
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
    const arrayBuf = await response.arrayBuffer();
    const buffer = Buffer.from(arrayBuf);
    if (buffer.length === 0) {
      return null;
    }
    return { buffer, mimeType };
  } catch {
    return null;
  }
}

// 将 gemini_image_gen 请求的 image_ids（含 kb_ 与 img_ 引用）解析并桥接为当前用户的授权 LibreChat File 记录；若任一显式引用无法解析则失败关闭
export async function resolveGeminiReferenceImages({
  imageFiles = [],
  image_ids = [],
  req,
  userId,
  fileStrategy,
  kbRoot,
  dataDir,
  settingsFile,
  getFiles,
  createFile,
  saveBase64Image,
  getStrategyFunctions,
  fetchImpl,
}: ResolveGeminiReferenceImagesParams): Promise<ResolveGeminiReferencesResult> {
  if (!image_ids || image_ids.length === 0) {
    return { ok: true, value: [] };
  }

  const effectiveUserId = String(userId || req?.user?.id || '').trim();
  const requestFilesMap = new Map<string, GeminiReferenceFileRecord>();
  for (const file of imageFiles) {
    if (file?.file_id) {
      requestFilesMap.set(file.file_id, { ...file });
    }
  }

  const orderedFiles: Array<GeminiReferenceFileRecord | undefined> = new Array(image_ids.length);
  const idsToFetch = new Set<string>();
  const missingIndices: number[] = [];

  for (let i = 0; i < image_ids.length; i++) {
    const rawId = image_ids[i];
    const id = typeof rawId === 'string' ? rawId.trim() : '';
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
      const id = String(image_ids[idx]).trim();
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

  // 对尚未存在 File 记录的 kb_ 与 img_ 引用，按当前用户权限加载原图并复用 LibreChat 文件存储与 createFile 创建正式 File 记录
  const bridgedCache = new Map<string, GeminiReferenceFileRecord>();
  for (let i = 0; i < image_ids.length; i++) {
    if (orderedFiles[i]) {
      continue;
    }
    const rawId = image_ids[i];
    const id = typeof rawId === 'string' ? rawId.trim() : '';
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
      fetchImpl,
    });
    if (!loaded) {
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
      if (canSaveViaBase64 && saveBase64Image) {
        const dataUrl = `data:${loaded.mimeType};base64,${loaded.buffer.toString('base64')}`;
        savedFile = await saveBase64Image(dataUrl, {
          req: effectiveReq,
          file_id: bridgedFileId,
          filename: baseFilename,
          endpoint: 'agents',
          context: FileContext.image_generation,
        });
      } else if (canSaveViaStrategy && createFile && getStrategyFunctions) {
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
                width: 1024,
                height: 1024,
                tenantId: req?.user?.tenantId,
              },
              true,
            );
          }
        }
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

  for (let i = 0; i < image_ids.length; i++) {
    const requestedId = String(image_ids[i] ?? '').trim() || `<empty_at_${i}>`;
    const imageFile = orderedFiles[i];
    if (!imageFile) {
      unresolvedIds.push(requestedId);
      continue;
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
      for await (const chunk of stream) {
        chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
      }
      const buffer = Buffer.concat(chunks);
      if (buffer.length === 0) {
        unresolvedIds.push(requestedId);
        continue;
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
