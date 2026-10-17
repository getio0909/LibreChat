import { atom, createStore } from 'jotai';
import { ContentTypes, Tools, splitToolCallName } from 'librechat-data-provider';
import type {
  TAttachment,
  TMessage,
  TMessageContentParts,
  UIResource,
} from 'librechat-data-provider';

const UI_DRAFT_STORAGE_KEY = 'zyb_studio_ui_drafts_v2';

export const STUDIO_SERVER_NAME = 'zyb_studio';
const STUDIO_RESOURCE_PREFIX = 'ui://zyb_studio/';
const STUDIO_SELECTION_PREFIX = 'ui://zyb_studio/selection/';
const STUDIO_COMPARE_PREFIX = 'ui://zyb_studio/compare/';
const STUDIO_MODEL_PREFIX = 'ui://zyb_studio/model/';
const STUDIO_MODEL3D_PREFIX = 'ui://zyb_studio/model3d/';

const SIGNED_FILE_PATH_RE = /^\/(?:studio-)?files\/([A-Za-z0-9_-]+\.[0-9a-f]{64})$/;
const CONST_STRING_RE = (name: string) =>
  new RegExp(`const\\s+${name}\\s*=\\s*("(?:\\\\.|[^"\\\\])*")`);

export interface StudioRenewedResourceLinks {
  previewUrl?: string;
  editorUrl?: string;
  beforeUrl?: string;
  afterUrl?: string;
  modelUrl?: string;
  expiresAt?: string;
  previewExpiresAt?: string;
}

export interface StudioUIActionContext {
  userId?: string | null;
  conversationId?: string | null;
  serverName?: string | null;
  resource?: UIResource | null;
  store?: ReturnType<typeof createStore>;
  renewFetcher?: typeof fetch;
}

export interface StudioLegacyPromptContext {
  serverName?: string | null;
  message?: TMessage | null;
  messages?: TMessage[] | null;
}

export const studioUIStore = createStore();
export const studioSelectionDraftsAtom = atom<Record<string, unknown>>({});
export const studioRenewedLinksAtom = atom<Record<string, StudioRenewedResourceLinks>>({});

// 解析资源所属的MCP服务名称，优先采用宿主显式传入的服务身份
export function resolveStudioServerName(
  resource?: Pick<UIResource, 'serverName'> | null,
  explicitServerName?: string | null,
): string {
  if (typeof explicitServerName === 'string' && explicitServerName.trim()) {
    return explicitServerName.trim();
  }
  if (typeof resource?.serverName === 'string' && resource.serverName.trim()) {
    return resource.serverName.trim();
  }
  return '';
}

// 校验UIResource是否同时具备zyb_studio服务身份与ui://zyb_studio/资源URI前缀
export function isStudioUIResource(
  resource?: Pick<UIResource, 'uri' | 'serverName'> | null,
  explicitServerName?: string | null,
): boolean {
  if (!resource || typeof resource.uri !== 'string') {
    return false;
  }
  const uri = resource.uri.trim();
  if (!uri.startsWith(STUDIO_RESOURCE_PREFIX)) {
    return false;
  }
  return resolveStudioServerName(resource, explicitServerName) === STUDIO_SERVER_NAME;
}

function isStudioModelUri(uri: string): boolean {
  return uri.startsWith(STUDIO_MODEL_PREFIX) || uri.startsWith(STUDIO_MODEL3D_PREFIX);
}

function extractToolCallPartInfo(
  part: TMessageContentParts | undefined,
): { id: string; serverName: string } | null {
  if (!part || part.type !== ContentTypes.TOOL_CALL) {
    return null;
  }
  const toolCall = part[ContentTypes.TOOL_CALL];
  if (!toolCall) {
    return null;
  }
  let rawName = '';
  if ('name' in toolCall && typeof toolCall.name === 'string') {
    rawName = toolCall.name;
  } else if ('function' in toolCall && typeof toolCall.function?.name === 'string') {
    rawName = toolCall.function.name;
  }
  if (!rawName) {
    return null;
  }
  const [, serverName] = splitToolCallName(rawName, [STUDIO_SERVER_NAME]);
  return {
    id: typeof toolCall.id === 'string' ? toolCall.id : '',
    serverName: serverName ?? '',
  };
}

// 从会话消息的tool_call分片构建权威MCP服务身份索引，防止非Studio服务伪造serverName或URI
export function buildMessageToolServerLookup(messages?: TMessage[] | null): {
  byToolCallId: Map<string, string>;
  byMessageId: Map<string, string>;
} {
  const byToolCallId = new Map<string, string>();
  const byMessageId = new Map<string, string>();
  if (!Array.isArray(messages)) {
    return { byToolCallId, byMessageId };
  }
  for (const message of messages) {
    if (!message || !Array.isArray(message.content)) {
      continue;
    }
    const messageServers = new Set<string>();
    for (const part of message.content) {
      const info = extractToolCallPartInfo(part);
      if (!info) {
        continue;
      }
      messageServers.add(info.serverName);
      if (info.id) {
        byToolCallId.set(info.id, info.serverName);
      }
    }
    if (message.messageId && messageServers.size === 1) {
      const [singleServer] = Array.from(messageServers);
      byMessageId.set(message.messageId, singleServer);
    }
  }
  return { byToolCallId, byMessageId };
}

// 根据宿主消息中的权威tool_call记录为UIResource绑定或覆写serverName
export function stampVerifiedResourceServerName(
  resource: UIResource,
  attachment: TAttachment,
  lookup: { byToolCallId: Map<string, string>; byMessageId: Map<string, string> },
  fallbackMessageId?: string,
): UIResource {
  const toolCallId = typeof attachment.toolCallId === 'string' ? attachment.toolCallId : '';
  const messageId =
    (typeof attachment.messageId === 'string' && attachment.messageId) || fallbackMessageId || '';
  const authoritativeServer =
    (toolCallId ? lookup.byToolCallId.get(toolCallId) : undefined) ??
    (messageId ? lookup.byMessageId.get(messageId) : undefined);
  if (authoritativeServer === undefined) {
    return resource;
  }
  const normalizedServer = authoritativeServer || undefined;
  if (resource.serverName === normalizedServer) {
    return resource;
  }
  return {
    ...resource,
    serverName: normalizedServer,
  };
}

function readSessionDraftMap(): Record<string, unknown> {
  if (typeof window === 'undefined' || !window.sessionStorage) {
    return {};
  }
  try {
    const raw = window.sessionStorage.getItem(UI_DRAFT_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function writeSessionDraftMap(map: Record<string, unknown>): void {
  if (typeof window === 'undefined' || !window.sessionStorage) {
    return;
  }
  try {
    window.sessionStorage.setItem(UI_DRAFT_STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Ignore storage quota or private browsing errors
  }
}

function extractConstString(html: string | undefined, constName: string): string {
  if (typeof html !== 'string' || !html) {
    return '';
  }
  const match = CONST_STRING_RE(constName).exec(html);
  if (!match?.[1]) {
    return '';
  }
  try {
    const parsed: unknown = JSON.parse(match[1]);
    return typeof parsed === 'string' ? parsed : '';
  } catch {
    return '';
  }
}

export function extractTokenSignaturePrefix(urlStr: string | undefined | null): string {
  if (typeof urlStr !== 'string' || !urlStr) {
    return '';
  }
  const clean = urlStr.split('?')[0].split('#')[0];
  const tail = clean.split('/').pop() ?? '';
  const dotIdx = tail.lastIndexOf('.');
  if (dotIdx > 0 && dotIdx < tail.length - 1) {
    return tail.slice(dotIdx + 1, dotIdx + 25);
  }
  return '';
}

function decodeBase64UrlPrefix(tokenPart: string): string {
  try {
    const normalized = tokenPart.replace(/-/g, '+').replace(/_/g, '/');
    const pad = '='.repeat((4 - (normalized.length % 4)) % 4);
    if (typeof window !== 'undefined' && typeof window.atob === 'function') {
      return window.atob(normalized + pad);
    }
    return Buffer.from(normalized + pad, 'base64').toString('utf-8');
  } catch {
    return '';
  }
}

export function isValidStudioSignedUrl(
  candidateUrl: unknown,
  referenceUrl: string,
  expectedPrefix?: 'edt:' | 'img:' | 'model:',
): candidateUrl is string {
  if (typeof candidateUrl !== 'string' || !candidateUrl.trim() || !referenceUrl.trim()) {
    return false;
  }
  try {
    const baseOrigin =
      typeof window !== 'undefined' && window.location?.origin && window.location.origin !== 'null'
        ? window.location.origin
        : 'http://localhost';
    const refParsed = new URL(referenceUrl, baseOrigin);
    const candParsed = new URL(candidateUrl, refParsed.origin);
    if (candParsed.origin !== refParsed.origin) {
      return false;
    }
    const match = SIGNED_FILE_PATH_RE.exec(candParsed.pathname);
    if (!match?.[1]) {
      return false;
    }
    if (!expectedPrefix) {
      return true;
    }
    const payloadB64 = match[1].split('.')[0] ?? '';
    const decoded = decodeBase64UrlPrefix(payloadB64);
    if (expectedPrefix === 'model:') {
      return !decoded.startsWith('img:') && !decoded.startsWith('edt:');
    }
    return decoded.startsWith(expectedPrefix);
  } catch {
    return false;
  }
}

export function extractStudioEditorCapability(
  resource?: Pick<UIResource, 'resourceId' | 'text' | 'uri'> | null,
  explicitCapability?: string,
): string {
  const signedUrl = extractConstString(resource?.text, 'SIGNED_URL');
  const fromHtml = extractTokenSignaturePrefix(signedUrl);
  if (fromHtml) {
    return fromHtml;
  }
  if (typeof explicitCapability === 'string' && explicitCapability.trim()) {
    return explicitCapability.trim();
  }
  if (typeof resource?.resourceId === 'string' && resource.resourceId.trim()) {
    return resource.resourceId.trim();
  }
  return 'default';
}

export function extractStudioSelectionImageId(uri: string | undefined | null): string {
  if (typeof uri !== 'string' || !uri.startsWith(STUDIO_SELECTION_PREFIX)) {
    return '';
  }
  return uri.slice(STUDIO_SELECTION_PREFIX.length).split(/[?#]/)[0].trim();
}

function extractEmbeddedModelUrl(html: string | undefined): string {
  return extractConstString(html, 'INITIAL_MODEL_URL') || extractConstString(html, 'MODEL_URL');
}

export function buildStudioDraftKey(
  context: StudioUIActionContext,
  explicitCapability?: string,
): string {
  const userId = typeof context.userId === 'string' ? context.userId.trim() : '';
  const resource = context.resource;
  const imageId = extractStudioSelectionImageId(resource?.uri);
  if (!userId || !resource || !imageId || !isStudioUIResource(resource, context.serverName)) {
    return '';
  }
  const conversationScope =
    typeof context.conversationId === 'string' && context.conversationId.trim()
      ? context.conversationId.trim()
      : 'active';
  const resourceScope =
    typeof resource.resourceId === 'string' && resource.resourceId.trim()
      ? resource.resourceId.trim()
      : 'resource';
  const capabilityScope = extractStudioEditorCapability(resource, explicitCapability);
  return `zyb_studio:selection:${userId}:${conversationScope}:${imageId}:${resourceScope}:${capabilityScope}`;
}

export function buildStudioResourceKey(context: StudioUIActionContext): string {
  const userId = typeof context.userId === 'string' ? context.userId.trim() : '';
  const resource = context.resource;
  const uri = typeof resource?.uri === 'string' ? resource.uri.trim() : '';
  if (!userId || !resource || !isStudioUIResource(resource, context.serverName)) {
    return '';
  }
  const conversationScope =
    typeof context.conversationId === 'string' && context.conversationId.trim()
      ? context.conversationId.trim()
      : 'active';
  const resourceScope =
    typeof resource.resourceId === 'string' && resource.resourceId.trim()
      ? resource.resourceId.trim()
      : 'resource';
  const capabilityScope = extractStudioEditorCapability(resource);
  return `zyb_studio:resource:${userId}:${conversationScope}:${uri}:${resourceScope}:${capabilityScope}`;
}

export function saveUIDraft(
  draftKey: string,
  draft: unknown,
  store: ReturnType<typeof createStore> = studioUIStore,
): void {
  if (!draftKey) {
    return;
  }
  const current = { ...store.get(studioSelectionDraftsAtom) };
  if (draft == null) {
    delete current[draftKey];
  } else {
    current[draftKey] = draft;
  }
  store.set(studioSelectionDraftsAtom, current);

  const sessionMap = readSessionDraftMap();
  if (draft == null) {
    delete sessionMap[draftKey];
  } else {
    sessionMap[draftKey] = draft;
  }
  writeSessionDraftMap(sessionMap);
}

export function loadUIDraft(
  draftKey: string,
  store: ReturnType<typeof createStore> = studioUIStore,
): unknown {
  if (!draftKey) {
    return undefined;
  }
  const current = store.get(studioSelectionDraftsAtom);
  if (Object.prototype.hasOwnProperty.call(current, draftKey)) {
    return current[draftKey];
  }
  const sessionMap = readSessionDraftMap();
  if (Object.prototype.hasOwnProperty.call(sessionMap, draftKey)) {
    const draft = sessionMap[draftKey];
    store.set(studioSelectionDraftsAtom, {
      ...current,
      [draftKey]: draft,
    });
    return draft;
  }
  return undefined;
}

export function saveStudioRenewedLinks(
  resourceKey: string,
  links: StudioRenewedResourceLinks,
  store: ReturnType<typeof createStore> = studioUIStore,
): void {
  if (!resourceKey) {
    return;
  }
  const current = store.get(studioRenewedLinksAtom);
  store.set(studioRenewedLinksAtom, {
    ...current,
    [resourceKey]: {
      ...(current[resourceKey] ?? {}),
      ...links,
    },
  });
}

export function loadStudioRenewedLinks(
  resourceKey: string,
  store: ReturnType<typeof createStore> = studioUIStore,
): StudioRenewedResourceLinks | undefined {
  if (!resourceKey) {
    return undefined;
  }
  return store.get(studioRenewedLinksAtom)[resourceKey];
}

export function resetStudioUIState(store: ReturnType<typeof createStore> = studioUIStore): void {
  store.set(studioSelectionDraftsAtom, {});
  store.set(studioRenewedLinksAtom, {});
  if (typeof window !== 'undefined' && window.sessionStorage) {
    try {
      window.sessionStorage.removeItem(UI_DRAFT_STORAGE_KEY);
    } catch {
      // Ignore storage errors
    }
  }
}

function formatLocalTimestampDisplay(isoText: string): string {
  const parsed = new Date(isoText);
  if (Number.isNaN(parsed.getTime())) {
    return isoText;
  }
  const pad2 = (n: number) => String(n).padStart(2, '0');
  return `${parsed.getFullYear()}-${pad2(parsed.getMonth() + 1)}-${pad2(parsed.getDate())} ${pad2(parsed.getHours())}:${pad2(parsed.getMinutes())}`;
}

// 匹配旧版MCP-UI自动包裹的英文提示模板及未标注语言的三重反引号围栏（曾导致被highlight.js误判为wasm代码块）
const LEGACY_UI_PROMPT_WRAPPER_RE =
  /^The user clicked a button in an embedded UI Resource,\s*and we got a message of type `prompt`\.[\s\S]*?```(?:[a-zA-Z0-9_-]*)\n([\s\S]*?)\n```[\s\S]*$/;

// 严格匹配智印工坊选区编辑器的确认选区消息载荷（sel_<hex> + 底图UUID或img_<hex>）
const STUDIO_CONFIRMED_SELECTION_PROMPT_RE =
  /^已确认选区\s+(sel_[0-9a-f]{16,64})（底图\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|img_[0-9a-f]{12,64})）(?:。|，请执行局部修改：[^\r\n]+)$/i;

function hasConfirmedStudioSelectionOrigin(
  imageId: string,
  context?: StudioLegacyPromptContext,
): boolean {
  if (!context) {
    return false;
  }
  if (typeof context.serverName === 'string' && context.serverName.trim() === STUDIO_SERVER_NAME) {
    return true;
  }
  if (!Array.isArray(context.messages) || context.messages.length === 0) {
    return false;
  }

  const lookup = buildMessageToolServerLookup(context.messages);
  const expectedUri = `${STUDIO_SELECTION_PREFIX}${imageId}`;
  let hasStudioOrigin = false;
  let hasSpoofedSelectionUri = false;

  for (const srv of lookup.byToolCallId.values()) {
    if (srv === STUDIO_SERVER_NAME) {
      hasStudioOrigin = true;
    }
  }
  for (const srv of lookup.byMessageId.values()) {
    if (srv === STUDIO_SERVER_NAME) {
      hasStudioOrigin = true;
    }
  }

  for (const msg of context.messages) {
    if (!msg || !Array.isArray(msg.attachments)) {
      continue;
    }
    for (const attachment of msg.attachments) {
      if (attachment?.type !== Tools.ui_resources) {
        continue;
      }
      const resources = attachment[Tools.ui_resources];
      if (!Array.isArray(resources)) {
        continue;
      }
      for (const rawResource of resources) {
        if (!rawResource) {
          continue;
        }
        const stamped = stampVerifiedResourceServerName(
          rawResource,
          attachment,
          lookup,
          msg.messageId,
        );
        if (stamped.uri === expectedUri) {
          if (stamped.serverName === STUDIO_SERVER_NAME) {
            hasStudioOrigin = true;
          } else {
            hasSpoofedSelectionUri = true;
          }
        }
      }
    }
  }

  const parentId = context.message?.parentMessageId;
  if (parentId && lookup.byMessageId.has(parentId)) {
    if (lookup.byMessageId.get(parentId) !== STUDIO_SERVER_NAME) {
      return false;
    }
  }

  return hasStudioOrigin && !hasSpoofedSelectionUri;
}

// 仅在确认属于智印工坊选区消息时，剥离历史落库的英文MCP-UI包装文本与代码块围栏
export function unwrapLegacyUIPromptText(
  text: string,
  context?: StudioLegacyPromptContext,
): string {
  if (typeof text !== 'string' || !text.includes('embedded UI Resource')) {
    return text;
  }
  const match = LEGACY_UI_PROMPT_WRAPPER_RE.exec(text.trim());
  const inner = match?.[1]?.trim();
  if (!inner) {
    return text;
  }
  const selectionMatch = STUDIO_CONFIRMED_SELECTION_PROMPT_RE.exec(inner);
  if (!selectionMatch?.[2]) {
    return text;
  }
  if (!hasConfirmedStudioSelectionOrigin(selectionMatch[2], context)) {
    return text;
  }
  return inner;
}

export function upgradePersistedStudioHtml(
  html: string,
  uri?: string | null,
  renewedLinks?: StudioRenewedResourceLinks,
  serverName?: string | null,
): string {
  if (
    typeof html !== 'string' ||
    !html ||
    typeof uri !== 'string' ||
    !isStudioUIResource({ uri, serverName: serverName ?? undefined }, serverName)
  ) {
    return html;
  }
  let next = html;

  // 将旧版持久化卡片中的原始UUID/sel_/img_标题与UTC ISO时间戳升级为中文友好标题与本地时间，并补齐移动端缩放高度通知
  if (uri.startsWith(STUDIO_SELECTION_PREFIX)) {
    next = next.replace(
      /<span class="title">只改局部\s*·\s*选区编辑器（[^（）]*?(?:[0-9a-f]{8}-[0-9a-f]{4}|img_[0-9a-f]+)[^（）]*?·\s*(\d+×\d+)）<\/span>/gi,
      '<span class="title">只改局部 · 选区编辑器（$1）</span>',
    );
    if (!next.includes('new ResizeObserver')) {
      next = next.replace(
        /window\.addEventListener\('resize',\s*syncStageLayout\);/g,
        "window.addEventListener('resize', function() { syncStageLayout(); notifyHostSize(); }); if (typeof ResizeObserver !== 'undefined' && root) { new ResizeObserver(function() { notifyHostSize(); }).observe(root); }",
      );
    }
  } else if (uri.startsWith(STUDIO_COMPARE_PREFIX)) {
    next = next.replace(
      /<span class="title">局部修改前后对比（[^（）]*?(?:sel_[0-9a-f]+|img_[0-9a-f]+|[0-9a-f]{8}-[0-9a-f]{4})[^（）]*?·\s*(\d+×\d+)(?:\s*·\s*变更像素\s*[^（）]+)?）<\/span>/gi,
      '<span class="title">局部修改前后对比（$1）</span>',
    );
  }

  next = next.replace(
    /(链接有效期至\s*)(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)/g,
    (_full, prefix: string, iso: string) => `${prefix}${formatLocalTimestampDisplay(iso)}`,
  );

  if (!renewedLinks) {
    return next;
  }

  if (renewedLinks.editorUrl) {
    const safeEditorJson = JSON.stringify(renewedLinks.editorUrl);
    next = next.replace(
      /const\s+SIGNED_URL\s*=\s*"(?:\\.|[^"\\])*";/,
      `const SIGNED_URL = ${safeEditorJson};`,
    );
  }
  if (renewedLinks.previewUrl) {
    const safePreviewJson = JSON.stringify(renewedLinks.previewUrl);
    next = next.replace(
      /const\s+PREVIEW_URL\s*=\s*"(?:\\.|[^"\\])*";/,
      `const PREVIEW_URL = ${safePreviewJson};`,
    );
    next = next.replace(
      /(<img\s+id="baseImg"\s+src=")[^"]*(")/,
      `$1${renewedLinks.previewUrl.replace(/"/g, '&quot;')}$2`,
    );
  }
  if (renewedLinks.beforeUrl) {
    const safeBeforeJson = JSON.stringify(renewedLinks.beforeUrl);
    next = next.replace(
      /const\s+BEFORE_URL\s*=\s*"(?:\\.|[^"\\])*";/,
      `const BEFORE_URL = ${safeBeforeJson};`,
    );
  }
  if (renewedLinks.afterUrl) {
    const safeAfterJson = JSON.stringify(renewedLinks.afterUrl);
    next = next.replace(
      /const\s+AFTER_URL\s*=\s*"(?:\\.|[^"\\])*";/,
      `const AFTER_URL = ${safeAfterJson};`,
    );
  }
  if (renewedLinks.modelUrl) {
    const safeModelJson = JSON.stringify(renewedLinks.modelUrl);
    next = next.replace(
      /const\s+INITIAL_MODEL_URL\s*=\s*"(?:\\.|[^"\\])*";/,
      `const INITIAL_MODEL_URL = ${safeModelJson};`,
    );
    next = next.replace(
      /const\s+MODEL_URL\s*=\s*"(?:\\.|[^"\\])*";/,
      `const MODEL_URL = ${safeModelJson};`,
    );
  }
  if (renewedLinks.expiresAt) {
    const safeExpiresJson = JSON.stringify(renewedLinks.expiresAt);
    next = next.replace(
      /const\s+EXPIRES_AT\s*=\s*"(?:\\.|[^"\\])*";/,
      `const EXPIRES_AT = ${safeExpiresJson};`,
    );
    next = next.replace(
      /(id="model-expires-badge"\s+data-expires=")[^"]*(")/,
      `$1${renewedLinks.expiresAt.replace(/"/g, '&quot;')}$2`,
    );
  }
  if (renewedLinks.previewExpiresAt) {
    const safePreviewExpiresJson = JSON.stringify(renewedLinks.previewExpiresAt);
    next = next.replace(
      /const\s+PREVIEW_EXPIRES_AT\s*=\s*"(?:\\.|[^"\\])*";/,
      `const PREVIEW_EXPIRES_AT = ${safePreviewExpiresJson};`,
    );
  }

  return next;
}

export async function renewStudioResourceLinks(
  payload: Record<string, unknown>,
  context: StudioUIActionContext,
): Promise<
  | ({
      status: 'renewed';
      resourceUri: string;
      sessionNonce?: string;
      editorCapability?: string;
    } & StudioRenewedResourceLinks)
  | { status: 'rejected'; reason: string }
> {
  const userId = typeof context.userId === 'string' ? context.userId.trim() : '';
  const resource = context.resource;
  const resourceUri = typeof resource?.uri === 'string' ? resource.uri.trim() : '';
  if (!userId || !resource || !isStudioUIResource(resource, context.serverName)) {
    return { status: 'rejected', reason: 'unauthenticated_or_invalid_resource' };
  }
  if (
    typeof payload.resourceUri === 'string' &&
    payload.resourceUri.trim() &&
    payload.resourceUri.trim() !== resourceUri
  ) {
    return { status: 'rejected', reason: 'resource_mismatch' };
  }

  const expectedCapability = extractStudioEditorCapability(resource);
  if (
    resourceUri.startsWith(STUDIO_SELECTION_PREFIX) &&
    typeof payload.editorCapability === 'string' &&
    payload.editorCapability.trim() &&
    expectedCapability !== 'default' &&
    payload.editorCapability.trim() !== expectedCapability
  ) {
    return { status: 'rejected', reason: 'capability_mismatch' };
  }

  const html = resource.text ?? '';
  let endpointUrl = '';
  let referencePreviewUrl = '';
  if (resourceUri.startsWith(STUDIO_SELECTION_PREFIX)) {
    const embeddedEditorUrl = extractConstString(html, 'SIGNED_URL');
    const embeddedPreviewUrl = extractConstString(html, 'PREVIEW_URL') || embeddedEditorUrl;
    referencePreviewUrl = embeddedPreviewUrl;
    endpointUrl = isValidStudioSignedUrl(payload.editorUrl, embeddedEditorUrl, 'edt:')
      ? payload.editorUrl
      : embeddedEditorUrl;
  } else if (resourceUri.startsWith(STUDIO_COMPARE_PREFIX)) {
    const embeddedAfterUrl = extractConstString(html, 'AFTER_URL');
    const embeddedBeforeUrl = extractConstString(html, 'BEFORE_URL');
    referencePreviewUrl = embeddedAfterUrl || embeddedBeforeUrl;
    endpointUrl = isValidStudioSignedUrl(payload.afterUrl, embeddedAfterUrl, 'img:')
      ? payload.afterUrl
      : embeddedAfterUrl;
  } else if (isStudioModelUri(resourceUri)) {
    const embeddedModelUrl = extractEmbeddedModelUrl(html);
    referencePreviewUrl = embeddedModelUrl;
    endpointUrl = isValidStudioSignedUrl(payload.modelUrl, embeddedModelUrl, 'model:')
      ? payload.modelUrl
      : embeddedModelUrl;
  }

  if (!endpointUrl) {
    return { status: 'rejected', reason: 'missing_signed_endpoint' };
  }

  const fetcher = context.renewFetcher ?? (typeof fetch === 'function' ? fetch : undefined);
  if (!fetcher) {
    return { status: 'rejected', reason: 'fetch_unavailable' };
  }

  try {
    const response = await fetcher(endpointUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'renew',
        resource_uri: resourceUri,
        image_id: payload.imageId,
        model_id: payload.modelId,
        editor_url: endpointUrl,
        preview_url: referencePreviewUrl || payload.previewUrl,
        before_url: extractConstString(html, 'BEFORE_URL') || payload.beforeUrl,
        after_url: extractConstString(html, 'AFTER_URL') || payload.afterUrl,
        model_url: extractEmbeddedModelUrl(html) || payload.modelUrl,
      }),
    });
    if (!response.ok) {
      return { status: 'rejected', reason: `http_${response.status}` };
    }
    const rawText = await response.text();
    const parsed: unknown = rawText ? JSON.parse(rawText) : null;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { status: 'rejected', reason: 'invalid_json' };
    }
    const data = parsed as Record<string, unknown>;
    const renewed: StudioRenewedResourceLinks = {};

    const candidateEditorUrl =
      data.editorUrl ?? data.editor_url ?? data.signedUrl ?? data.signed_url;
    if (isValidStudioSignedUrl(candidateEditorUrl, endpointUrl, 'edt:')) {
      renewed.editorUrl = candidateEditorUrl;
    }

    const candidatePreviewUrl = data.previewUrl ?? data.preview_url;
    if (
      referencePreviewUrl &&
      isValidStudioSignedUrl(candidatePreviewUrl, referencePreviewUrl, 'img:')
    ) {
      renewed.previewUrl = candidatePreviewUrl;
    }

    const candidateBeforeUrl = data.beforeUrl ?? data.before_url;
    if (
      referencePreviewUrl &&
      isValidStudioSignedUrl(candidateBeforeUrl, referencePreviewUrl, 'img:')
    ) {
      renewed.beforeUrl = candidateBeforeUrl;
    }

    const candidateAfterUrl = data.afterUrl ?? data.after_url;
    if (
      referencePreviewUrl &&
      isValidStudioSignedUrl(candidateAfterUrl, referencePreviewUrl, 'img:')
    ) {
      renewed.afterUrl = candidateAfterUrl;
    }

    const candidateModelUrl =
      data.modelUrl ??
      data.model_url ??
      (isStudioModelUri(resourceUri) ? (data.signedUrl ?? data.signed_url) : undefined);
    if (
      referencePreviewUrl &&
      isValidStudioSignedUrl(candidateModelUrl, referencePreviewUrl, 'model:')
    ) {
      renewed.modelUrl = candidateModelUrl;
    }

    if (
      !renewed.editorUrl &&
      !renewed.previewUrl &&
      !renewed.beforeUrl &&
      !renewed.afterUrl &&
      !renewed.modelUrl
    ) {
      return { status: 'rejected', reason: 'no_valid_renewed_links' };
    }

    const expiresAt = data.expiresAt ?? data.expires_at;
    if (typeof expiresAt === 'string' && expiresAt) {
      renewed.expiresAt = expiresAt;
    }
    const previewExpiresAt = data.previewExpiresAt ?? data.preview_expires_at;
    if (typeof previewExpiresAt === 'string' && previewExpiresAt) {
      renewed.previewExpiresAt = previewExpiresAt;
    }

    const resourceKey = buildStudioResourceKey(context);
    saveStudioRenewedLinks(resourceKey, renewed, context.store ?? studioUIStore);

    return {
      status: 'renewed',
      resourceUri,
      sessionNonce: typeof payload.sessionNonce === 'string' ? payload.sessionNonce : undefined,
      editorCapability: expectedCapability,
      ...renewed,
    };
  } catch {
    return { status: 'rejected', reason: 'network_error' };
  }
}
