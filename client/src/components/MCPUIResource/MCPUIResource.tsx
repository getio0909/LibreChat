import React, { useCallback } from 'react';
import type { UIActionResult } from '@mcp-ui/client';
import { useOptionalMessagesConversation, useOptionalMessagesOperations } from '~/Providers';
import { useConversationUIResources } from '~/hooks/Messages/useConversationUIResources';
import UIResourceRenderer, { isSupportedUIResource } from './Renderer';
import { handleUIAction } from '~/utils';
import { useLocalize } from '~/hooks';

const AUTO_RESIZE_HTML_PROPS = {
  autoResizeIframe: { width: true, height: true },
};

interface MCPUIResourceProps {
  node: {
    properties: {
      resourceId: string;
    };
  };
}

/** Renders an MCP UI resource based on its resource ID. Works in chat, share, and search views. */
export function MCPUIResource(props: MCPUIResourceProps) {
  const { resourceId } = props.node.properties;
  const localize = useLocalize();
  const { ask } = useOptionalMessagesOperations();
  const { conversationId } = useOptionalMessagesConversation();

  const conversationResourceMap = useConversationUIResources(conversationId ?? undefined);
  const onUIAction = useCallback(
    async (result: UIActionResult) => handleUIAction(result, ask),
    [ask],
  );

  const uiResource = conversationResourceMap.get(resourceId ?? '');

  if (!uiResource) {
    return (
      <span className="bg-surface-tertiary text-text-secondary inline-flex items-center rounded px-2 py-1 text-xs font-medium">
        {localize('com_ui_ui_resource_not_found', {
          0: resourceId ?? '',
        })}
      </span>
    );
  }

  if (!isSupportedUIResource(uiResource)) {
    return null;
  }

  try {
    return (
      <span className="mx-1 inline-block w-full align-middle">
        <UIResourceRenderer
          resource={uiResource}
          onUIAction={onUIAction}
          htmlProps={AUTO_RESIZE_HTML_PROPS}
        />
      </span>
    );
  } catch (error) {
    console.error('Error rendering UI resource:', error);
    return (
      <span className="bg-status-error-subtle text-status-error inline-flex items-center rounded px-2 py-1 text-xs font-medium">
        {localize('com_ui_ui_resource_error', { 0: uiResource.name || resourceId })}
      </span>
    );
  }
}
