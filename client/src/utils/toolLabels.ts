import { Constants, splitToolCallName } from 'librechat-data-provider';
import type { TranslationKeys } from '~/hooks';

/**
 * Shared tool-name parsing + friendly-label mapping used by the main
 * tool-call UI and the subagent ticker. Centralized so the MCP
 * delimiter (`<tool>_mcp_<server>`) and native-tool short names read
 * consistently in every surface that renders tool lifecycle.
 */

/** Native tool id → translation key for a user-readable short name. */
export const TOOL_FRIENDLY_NAME_KEYS: Record<string, TranslationKeys> = {
  execute_code: 'com_ui_tool_name_code',
  run_tools_with_code: 'com_ui_tool_name_code',
  run_tools_with_bash: 'com_ui_tool_name_code',
  bash_tool: 'com_ui_tool_name_code',
  [Constants.CHECK_BACKGROUND_TASK]: 'com_ui_background_tasks',
  web_search: 'com_ui_tool_name_web_search',
  image_gen_oai: 'com_ui_tool_name_image_gen',
  image_edit_oai: 'com_ui_tool_name_image_edit',
  gemini_image_gen: 'com_ui_tool_name_image_gen',
  file_search: 'com_ui_tool_name_file_search',
  code_interpreter: 'com_ui_tool_name_code_analysis',
  retrieval: 'com_ui_tool_name_file_search',
  ask_user_question: 'com_ui_tool_name_ask_user_question',
  create_file: 'com_ui_tool_name_create_file',
  set_memory: 'com_ui_tool_name_set_memory',
  edit_file: 'com_ui_tool_name_edit_file',
  delete_memory: 'com_ui_tool_name_delete_memory',
  skill: 'com_ui_skill',
  read_file: 'com_ui_tool_name_read_file',
};

/** Built-in MCP server id → translation key for a user-readable server name. */
export const MCP_SERVER_FRIENDLY_NAME_KEYS: Record<string, TranslationKeys> = {
  zyb_studio: 'com_ui_mcp_server_zyb_studio',
};

/** Studio MCP tool id → translation key for a user-readable action name. */
export const MCP_TOOL_FRIENDLY_NAME_KEYS: Record<string, TranslationKeys> = {
  search_library: 'com_ui_tool_name_search_library',
  compare_materials: 'com_ui_tool_name_compare_materials',
  select_region: 'com_ui_tool_name_select_region',
  edit_region: 'com_ui_tool_name_edit_region',
  recover_region_edit: 'com_ui_tool_name_recover_region_edit',
  finish_region_edit: 'com_ui_tool_name_recover_region_edit',
  create_3d_model: 'com_ui_tool_name_create_3d_model',
  check_3d_model: 'com_ui_tool_name_check_3d_model',
};

export function getMCPServerDisplayLabel(
  serverName: string | undefined,
  localize: (key: TranslationKeys) => string,
): string {
  if (!serverName) {
    return '';
  }
  const key = MCP_SERVER_FRIENDLY_NAME_KEYS[serverName];
  return key ? localize(key) : serverName;
}

export function getMCPToolDisplayLabel(
  toolName: string | undefined,
  localize: (key: TranslationKeys) => string,
): string {
  if (!toolName) {
    return '';
  }
  const key = MCP_TOOL_FRIENDLY_NAME_KEYS[toolName];
  return key ? localize(key) : toolName;
}

export interface ParsedToolName {
  /** Original tool id (unchanged). */
  raw: string;
  /** MCP server name when the tool follows `<tool>_mcp_<server>`, else `''`. */
  mcpServer: string;
  /** Tool-specific name — the `<tool>` half of an MCP id, or the raw
   *  name for native tools. Useful as the "action" label shown in a
   *  code-style badge next to the server name. */
  toolName: string;
  /** Translation key for a user-friendly display name, when the raw
   *  name matches a built-in native tool (web_search, execute_code, …).
   *  Absent for MCP tools and unknown names. */
  friendlyKey?: TranslationKeys;
}

/**
 * Split an incoming tool id into its constituent parts:
 *
 *   - `search_code_mcp_github` → `{ mcpServer: 'github', toolName: 'search_code' }`
 *   - `web_search`             → `{ mcpServer: '', toolName: 'web_search', friendlyKey: 'com_ui_tool_name_web_search' }`
 *   - `some_custom_tool`       → `{ mcpServer: '', toolName: 'some_custom_tool' }`
 */
export function parseToolName(
  rawName: string,
  knownServerNames?: readonly string[],
): ParsedToolName {
  if (rawName.includes(Constants.mcp_delimiter)) {
    const [toolName, mcpServer = ''] = splitToolCallName(rawName, knownServerNames);
    return { raw: rawName, mcpServer, toolName };
  }
  const friendlyKey = TOOL_FRIENDLY_NAME_KEYS[rawName] ?? MCP_TOOL_FRIENDLY_NAME_KEYS[rawName];
  return {
    raw: rawName,
    mcpServer: '',
    toolName: rawName,
    ...(friendlyKey ? { friendlyKey } : {}),
  };
}

/**
 * Resolve a tool id to a single user-facing short label. Pure string —
 * use `parseToolName` directly when you need structured parts (e.g. to
 * render a code-style badge for the tool name next to the server).
 *
 *   - MCP tool  → localized friendly server name (or raw server name)
 *   - Native   → localized friendly name from {@link TOOL_FRIENDLY_NAME_KEYS}
 *   - Unknown  → raw name
 */
export function getToolDisplayLabel(
  rawName: string,
  localize: (key: TranslationKeys) => string,
  knownServerNames?: readonly string[],
): string {
  const parsed = parseToolName(rawName, knownServerNames);
  if (parsed.mcpServer) return getMCPServerDisplayLabel(parsed.mcpServer, localize);
  if (parsed.friendlyKey) return localize(parsed.friendlyKey);
  return parsed.toolName;
}
