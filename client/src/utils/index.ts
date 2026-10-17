import React from 'react';
import type { UIActionResult } from '@mcp-ui/client';
import type { TAskFunction } from '~/common';
import logger from './logger';

export * from './map';
export * from './policies';
export * from './json';
export * from './icons';
export * from './svg';
export * from './row';
export * from './email';
export * from './share';
export * from './files';
export * from './media';
export * from './greeting';
export * from './latex';
export * from './tilde';
export * from './morph';
export * from './forms';
export * from './roles';
export * from './errors';
export * from './agents';
export * from './drafts';
export * from './convos';
export * from './queue';
export * from './routes';
export * from './presets';
export * from './prompts';
export * from './voices';
export * from './textarea';
export * from './messages';
export * from './focus';
export * from './tokens';
export * from './balance';
export * from './redirect';
export * from './links';
export * from './languages';
export * from './conversation';
export * from './endpoints';
export * from './resources';
export * from './configHtml';
export * from './downloadFile';
export * from './scaleImage';
export * from './timestamps';
export * from './localStorage';
export * from './promptGroups';
export * from './previewCache';
export * from './groupToolCalls';
export * from './toolLabels';
export * from './favoritesError';
export * from './approval';
export * from './steer';
export * from './pane';
export * from './activityLabels';
export * from './agentModelSelection';
export * from './runStepDuration';
export * from './toolCallPhase';
export * from './documentTitle';
export * from './reasoningLabels';
export * from './numbers';
export * from './studio';
export { default as cn } from './cn';
export { default as logger } from './logger';
export { default as getLoginError } from './getLoginError';
export { default as cleanupPreset } from './cleanupPreset';
export { default as buildDefaultConvo } from './buildDefaultConvo';
export { default as getDefaultEndpoint } from './getDefaultEndpoint';
export { default as createChatSearchParams, processValidSettings } from './createChatSearchParams';
export { getThemeFromEnv } from './getThemeFromEnv';
import {
  buildStudioDraftKey,
  extractCurrentStudioEditorCapability,
  extractStudioSelectionImageId,
  isStudioUIResource,
  loadUIDraft,
  renewStudioResourceLinks,
  saveUIDraft,
  studioUIStore,
} from './studio';
import type { StudioUIActionContext } from './studio';

export const languages = [
  'java',
  'c',
  'markdown',
  'css',
  'html',
  'xml',
  'bash',
  'json',
  'yaml',
  'jsx',
  'python',
  'c++',
  'javascript',
  'csharp',
  'php',
  'typescript',
  'swift',
  'objectivec',
  'sql',
  'r',
  'kotlin',
  'ruby',
  'go',
  'x86asm',
  'matlab',
  'perl',
  'pascal',
];

export const removeFocusOutlines = '';
export const removeFocusRings = 'focus:outline-hidden focus:ring-0 focus:ring-offset-0';

export const cardStyle =
  'transition-colors rounded-md min-w-[4.6875rem] border border-border-medium font-normal bg-surface-secondary hover:bg-surface-hover text-text-primary focus:outline-hidden data-[state=open]:bg-surface-hover';

export const defaultTextProps =
  'rounded-md border border-border-light focus:border-border-heavy focus:bg-surface-secondary bg-transparent text-sm text-text-primary shadow-[0_0_0.625rem_rgba(0,0,0,0.05)] outline-hidden focus-within:placeholder:text-text-primary focus:placeholder:text-text-primary placeholder:text-text-secondary focus:outline-hidden focus:ring-text-primary/20 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50';

export const optionText =
  'p-0 shadow-none text-right pr-1 h-8 border-transparent hover:bg-surface-hover transition-colors';

export const defaultTextPropsLabel =
  'rounded-md border border-border-medium bg-transparent text-sm text-text-primary shadow-[0_0_0.625rem_rgba(0,0,0,0.10)] outline-hidden focus-within:placeholder:text-text-primary focus:placeholder:text-text-primary placeholder:text-text-secondary focus:outline-hidden disabled:cursor-not-allowed disabled:opacity-50';

export function capitalizeFirstLetter(string: string) {
  return string.charAt(0).toUpperCase() + string.slice(1);
}

export const handleDoubleClick: React.MouseEventHandler<HTMLElement> = (event) => {
  const range = document.createRange();
  range.selectNodeContents(event.target as Node);
  const selection = window.getSelection();
  if (!selection) {
    return;
  }
  selection.removeAllRanges();
  selection.addRange(range);
};

export const extractContent = (
  children: React.ReactNode | { props: { children: React.ReactNode } } | string,
): string => {
  if (typeof children === 'string') {
    return children;
  }
  if (React.isValidElement(children)) {
    return extractContent((children.props as { children?: React.ReactNode }).children);
  }
  if (Array.isArray(children)) {
    return children.map(extractContent).join('');
  }
  return '';
};

function verifyStudioSelectionContext(
  payload: Record<string, unknown>,
  context?: StudioUIActionContext,
  requireCapability = false,
):
  | {
      ok: true;
      draftKey: string;
      editorCapability: string;
      sessionNonce?: string;
    }
  | {
      ok: false;
      reason: string;
    } {
  if (!context?.userId?.trim() || !context.resource) {
    return { ok: false, reason: 'unauthenticated_or_missing_resource' };
  }
  const resourceImageId = extractStudioSelectionImageId(context.resource.uri);
  if (!resourceImageId || !isStudioUIResource(context.resource, context.serverName)) {
    return { ok: false, reason: 'invalid_selection_resource' };
  }
  if (
    typeof payload.resourceUri === 'string' &&
    payload.resourceUri.trim() &&
    payload.resourceUri.trim() !== context.resource.uri
  ) {
    return { ok: false, reason: 'resource_uri_mismatch' };
  }
  if (
    typeof payload.imageId === 'string' &&
    payload.imageId.trim() &&
    payload.imageId.trim() !== resourceImageId
  ) {
    return { ok: false, reason: 'image_id_mismatch' };
  }
  const expectedCapability = extractCurrentStudioEditorCapability(context.resource);
  const providedCapability =
    typeof payload.editorCapability === 'string' ? payload.editorCapability.trim() : '';
  if (!expectedCapability) {
    return { ok: false, reason: 'invalid_current_capability' };
  }
  if (requireCapability && !providedCapability) {
    return { ok: false, reason: 'missing_capability' };
  }
  if (providedCapability && providedCapability !== expectedCapability) {
    return { ok: false, reason: 'capability_mismatch' };
  }
  const draftKey = buildStudioDraftKey(context);
  if (!draftKey) {
    return { ok: false, reason: 'invalid_draft_scope' };
  }
  if (typeof payload.draftKey === 'string' && payload.draftKey.trim()) {
    const expectedIframeKey = `zyb_studio:selection:${resourceImageId}:${expectedCapability}`;
    if (payload.draftKey.trim() !== draftKey && payload.draftKey.trim() !== expectedIframeKey) {
      return { ok: false, reason: 'draft_key_mismatch' };
    }
  }
  return {
    ok: true,
    draftKey,
    editorCapability: expectedCapability,
    sessionNonce: typeof payload.sessionNonce === 'string' ? payload.sessionNonce : undefined,
  };
}

/** Converts supported legacy MCP-UI actions into the existing conversation submission path. */
export const handleUIAction = async (
  result: UIActionResult,
  ask: TAskFunction,
  context?: StudioUIActionContext,
) => {
  const rawAction = result as {
    type?: string;
    payload?: Record<string, unknown>;
  };
  if (rawAction?.type === 'ui-draft-save') {
    const payload = rawAction.payload ?? {};
    const verified = verifyStudioSelectionContext(payload, context, true);
    if (!verified.ok) {
      return { status: 'rejected', reason: verified.reason };
    }
    saveUIDraft(verified.draftKey, payload.draft ?? null, context?.store ?? studioUIStore);
    return {
      status: 'saved',
      draftKey: verified.draftKey,
      editorCapability: verified.editorCapability,
      sessionNonce: verified.sessionNonce,
    };
  }
  if (rawAction?.type === 'ui-draft-load') {
    const payload = rawAction.payload ?? {};
    const verified = verifyStudioSelectionContext(payload, context);
    if (!verified.ok) {
      return { status: 'rejected', reason: verified.reason };
    }
    const draft = loadUIDraft(verified.draftKey, context?.store ?? studioUIStore);
    return {
      status: 'loaded',
      draftKey: verified.draftKey,
      editorCapability: verified.editorCapability,
      sessionNonce: verified.sessionNonce,
      draft,
    };
  }
  if (rawAction?.type === 'ui-link-renew') {
    const payload = rawAction.payload ?? {};
    if (!context) {
      return { status: 'rejected', reason: 'missing_context' };
    }
    return renewStudioResourceLinks(payload, context);
  }

  const { type, payload } = result;
  let messageText = '';

  if (type === 'intent') {
    const { intent, params } = payload;
    messageText = `The user clicked a button in an embedded UI Resource, and we got a message of type \`intent\`.
The intent is \`${intent}\` and the params are:

\`\`\`json
${JSON.stringify(params, null, 2)}
\`\`\`

Execute the intent that is mentioned in the message using the tools available to you.
    `;
  } else if (type === 'tool') {
    const { toolName, params } = payload;
    messageText = `The user clicked a button in an embedded UI Resource, and we got a message of type \`tool\`.
The tool name is \`${toolName}\` and the params are:

\`\`\`json
${JSON.stringify(params, null, 2)}
\`\`\`

Execute the tool that is mentioned in the message using the tools available to you.
    `;
  } else if (type === 'prompt') {
    const { prompt } = payload;
    if (isStudioUIResource(context?.resource, context?.serverName)) {
      // 仅对已认证的智印工坊资源直接提交纯中文指令文本，避免无语言代码块围栏触发wasm语法高亮标签
      messageText = typeof prompt === 'string' ? prompt.trim() : '';
      if (!messageText) {
        return;
      }
    } else {
      messageText = `The user clicked a button in an embedded UI Resource, and we got a message of type \`prompt\`.
The prompt is:

\`\`\`
${prompt}
\`\`\`

Execute the intention of the prompt that is mentioned in the message using the tools available to you.
    `;
    }
  } else {
    return;
  }

  logger.debug('MCP-UI', 'About to submit message:', messageText);
  ask({ text: messageText });
  logger.debug('MCP-UI', 'Message submitted successfully');
};
