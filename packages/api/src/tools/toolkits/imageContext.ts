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
