import { useMemo } from 'react';
import { UIResourceRenderer as LegacyUIResourceRenderer } from '@mcp-ui/client';
import {
  isHtmlMediaType,
  isMcpAppMimeType,
  resolveMCPUIResourceMimeType,
} from 'librechat-data-provider';
import type { UIResource } from 'librechat-data-provider';
import type { ComponentProps } from 'react';
import { useMCPAppsPolicy } from '~/Providers/MCPAppsPolicyContext';
import { loadUIDraft } from '~/utils';

type LegacyRendererProps = ComponentProps<typeof LegacyUIResourceRenderer>;

type UIResourceRendererProps = Omit<
  LegacyRendererProps,
  'resource' | 'remoteDomProps' | 'supportedContentTypes'
> & {
  resource: UIResource;
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
  ...props
}: UIResourceRendererProps) {
  const { legacyHtmlEnabled } = useMCPAppsPolicy();
  const isSupported = legacyHtmlEnabled && isSupportedUIResource(resource);

  const safeResource = useMemo(() => {
    if (!isSupported) {
      return null;
    }
    const nextResource = { ...resource };
    delete nextResource.contentType;
    nextResource.mimeType = 'text/html';
    return nextResource;
  }, [isSupported, resource]);

  const safeHtmlProps = useMemo(() => {
    const nextHtmlProps = { ...htmlProps };
    delete nextHtmlProps.sandboxPermissions;
    const uri = resource?.uri ?? '';
    if (uri.startsWith('ui://zyb_studio/selection/')) {
      const imageId = uri.slice('ui://zyb_studio/selection/'.length).split('?')[0];
      const draftKey = imageId ? `zyb_studio_selection_draft_${imageId}` : '';
      const selectionDraft = draftKey ? loadUIDraft(draftKey) : undefined;
      if (selectionDraft !== undefined) {
        nextHtmlProps.iframeRenderData = {
          ...(nextHtmlProps.iframeRenderData ?? {}),
          selectionDraft,
        };
      }
    }
    return nextHtmlProps;
  }, [htmlProps, resource?.uri]);

  if (!safeResource) {
    return null;
  }

  return (
    <LegacyUIResourceRenderer
      {...props}
      resource={safeResource}
      htmlProps={safeHtmlProps}
      supportedContentTypes={['rawHtml']}
    />
  );
}
