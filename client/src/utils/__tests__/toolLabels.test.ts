import { Constants } from 'librechat-data-provider';
import {
  parseToolName,
  getToolDisplayLabel,
  getMCPServerDisplayLabel,
  getMCPToolDisplayLabel,
  TOOL_FRIENDLY_NAME_KEYS,
  MCP_SERVER_FRIENDLY_NAME_KEYS,
  MCP_TOOL_FRIENDLY_NAME_KEYS,
} from '../toolLabels';
import zhHans from '~/locales/zh-Hans/translation.json';

describe('parseToolName', () => {
  it('splits an MCP tool id into server + tool name', () => {
    const parsed = parseToolName('search_code_mcp_github');
    expect(parsed).toEqual({
      raw: 'search_code_mcp_github',
      mcpServer: 'github',
      toolName: 'search_code',
    });
  });

  it('handles an MCP id whose tool-name portion contains underscores', () => {
    const parsed = parseToolName('deeply_nested_sub_tool_mcp_some-server');
    expect(parsed.mcpServer).toBe('some-server');
    expect(parsed.toolName).toBe('deeply_nested_sub_tool');
  });

  it('returns empty mcpServer + friendlyKey for a known native tool', () => {
    const parsed = parseToolName('web_search');
    expect(parsed).toEqual({
      raw: 'web_search',
      mcpServer: '',
      toolName: 'web_search',
      friendlyKey: TOOL_FRIENDLY_NAME_KEYS.web_search,
    });
  });

  it('returns empty mcpServer + no friendlyKey for an unknown native tool', () => {
    const parsed = parseToolName('custom_tool');
    expect(parsed).toEqual({
      raw: 'custom_tool',
      mcpServer: '',
      toolName: 'custom_tool',
    });
  });

  it('handles an MCP id with an empty server segment (pathological)', () => {
    /** `<tool>_mcp_` with nothing after the delimiter — treat as MCP
     *  with an empty server name rather than falling through to the
     *  native-tool path, so the display logic can surface "broken" ids
     *  rather than silently showing the whole thing as a tool name. */
    const parsed = parseToolName('foo_mcp_');
    expect(parsed.mcpServer).toBe('');
    expect(parsed.toolName).toBe('foo');
  });
});

describe('getToolDisplayLabel', () => {
  const identityLocalize = (key: string): string => key;
  const zhLocalize = (key: string): string =>
    (zhHans as Record<string, string>)[key] ?? key;

  it('returns the MCP server name for an MCP tool', () => {
    expect(getToolDisplayLabel('search_code_mcp_github', identityLocalize)).toBe('github');
  });

  it('returns localized Chinese display names for zyb_studio MCP server and tools without vendor names', () => {
    expect(getToolDisplayLabel('search_library_mcp_zyb_studio', zhLocalize, ['zyb_studio'])).toBe(
      '设计工坊',
    );
    expect(getMCPServerDisplayLabel('zyb_studio', zhLocalize)).toBe('设计工坊');
    expect(getMCPServerDisplayLabel('zyb_studio', identityLocalize)).toBe(
      MCP_SERVER_FRIENDLY_NAME_KEYS.zyb_studio,
    );

    const expectedZhToolNames: Record<string, string> = {
      search_library: '检索素材图库',
      compare_materials: '打开素材对比面板',
      select_region: '打开局部圈选画布',
      edit_region: '执行局部重绘',
      recover_region_edit: '同步精修结果',
      finish_region_edit: '同步精修结果',
      create_3d_model: '生成 3D 白模',
      check_3d_model: '查询 3D 进度',
    };
    for (const [toolName, expected] of Object.entries(expectedZhToolNames)) {
      const label = getMCPToolDisplayLabel(toolName, zhLocalize);
      expect(label).toBe(expected);
      expect(label).not.toMatch(/gemini|openai|fal|tripo|hunyuan|zyb_studio/i);
      expect(MCP_TOOL_FRIENDLY_NAME_KEYS[toolName]).toBeDefined();
    }
  });

  it('returns the friendly translation key for a known native tool', () => {
    /** The identity-localize stub returns the translation key itself,
     *  which is what the real `useLocalize` would resolve at render time. */
    expect(getToolDisplayLabel('web_search', identityLocalize)).toBe(
      TOOL_FRIENDLY_NAME_KEYS.web_search,
    );
  });

  it('returns the code translation key for bash PTC tool calls', () => {
    expect(getToolDisplayLabel(Constants.BASH_PROGRAMMATIC_TOOL_CALLING, identityLocalize)).toBe(
      TOOL_FRIENDLY_NAME_KEYS[Constants.BASH_PROGRAMMATIC_TOOL_CALLING],
    );
  });

  it('returns the code translation key for bash_tool calls', () => {
    expect(getToolDisplayLabel('bash_tool', identityLocalize)).toBe(
      TOOL_FRIENDLY_NAME_KEYS.bash_tool,
    );
  });

  it('uses the background task label for the native poll tool, not an MCP tool with that name', () => {
    expect(getToolDisplayLabel(Constants.CHECK_BACKGROUND_TASK, identityLocalize)).toBe(
      'com_ui_background_tasks',
    );
    expect(
      getToolDisplayLabel(
        `${Constants.CHECK_BACKGROUND_TASK}${Constants.mcp_delimiter}remote`,
        identityLocalize,
      ),
    ).toBe('remote');
  });

  it.each(['set_memory', 'delete_memory'])('returns a friendly memory label for %s', (toolName) => {
    expect(getToolDisplayLabel(toolName, identityLocalize)).toBe(TOOL_FRIENDLY_NAME_KEYS[toolName]);
  });

  it('returns the raw name for an unknown native tool', () => {
    expect(getToolDisplayLabel('custom_tool', identityLocalize)).toBe('custom_tool');
  });
});
