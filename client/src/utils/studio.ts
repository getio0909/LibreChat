import { atom, createStore } from 'jotai';
import type { UIResource } from 'librechat-data-provider';

const UI_DRAFT_STORAGE_KEY = 'zyb_studio_ui_drafts_v2';

const STUDIO_RESOURCE_PREFIX = 'ui://zyb_studio/';
const STUDIO_SELECTION_PREFIX = 'ui://zyb_studio/selection/';
const STUDIO_COMPARE_PREFIX = 'ui://zyb_studio/compare/';
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
  resource?: UIResource | null;
  store?: ReturnType<typeof createStore>;
  renewFetcher?: typeof fetch;
}

export const studioUIStore = createStore();
export const studioSelectionDraftsAtom = atom<Record<string, unknown>>({});
export const studioRenewedLinksAtom = atom<Record<string, StudioRenewedResourceLinks>>({});

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

export function buildStudioDraftKey(
  context: StudioUIActionContext,
  explicitCapability?: string,
): string {
  const userId = typeof context.userId === 'string' ? context.userId.trim() : '';
  const resource = context.resource;
  const imageId = extractStudioSelectionImageId(resource?.uri);
  if (!userId || !resource || !imageId) {
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
  if (!userId || !uri.startsWith(STUDIO_RESOURCE_PREFIX)) {
    return '';
  }
  const conversationScope =
    typeof context.conversationId === 'string' && context.conversationId.trim()
      ? context.conversationId.trim()
      : 'active';
  const resourceScope =
    typeof resource?.resourceId === 'string' && resource.resourceId.trim()
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

export function upgradePersistedStudioHtml(
  html: string,
  uri?: string | null,
  renewedLinks?: StudioRenewedResourceLinks,
): string {
  if (
    typeof html !== 'string' ||
    !html ||
    typeof uri !== 'string' ||
    !uri.startsWith(STUDIO_RESOURCE_PREFIX)
  ) {
    return html;
  }
  let next = html;

  // 将旧版持久化卡片中的原始UUID/sel_/img_标题与UTC ISO时间戳升级为中文友好标题与本地时间
  if (uri.startsWith(STUDIO_SELECTION_PREFIX)) {
    next = next.replace(
      /<span class="title">只改局部\s*·\s*选区编辑器（[^（）]*?(?:[0-9a-f]{8}-[0-9a-f]{4}|img_[0-9a-f]+)[^（）]*?·\s*(\d+×\d+)）<\/span>/gi,
      '<span class="title">只改局部 · 选区编辑器（$1）</span>',
    );
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
  if (!userId || !resource || !resourceUri.startsWith(STUDIO_RESOURCE_PREFIX)) {
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
  } else if (resourceUri.startsWith(STUDIO_MODEL3D_PREFIX)) {
    const embeddedModelUrl = extractConstString(html, 'MODEL_URL');
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
        model_url: extractConstString(html, 'MODEL_URL') || payload.modelUrl,
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

    const candidateModelUrl = data.modelUrl ?? data.model_url;
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
