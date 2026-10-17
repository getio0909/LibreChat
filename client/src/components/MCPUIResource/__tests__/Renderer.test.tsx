import { render, screen } from '@testing-library/react';
import { UIResourceRenderer as LegacyUIResourceRenderer } from '@mcp-ui/client';
import type { TStartupConfig, UIResource } from 'librechat-data-provider';
import type { UIActionResult } from '@mcp-ui/client';
import { MCPAppsPolicyProvider } from '~/Providers/MCPAppsPolicyContext';
import { handleUIAction, saveUIDraft, loadUIDraft } from '~/utils';
import UIResourceRenderer from '../Renderer';

jest.mock('@mcp-ui/client', () => ({
  UIResourceRenderer: jest.fn(({ resource }) => (
    <div data-testid="legacy-ui-resource" data-mime-type={resource.mimeType} />
  )),
}));

const mockLegacyRenderer = LegacyUIResourceRenderer as jest.MockedFunction<
  typeof LegacyUIResourceRenderer
>;

const renderEnabled = (ui: React.ReactElement) =>
  render(
    <MCPAppsPolicyProvider
      startupConfig={{ mcpApps: { enabled: true, legacyHtmlEnabled: true } } as TStartupConfig}
      ready
      userId="user-1"
    >
      {ui}
    </MCPAppsPolicyProvider>,
  );

describe('UIResourceRenderer', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    window.sessionStorage.clear();
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

  it('keeps resource and htmlProps references stable across breakpoint re-renders and restores saved selection drafts', async () => {
    const resource: UIResource = {
      resourceId: 'selection-1',
      uri: 'ui://zyb_studio/selection/img_demo123',
      mimeType: 'text/html',
      text: '<p>Selection editor</p>',
    };
    const htmlProps = { autoResizeIframe: { width: true, height: true } };
    const ask = jest.fn();

    const saveResp = await handleUIAction(
      {
        type: 'ui-draft-save',
        payload: {
          draftKey: 'zyb_studio_selection_draft_img_demo123',
          draft: { tool: 'rect', instruction: '只改中间山峰轮廓' },
        },
      } as unknown as UIActionResult,
      ask,
    );
    expect(saveResp).toEqual({
      status: 'saved',
      draftKey: 'zyb_studio_selection_draft_img_demo123',
    });
    expect(ask).not.toHaveBeenCalled();

    const loadResp = await handleUIAction(
      {
        type: 'ui-draft-load',
        payload: { draftKey: 'zyb_studio_selection_draft_img_demo123' },
      } as unknown as UIActionResult,
      ask,
    );
    expect(loadResp).toEqual({
      status: 'loaded',
      draftKey: 'zyb_studio_selection_draft_img_demo123',
      draft: { tool: 'rect', instruction: '只改中间山峰轮廓' },
    });
    expect(loadUIDraft('zyb_studio_selection_draft_img_demo123')).toEqual({
      tool: 'rect',
      instruction: '只改中间山峰轮廓',
    });

    const { rerender } = renderEnabled(
      <UIResourceRenderer resource={resource} htmlProps={htmlProps} />,
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
        <UIResourceRenderer resource={resource} htmlProps={htmlProps} />
      </MCPAppsPolicyProvider>,
    );
    const secondProps = mockLegacyRenderer.mock.calls[1][0];
    expect(secondProps.resource).toBe(firstProps.resource);
    expect(secondProps.htmlProps).toBe(firstProps.htmlProps);

    saveUIDraft('zyb_studio_selection_draft_img_demo123', null);
    expect(loadUIDraft('zyb_studio_selection_draft_img_demo123')).toBeUndefined();
  });
});
