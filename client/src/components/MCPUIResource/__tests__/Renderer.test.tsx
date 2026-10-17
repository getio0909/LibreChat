import { act, render, screen, waitFor } from '@testing-library/react';
import { UIResourceRenderer as LegacyUIResourceRenderer } from '@mcp-ui/client';
import type { TStartupConfig, UIResource } from 'librechat-data-provider';
import type { UIActionResult } from '@mcp-ui/client';
import type { MessagesViewContextValue } from '~/Providers/MessagesViewContext';
import {
  buildStudioDraftKey,
  handleUIAction,
  loadUIDraft,
  resetStudioUIState,
  saveUIDraft,
  studioSelectionDraftsAtom,
  studioUIStore,
  unwrapLegacyUIPromptText,
  upgradePersistedStudioHtml,
} from '~/utils';
import { MCPAppsPolicyProvider } from '~/Providers/MCPAppsPolicyContext';
import { MessagesViewContext } from '~/Providers/MessagesViewContext';
import UIResourceRenderer from '../Renderer';

const actualMcpUi = jest.requireActual('@mcp-ui/client') as typeof import('@mcp-ui/client');

jest.mock('@mcp-ui/client', () => ({
  UIResourceRenderer: jest.fn(({ resource }) => (
    <div data-testid="legacy-ui-resource" data-mime-type={resource.mimeType} />
  )),
}));

const mockLegacyRenderer = LegacyUIResourceRenderer as jest.MockedFunction<
  typeof LegacyUIResourceRenderer
>;

// 构造符合真实签名路径结构的测试令牌URL（包含img:或edt:前缀与64位十六进制签名）
function makeSignedStudioUrl(
  prefix: 'img' | 'edt',
  id: string,
  sigChar: string,
  origin = 'http://localhost:18080',
): string {
  const rawPayload =
    prefix === 'edt'
      ? `edt:user_1:1893456000:${'a'.repeat(64)}:nonce_${sigChar}:${id}`
      : `img:user_1:1893456000:${id}`;
  const b64 = Buffer.from(rawPayload, 'utf-8')
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/g, '');
  return `${origin}/files/${b64}.${sigChar.repeat(64)}`;
}

const renderEnabled = (
  ui: React.ReactElement,
  {
    userId = 'user-1',
    conversationId = 'convo-1',
  }: { userId?: string; conversationId?: string } = {},
) =>
  render(
    <MCPAppsPolicyProvider
      startupConfig={{ mcpApps: { enabled: true, legacyHtmlEnabled: true } } as TStartupConfig}
      ready
      userId={userId}
    >
      <MessagesViewContext.Provider
        value={{ conversationId } as unknown as MessagesViewContextValue}
      >
        {ui}
      </MessagesViewContext.Provider>
    </MCPAppsPolicyProvider>,
  );

describe('UIResourceRenderer', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    resetStudioUIState();
  });

  it.each([
    'application/vnd.mcp-ui.remote-dom+javascript',
    'application/vnd.mcp-ui.remote-dom',
    'text/uri-list',
    'text/html;profile=mcp-app',
  ])('blocks MIME type %s from the legacy renderer', (mimeType) => {
    const resource: UIResource = {
      resourceId: 'unsafe-resource',
      uri: 'ui://unsafe',
      mimeType,
      text: "root.innerHTML='<img src=x onerror=alert(window.origin)>'",
    };

    const { container } = renderEnabled(<UIResourceRenderer resource={resource} />);

    expect(container).toBeEmptyDOMElement();
    expect(mockLegacyRenderer).not.toHaveBeenCalled();
  });

  it('blocks malformed non-string MIME values', () => {
    const resource: UIResource = {
      resourceId: 'malformed-resource',
      uri: 'ui://malformed',
      mimeType: 1 as unknown as string,
      text: '<p>Malformed resource</p>',
    };

    const { container } = renderEnabled(<UIResourceRenderer resource={resource} />);

    expect(container).toBeEmptyDOMElement();
    expect(mockLegacyRenderer).not.toHaveBeenCalled();
  });

  it('forces text/html through the raw HTML renderer without popup permissions', () => {
    const resource: UIResource = {
      resourceId: 'html-resource',
      uri: 'ui://html',
      mimeType: 'text/html',
      contentType: 'remoteDom',
      text: '<p>Safe iframe content</p>',
    };

    renderEnabled(
      <UIResourceRenderer
        resource={resource}
        htmlProps={{ sandboxPermissions: 'allow-popups allow-same-origin' }}
      />,
    );

    expect(screen.getByTestId('legacy-ui-resource')).toBeInTheDocument();
    expect(mockLegacyRenderer).toHaveBeenCalledWith(
      expect.objectContaining({
        resource: expect.not.objectContaining({ contentType: expect.anything() }),
        htmlProps: {},
        supportedContentTypes: ['rawHtml'],
      }),
      expect.any(Object),
    );
  });

  it.each(['text/html; charset=utf-8', 'TEXT/HTML'])('normalizes HTML MIME type %s', (mimeType) => {
    const resource: UIResource = {
      resourceId: 'html-resource',
      uri: 'ui://html',
      mimeType,
      text: '<p>Safe iframe content</p>',
    };

    renderEnabled(<UIResourceRenderer resource={resource} />);

    expect(mockLegacyRenderer).toHaveBeenCalledWith(
      expect.objectContaining({
        resource: expect.objectContaining({ mimeType: 'text/html' }),
        supportedContentTypes: ['rawHtml'],
      }),
      expect.any(Object),
    );
  });

  it.each([undefined, null])('defaults an absent MIME type (%s) to legacy HTML', (mimeType) => {
    const resource = {
      resourceId: 'legacy-resource',
      uri: 'ui://legacy',
      mimeType,
      text: '<p>Legacy resource</p>',
    } as unknown as UIResource;

    renderEnabled(<UIResourceRenderer resource={resource} />);

    expect(mockLegacyRenderer).toHaveBeenCalledWith(
      expect.objectContaining({
        resource: expect.objectContaining({ mimeType: 'text/html' }),
      }),
      expect.any(Object),
    );
  });

  it('does not reinterpret an explicit empty MIME type as HTML', () => {
    const resource: UIResource = {
      resourceId: 'empty-resource',
      uri: 'ui://empty',
      mimeType: '',
      text: '<p>Explicitly untyped</p>',
    };

    const { container } = renderEnabled(<UIResourceRenderer resource={resource} />);

    expect(container).toBeEmptyDOMElement();
    expect(mockLegacyRenderer).not.toHaveBeenCalled();
  });

  it('does not invoke the legacy SDK without an enabled host policy', () => {
    const resource: UIResource = {
      resourceId: 'stored-html',
      uri: 'ui://legacy/stored',
      mimeType: 'text/html',
      text: '<p>Stored legacy view</p>',
    };

    const { container } = render(<UIResourceRenderer resource={resource} />);

    expect(container).toBeEmptyDOMElement();
    expect(mockLegacyRenderer).not.toHaveBeenCalled();
  });

  it('keeps resource and htmlProps references stable across breakpoint re-renders and scopes Jotai drafts by user, conversation, and editor capability', async () => {
    const editorUrl1 = makeSignedStudioUrl('edt', 'img_demo123', '1');
    const editorUrl2 = makeSignedStudioUrl('edt', 'img_demo123', '2');
    const resource1: UIResource = {
      resourceId: 'selection-1',
      uri: 'ui://zyb_studio/selection/img_demo123',
      mimeType: 'text/html',
      text: `<script>const SIGNED_URL = "${editorUrl1}";</script>`,
    };
    const resource2SameImage: UIResource = {
      resourceId: 'selection-2',
      uri: 'ui://zyb_studio/selection/img_demo123',
      mimeType: 'text/html',
      text: `<script>const SIGNED_URL = "${editorUrl2}";</script>`,
    };
    const nonSelectionResource: UIResource = {
      resourceId: 'other-1',
      uri: 'ui://other/widget',
      mimeType: 'text/html',
      text: '<p>Other widget</p>',
    };
    const htmlProps = { autoResizeIframe: { width: true, height: true } };
    const ask = jest.fn();

    const expectedKey1 = buildStudioDraftKey({
      userId: 'user-1',
      conversationId: 'convo-1',
      resource: resource1,
    });
    expect(expectedKey1).toContain(
      'user-1:convo-1:img_demo123:selection-1:111111111111111111111111',
    );

    // 未认证或非选区编辑器资源发起的草稿读写请求必须被拒绝
    const unauthResp = await handleUIAction(
      {
        type: 'ui-draft-save',
        payload: {
          imageId: 'img_demo123',
          draft: { tool: 'rect', instruction: '未授权写入' },
        },
      } as unknown as UIActionResult,
      ask,
    );
    expect(unauthResp).toEqual({
      status: 'rejected',
      reason: 'unauthenticated_or_missing_resource',
    });

    const wrongResourceResp = await handleUIAction(
      {
        type: 'ui-draft-save',
        payload: {
          imageId: 'img_demo123',
          draft: { tool: 'rect', instruction: '越权写入' },
        },
      } as unknown as UIActionResult,
      ask,
      { userId: 'user-1', conversationId: 'convo-1', resource: nonSelectionResource },
    );
    expect(wrongResourceResp).toEqual({
      status: 'rejected',
      reason: 'invalid_selection_resource',
    });

    const saveResp = await handleUIAction(
      {
        type: 'ui-draft-save',
        payload: {
          draftKey: `zyb_studio:selection:img_demo123:${'1'.repeat(24)}`,
          imageId: 'img_demo123',
          resourceUri: 'ui://zyb_studio/selection/img_demo123',
          editorCapability: '1'.repeat(24),
          sessionNonce: 'nonce-abc',
          draft: { tool: 'rect', instruction: '只改中间山峰轮廓' },
        },
      } as unknown as UIActionResult,
      ask,
      { userId: 'user-1', conversationId: 'convo-1', resource: resource1 },
    );
    expect(saveResp).toEqual({
      status: 'saved',
      draftKey: expectedKey1,
      editorCapability: '1'.repeat(24),
      sessionNonce: 'nonce-abc',
    });
    expect(ask).not.toHaveBeenCalled();
    expect(studioUIStore.get(studioSelectionDraftsAtom)[expectedKey1]).toEqual({
      tool: 'rect',
      instruction: '只改中间山峰轮廓',
    });

    // 不同用户、不同会话或同一图片的第二张选区编辑器卡片均无法读取第一张编辑器的草稿
    const user2Load = await handleUIAction(
      {
        type: 'ui-draft-load',
        payload: { imageId: 'img_demo123' },
      } as unknown as UIActionResult,
      ask,
      { userId: 'user-2', conversationId: 'convo-1', resource: resource1 },
    );
    expect(user2Load).toEqual(expect.objectContaining({ status: 'loaded', draft: undefined }));

    const secondEditorLoad = await handleUIAction(
      {
        type: 'ui-draft-load',
        payload: { imageId: 'img_demo123' },
      } as unknown as UIActionResult,
      ask,
      { userId: 'user-1', conversationId: 'convo-1', resource: resource2SameImage },
    );
    expect(secondEditorLoad).toEqual(
      expect.objectContaining({ status: 'loaded', draft: undefined }),
    );

    const { rerender } = renderEnabled(
      <UIResourceRenderer resource={resource1} htmlProps={htmlProps} />,
    );
    const firstProps = mockLegacyRenderer.mock.calls[0][0];
    expect(firstProps.htmlProps?.iframeRenderData).toEqual({
      selectionDraft: { tool: 'rect', instruction: '只改中间山峰轮廓' },
    });

    rerender(
      <MCPAppsPolicyProvider
        startupConfig={{ mcpApps: { enabled: true, legacyHtmlEnabled: true } } as TStartupConfig}
        ready
        userId="user-1"
      >
        <MessagesViewContext.Provider
          value={{ conversationId: 'convo-1' } as unknown as MessagesViewContextValue}
        >
          <UIResourceRenderer resource={resource1} htmlProps={htmlProps} />
        </MessagesViewContext.Provider>
      </MCPAppsPolicyProvider>,
    );
    const secondProps = mockLegacyRenderer.mock.calls[1][0];
    expect(secondProps.resource).toBe(firstProps.resource);
    expect(secondProps.htmlProps).toBe(firstProps.htmlProps);

    saveUIDraft(expectedKey1, null);
    expect(loadUIDraft(expectedKey1)).toBeUndefined();
  });

  it('completes real @mcp-ui/client postMessage round trips for draft restore and in-place link renewal and upgrades persisted Studio HTML', async () => {
    mockLegacyRenderer.mockImplementation((props) => actualMcpUi.UIResourceRenderer(props));

    const oldEditorUrl = makeSignedStudioUrl('edt', 'img_demo123', 'a');
    const oldPreviewUrl = makeSignedStudioUrl('img', 'img_demo123', 'b');
    const newEditorUrl = makeSignedStudioUrl('edt', 'img_demo123', 'c');
    const newPreviewUrl = makeSignedStudioUrl('img', 'img_demo123', 'd');

    const persistedLegacyHtml = [
      '<div class="header-row">',
      '<span class="title">只改局部 · 选区编辑器（f21a1a8e-106f-4d88-96a8-53cf8298e3db · 1264×848）</span>',
      '<span class="meta" id="expiryBadge">链接有效期至 2026-04-17T09:55:56Z</span>',
      '</div>',
      `<img id="baseImg" src="${oldPreviewUrl}" />`,
      `<script>const SIGNED_URL = "${oldEditorUrl}"; const PREVIEW_URL = "${oldPreviewUrl}"; const EXPIRES_AT = "2026-04-17T09:55:56Z";</script>`,
    ].join('\n');

    const upgraded = upgradePersistedStudioHtml(
      persistedLegacyHtml,
      'ui://zyb_studio/selection/img_demo123',
    );
    expect(upgraded).toContain('<span class="title">只改局部 · 选区编辑器（1264×848）</span>');
    expect(upgraded).not.toContain('f21a1a8e-106f-4d88-96a8-53cf8298e3db');
    expect(upgraded).not.toContain('链接有效期至 2026-04-17T09:55:56Z');

    const resource: UIResource = {
      resourceId: 'selection-live-1',
      uri: 'ui://zyb_studio/selection/img_demo123',
      mimeType: 'text/html',
      text: persistedLegacyHtml,
    };

    const scopedKey = buildStudioDraftKey({
      userId: 'user-1',
      conversationId: 'convo-1',
      resource,
    });
    saveUIDraft(scopedKey, { tool: 'polygon', instruction: '局部底座浮雕加深' });

    const originalFetch = global.fetch;
    const mockFetch = jest.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () =>
        JSON.stringify({
          status: 'renewed',
          previewUrl: newPreviewUrl,
          editorUrl: newEditorUrl,
          expiresAt: '2026-04-17T12:30:00Z',
          previewExpiresAt: '2026-04-24T12:30:00Z',
        }),
    });
    global.fetch = mockFetch as unknown as typeof fetch;

    try {
      const { container } = renderEnabled(<UIResourceRenderer resource={resource} />);
      const iframe = container.querySelector('iframe');
      expect(iframe).not.toBeNull();

      const postedToIframe: unknown[] = [];
      const fakeContentWindow = {
        postMessage: jest.fn((msg: unknown) => {
          postedToIframe.push(msg);
        }),
      };
      Object.defineProperty(iframe!, 'contentWindow', {
        value: fakeContentWindow,
        configurable: true,
      });

      // 1. 触发真实的 @mcp-ui/client ui-lifecycle-iframe-ready 生命周期消息并校验 selectionDraft 投递
      await act(async () => {
        window.dispatchEvent(
          new MessageEvent('message', {
            source: fakeContentWindow as unknown as MessageEventSource,
            data: { type: 'ui-lifecycle-iframe-ready' },
          }),
        );
      });

      expect(fakeContentWindow.postMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          type: 'ui-lifecycle-iframe-render-data',
          payload: {
            renderData: {
              selectionDraft: { tool: 'polygon', instruction: '局部底座浮雕加深' },
            },
          },
        }),
        '*',
      );

      // 2. 触发真实的 ui-link-renew 消息，校验宿主调用 POST /files/<token> (action="renew") 并通过 ui-message-response 回传新令牌且升级持久化HTML
      await act(async () => {
        window.dispatchEvent(
          new MessageEvent('message', {
            source: fakeContentWindow as unknown as MessageEventSource,
            data: {
              type: 'ui-link-renew',
              messageId: 'renew-msg-1',
              payload: {
                resourceUri: 'ui://zyb_studio/selection/img_demo123',
                imageId: 'img_demo123',
                editorCapability: 'a'.repeat(24),
                sessionNonce: 'session-nonce-12345678',
                editorUrl: oldEditorUrl,
                previewUrl: oldPreviewUrl,
              },
            },
          }),
        );
      });

      await waitFor(() => {
        expect(mockFetch).toHaveBeenCalledWith(
          oldEditorUrl,
          expect.objectContaining({
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      });

      await waitFor(() => {
        expect(fakeContentWindow.postMessage).toHaveBeenCalledWith(
          expect.objectContaining({
            type: 'ui-message-response',
            messageId: 'renew-msg-1',
            payload: {
              response: expect.objectContaining({
                status: 'renewed',
                sessionNonce: 'session-nonce-12345678',
                editorCapability: 'a'.repeat(24),
                previewUrl: newPreviewUrl,
                editorUrl: newEditorUrl,
              }),
            },
          }),
          '*',
        );
      });

      const updatedSrcDoc = container.querySelector('iframe')?.getAttribute('srcdoc') ?? '';
      expect(updatedSrcDoc).toContain(newEditorUrl);
      expect(updatedSrcDoc).toContain(newPreviewUrl);
    } finally {
      global.fetch = originalFetch;
    }
  });

  it('submits clean Chinese prompt text without English MCP-UI boilerplate or code fences and unwraps legacy persisted prompt messages', async () => {
    const ask = jest.fn();
    const chinesePrompt =
      '已确认选区 sel_807d4455d1784369a3cc8525（底图 f21a1a8e-106f-4d88-96a8-53cf8298e3db），请执行局部修改：将台面改为汉白玉';

    await handleUIAction(
      {
        type: 'prompt',
        payload: { prompt: `  ${chinesePrompt}  ` },
      } as unknown as UIActionResult,
      ask,
    );

    expect(ask).toHaveBeenCalledTimes(1);
    expect(ask).toHaveBeenCalledWith({ text: chinesePrompt });
    expect(ask.mock.calls[0][0].text).not.toContain('embedded UI Resource');
    expect(ask.mock.calls[0][0].text).not.toContain('```');

    const legacyWrapped = [
      'The user clicked a button in an embedded UI Resource, and we got a message of type `prompt`.',
      'The prompt is:',
      '',
      '```',
      chinesePrompt,
      '```',
      '',
      'Execute the intention of the prompt that is mentioned in the message using the tools available to you.',
    ].join('\n');

    expect(unwrapLegacyUIPromptText(legacyWrapped)).toBe(chinesePrompt);
    expect(unwrapLegacyUIPromptText('普通用户提问文本')).toBe('普通用户提问文本');
  });
});
