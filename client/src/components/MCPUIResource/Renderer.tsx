import { useCallback, useMemo, useState } from 'react';
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
  isStudioUIResource,
  loadUIDraft,
  studioRenewedLinksAtom,
  studioUIStore,
  upgradePersistedStudioHtml,
} from '~/utils/studio';
import { useOptionalMessagesConversation } from '~/Providers/MessagesViewContext';
import { useMCPAppsHost } from '~/Providers/MCPAppsPolicyContext';
import { handleUIAction } from '~/utils';
import { useLocalize } from '~/hooks';

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
  const localize = useLocalize();
  const [deniedResourceId, setDeniedResourceId] = useState<string | null>(null);
  const { conversationId } = useOptionalMessagesConversation();
  const isSupported = Boolean(policy?.legacyHtmlEnabled) && isSupportedUIResource(resource);
  const legacyStudioCard =
    resource.uri.startsWith('ui://zyb_studio/') &&
    (!resource.serverName || resource.serverName === 'zyb_studio') &&
    !isStudioUIResource(resource, resource.serverName);

  const resourceKey = useMemo(
    () =>
      buildStudioResourceKey({
        userId,
        conversationId,
        serverName: resource.serverName,
        resource,
      }),
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
        nextResource.serverName,
        typeof nextResource.verifiedServerName === 'string'
          ? nextResource.verifiedServerName
          : undefined,
      );
    }
    return nextResource;
  }, [isSupported, resource, renewedLinks]);

  const safeHtmlProps = useMemo(() => {
    const nextHtmlProps = { ...htmlProps };
    delete nextHtmlProps.sandboxPermissions;
    const draftKey = buildStudioDraftKey({
      userId,
      conversationId,
      serverName: resource.serverName,
      resource,
    });
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
        const response = await handleUIAction(result, NOOP_ASK, {
          userId,
          conversationId,
          serverName: resource.serverName,
          resource: safeResource ?? resource,
          store: studioUIStore,
          renewFetcher: renewThroughHost as typeof fetch,
        });
        if (
          rawType === 'ui-link-renew' &&
          response?.status === 'rejected' &&
          (response.reason === 'http_403' ||
            (legacyStudioCard && response.reason === 'unauthenticated_or_invalid_resource'))
        ) {
          console.warn('[StudioRenew] Stored card renewal was denied; reopen the Studio view');
          setDeniedResourceId(resource.resourceId);
        } else if (rawType === 'ui-link-renew' && response?.status === 'renewed') {
          setDeniedResourceId(null);
        }
        return response;
      }
      return onUIAction?.(result);
    },
    [onUIAction, userId, conversationId, resource, safeResource, legacyStudioCard],
  );

  if (!safeResource) {
    return null;
  }

  return (
    <>
      <LegacyUIResourceRenderer
        {...props}
        resource={safeResource}
        onUIAction={scopedOnUIAction}
        htmlProps={safeHtmlProps}
        supportedContentTypes={['rawHtml']}
      />
      {(legacyStudioCard || deniedResourceId === resource.resourceId) && (
        <p
          role="alert"
          className="bg-status-warning-subtle text-text-primary mt-2 rounded px-3 py-2 text-sm"
        >
          {localize('com_ui_studio_reopen_card')}
        </p>
      )}
    </>
  );
}
