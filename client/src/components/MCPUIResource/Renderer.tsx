import { useCallback, useMemo } from 'react';
import { useAtomValue } from 'jotai';
import { UIResourceRenderer as LegacyUIResourceRenderer } from '@mcp-ui/client';
import {
  isHtmlMediaType,
  isMcpAppMimeType,
  request,
  resolveMCPUIResourceMimeType,
} from 'librechat-data-provider';
import type { UIResource } from 'librechat-data-provider';
import type { UIActionResult } from '@mcp-ui/client';
import type { ComponentProps } from 'react';
import type { TAskFunction } from '~/common';
import {
  buildStudioDraftKey,
  buildStudioResourceKey,
  loadUIDraft,
  studioRenewedLinksAtom,
  studioUIStore,
  upgradePersistedStudioHtml,
} from '~/utils/studio';
import { useOptionalMessagesConversation } from '~/Providers/MessagesViewContext';
import { useMCPAppsHost } from '~/Providers/MCPAppsPolicyContext';
import { handleUIAction } from '~/utils';

type LegacyRendererProps = ComponentProps<typeof LegacyUIResourceRenderer>;

type UIResourceRendererProps = Omit<
  LegacyRendererProps,
  'resource' | 'remoteDomProps' | 'supportedContentTypes'
> & {
  resource: UIResource;
};

const NOOP_ASK: TAskFunction = () => undefined;

const renewThroughHost = async (
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> => {
  const endpoint = new URL(String(input), window.location.origin);
  const match = /^\/(?:studio-files|files)\/([A-Za-z0-9_-]+\.[a-f0-9]{64})$/.exec(
    endpoint.pathname,
  );
  if (endpoint.origin !== window.location.origin || !match) {
    throw new Error('Invalid Studio renewal endpoint');
  }
  return request.authenticatedFetch(`/api/mcp/studio/renew/${encodeURIComponent(match[1])}`, init);
};

export function isSupportedUIResource(
  resource: UIResource | null | undefined,
): resource is UIResource {
  if (!resource || (resource.mimeType != null && typeof resource.mimeType !== 'string')) {
    return false;
  }
  const mimeType = resolveMCPUIResourceMimeType(resource.mimeType);
  return !isMcpAppMimeType(mimeType) && isHtmlMediaType(mimeType);
}

/** Restricts legacy MCP-UI rendering to sandboxed inline HTML resources. */
export default function UIResourceRenderer({
  resource,
  htmlProps,
  onUIAction,
  ...props
}: UIResourceRendererProps) {
  const { policy, userId } = useMCPAppsHost();
  const { conversationId } = useOptionalMessagesConversation();
  const isSupported = Boolean(policy?.legacyHtmlEnabled) && isSupportedUIResource(resource);

  const resourceKey = useMemo(
    () => buildStudioResourceKey({ userId, conversationId, resource }),
    [userId, conversationId, resource],
  );
  const renewedLinksMap = useAtomValue(studioRenewedLinksAtom, { store: studioUIStore });
  const renewedLinks = resourceKey ? renewedLinksMap[resourceKey] : undefined;

  const safeResource = useMemo(() => {
    if (!isSupported) {
      return null;
    }
    const nextResource = { ...resource };
    delete nextResource.contentType;
    nextResource.mimeType = 'text/html';
    if (typeof nextResource.text === 'string' && nextResource.text) {
      nextResource.text = upgradePersistedStudioHtml(
        nextResource.text,
        nextResource.uri,
        renewedLinks,
      );
    }
    return nextResource;
  }, [isSupported, resource, renewedLinks]);

  const safeHtmlProps = useMemo(() => {
    const nextHtmlProps = { ...htmlProps };
    delete nextHtmlProps.sandboxPermissions;
    const draftKey = buildStudioDraftKey({ userId, conversationId, resource });
    const selectionDraft = draftKey ? loadUIDraft(draftKey, studioUIStore) : undefined;
    if (selectionDraft !== undefined || renewedLinks !== undefined) {
      nextHtmlProps.iframeRenderData = {
        ...(nextHtmlProps.iframeRenderData ?? {}),
        ...(selectionDraft !== undefined ? { selectionDraft } : {}),
        ...(renewedLinks !== undefined ? { renewedLinks } : {}),
      };
    }
    return nextHtmlProps;
  }, [htmlProps, userId, conversationId, resource, renewedLinks]);

  const scopedOnUIAction = useCallback(
    async (result: UIActionResult) => {
      const rawType = (result as { type?: string } | null)?.type;
      if (
        rawType === 'ui-draft-save' ||
        rawType === 'ui-draft-load' ||
        rawType === 'ui-link-renew'
      ) {
        return handleUIAction(result, NOOP_ASK, {
          userId,
          conversationId,
          resource,
          store: studioUIStore,
          renewFetcher: renewThroughHost as typeof fetch,
        });
      }
      return onUIAction?.(result);
    },
    [onUIAction, userId, conversationId, resource],
  );

  if (!safeResource) {
    return null;
  }

  return (
    <LegacyUIResourceRenderer
      {...props}
      resource={safeResource}
      onUIAction={scopedOnUIAction}
      htmlProps={safeHtmlProps}
      supportedContentTypes={['rawHtml']}
    />
  );
}
