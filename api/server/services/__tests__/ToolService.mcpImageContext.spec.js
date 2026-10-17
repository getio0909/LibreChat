const {
  Constants,
  Permissions,
  EToolResources,
  EModelEndpoint,
  PermissionTypes,
  AgentCapabilities,
} = require('librechat-data-provider');

const mockGetEndpointsConfig = jest.fn();
const mockGetAppConfig = jest.fn();
const mockGetMCPServerTools = jest.fn();
const mockGetCachedTools = jest.fn();
const mockLoadToolDefinitions = jest.fn();
const mockGetUserMCPAuthMap = jest.fn();
const mockLoadToolsUtil = jest.fn();
const mockGetRoleByName = jest.fn();
const mockResolveConfigServers = jest.fn();
const mockResolveMcpServerNames = jest.fn();
const mockUserCanUseMCPServers = jest.fn().mockResolvedValue(true);

jest.mock('~/server/services/Config', () => ({
  getEndpointsConfig: (...args) => mockGetEndpointsConfig(...args),
  getAppConfig: (...args) => mockGetAppConfig(...args),
  getMCPServerTools: (...args) => mockGetMCPServerTools(...args),
  getCachedTools: (...args) => mockGetCachedTools(...args),
}));

jest.mock('@librechat/api', () => ({
  ...jest.requireActual('@librechat/api'),
  loadToolDefinitions: (...args) => mockLoadToolDefinitions(...args),
  getUserMCPAuthMap: (...args) => mockGetUserMCPAuthMap(...args),
}));

jest.mock('~/app/clients/tools/util', () => ({
  loadTools: (...args) => mockLoadToolsUtil(...args),
}));

jest.mock('~/server/services/Tools/credentials', () => ({
  loadAuthValues: jest.fn().mockResolvedValue({}),
}));

jest.mock('~/server/services/Tools/search', () => ({
  createOnSearchResults: jest.fn(),
}));

jest.mock('~/server/services/Tools/mcp', () => ({
  reinitMCPServer: jest.fn(),
}));

jest.mock('~/server/services/Files/process', () => ({
  processFileURL: jest.fn(),
  uploadImageBuffer: jest.fn(),
}));

jest.mock('~/app/clients/tools/util/fileSearch', () => ({
  primeFiles: jest.fn().mockResolvedValue({}),
}));

jest.mock('~/server/services/Files/Code/process', () => ({
  primeFiles: jest.fn().mockResolvedValue({}),
}));

jest.mock('../ActionService', () => ({
  loadActionSets: jest.fn().mockResolvedValue([]),
  decryptMetadata: jest.fn(),
  createActionTool: jest.fn(),
  domainParser: jest.fn(),
  legacyDomainEncode: jest.fn(),
}));

jest.mock('~/server/services/Threads', () => ({
  recordUsage: jest.fn(),
}));

jest.mock('~/models', () => ({
  findPluginAuthsByKeys: jest.fn(),
  getRoleByName: (...args) => mockGetRoleByName(...args),
  setConvoLaneGit: jest.fn(),
  getConvoLaneContext: jest.fn(),
  reserveConvoLaneGitSeq: jest.fn(),
}));

jest.mock('~/config', () => ({
  getFlowStateManager: jest.fn(() => ({ getFlowState: jest.fn() })),
  getMCPServersRegistry: jest.fn(() => ({
    getServerConfig: jest.fn(),
  })),
}));

jest.mock('~/server/services/MCP', () => ({
  resolveConfigServers: (...args) => mockResolveConfigServers(...args),
  resolveMcpServerNames: (...args) => mockResolveMcpServerNames(...args),
  resolveMcpServerContext: async (...args) => {
    const configServers = (await mockResolveConfigServers(...args)) ?? {};
    const serverNames = Object.keys(configServers);
    return { configServers, serverNames, rawServerNames: serverNames };
  },
  resolveCollisionAuditNames: jest.fn(async ({ rawServerNames, accessibleServerNames }) => ({
    names: accessibleServerNames?.length ? accessibleServerNames : rawServerNames,
    complete: true,
  })),
  createMCPPermissionContext: jest.fn((req) => ({
    canUseServers: (user) => mockUserCanUseMCPServers(user, req),
  })),
  userCanUseMCPServers: mockUserCanUseMCPServers,
}));

jest.mock('~/cache', () => ({
  getLogStores: jest.fn(() => ({})),
}));

const { loadAgentTools } = require('../ToolService');

function buildRole() {
  return {
    permissions: {
      [PermissionTypes.FILE_SEARCH]: { [Permissions.USE]: true },
      [PermissionTypes.RUN_CODE]: { [Permissions.USE]: true },
      [PermissionTypes.WEB_SEARCH]: { [Permissions.USE]: true },
      [PermissionTypes.MCP_SERVERS]: { [Permissions.USE]: true },
    },
  };
}

function createEndpointsConfig(capabilities = [AgentCapabilities.tools]) {
  return {
    [EModelEndpoint.agents]: {
      capabilities,
    },
  };
}

function createMockReq(capabilities = [AgentCapabilities.tools]) {
  return {
    user: { id: 'user-123', role: 'USER' },
    config: {
      endpoints: createEndpointsConfig(capabilities),
      mcpConfig: {
        zyb_studio: {
          type: 'streamable-http',
          url: 'http://studio:8000/mcp',
        },
      },
    },
    body: {
      conversationId: 'conv-456',
    },
  };
}

// 验证挂载 MCP 工具且存在图片时动态注入图片 ID 与文件名上下文
describe('loadAgentTools — MCP image context', () => {
  const capabilities = [AgentCapabilities.tools];
  const mcpServerTool = `${Constants.mcp_server}${Constants.mcp_delimiter}zyb_studio`;
  const imageFiles = [
    { file_id: 'img-111', filename: 'stone_slab.png' },
    { file_id: 'img-222', filename: 'booth_ref.webp' },
  ];

  beforeEach(() => {
    jest.clearAllMocks();
    mockGetRoleByName.mockResolvedValue(buildRole());
    mockGetEndpointsConfig.mockResolvedValue(createEndpointsConfig(capabilities));
    mockGetAppConfig.mockResolvedValue({
      endpoints: createEndpointsConfig(capabilities),
      mcpConfig: {
        zyb_studio: {
          type: 'streamable-http',
          url: 'http://studio:8000/mcp',
        },
      },
    });
    mockResolveConfigServers.mockResolvedValue({
      zyb_studio: {
        type: 'streamable-http',
        url: 'http://studio:8000/mcp',
      },
    });
    mockResolveMcpServerNames.mockResolvedValue(['zyb_studio']);
    mockLoadToolDefinitions.mockResolvedValue({
      toolDefinitions: [
        {
          name: `select_region${Constants.mcp_delimiter}zyb_studio`,
          description: 'Select region on image',
        },
      ],
      toolRegistry: new Map(),
      hasDeferredTools: false,
    });
  });

  it.each([true, false])(
    'populates dynamicToolContextMap.mcp_image_context with neutral id (filename, kind, turn) list and contract args (definitionsOnly=%s)',
    async (definitionsOnly) => {
      if (!definitionsOnly) {
        mockLoadToolsUtil.mockResolvedValue({
          loadedTools: [{ name: `select_region${Constants.mcp_delimiter}zyb_studio`, mcp: true }],
          toolContextMap: {},
          dynamicToolContextMap: {},
        });
      }

      const mcpImageFiles = [
        { file_id: 'img-111', filename: 'stone_slab.png', kind: 'upload', turn: 1 },
        { file_id: 'img-gen-1', filename: 'concept_1.png', kind: 'generated', turn: 1 },
        { file_id: 'img-222', filename: 'booth_ref.webp', kind: 'upload', turn: 2 },
      ];

      const result = await loadAgentTools({
        req: createMockReq(capabilities),
        res: {},
        agent: { id: 'agent_mcp_img', tools: [mcpServerTool] },
        tool_resources: {
          [EToolResources.image_edit]: { files: imageFiles },
        },
        mcpImageFiles,
        definitionsOnly,
      });

      expect(result.dynamicToolContextMap?.mcp_image_context).toContain(
        '- img-111 (stone_slab.png, upload, turn 1)',
      );
      expect(result.dynamicToolContextMap?.mcp_image_context).toContain(
        '- img-gen-1 (concept_1.png, generated, turn 1)',
      );
      expect(result.dynamicToolContextMap?.mcp_image_context).toContain(
        '- img-222 (booth_ref.webp, upload, turn 2)',
      );
      expect(result.dynamicToolContextMap?.mcp_image_context).toContain(
        '`gemini_image_gen.image_ids`',
      );
      expect(result.dynamicToolContextMap?.mcp_image_context).toContain('`select_region.image_id`');
      expect(result.dynamicToolContextMap?.mcp_image_context).toContain('`edit_region.base_id`');
      expect(result.dynamicToolContextMap?.mcp_image_context).toContain(
        '`compare_materials.base_id`',
      );
      expect(result.dynamicToolContextMap?.mcp_image_context).toContain('`references[].id`');
      expect(result.dynamicToolContextMap?.mcp_image_context).toContain('`references[].use`');
      expect(result.dynamicToolContextMap?.mcp_image_context).toContain(
        '`create_3d_model.image_id`',
      );
      expect(result.dynamicToolContextMap?.mcp_image_context).not.toContain('edit_image.base_id');
      expect(result.dynamicToolContextMap?.mcp_image_context).not.toContain(
        'design_concepts.reference_ids',
      );
      expect(result.dynamicToolContextMap?.mcp_image_context).not.toContain('reference_uses');
    },
  );

  it('does not populate mcp_image_context when no MCP server is attached', async () => {
    const result = await loadAgentTools({
      req: createMockReq(capabilities),
      res: {},
      agent: { id: 'agent_no_mcp', tools: ['calculator'] },
      tool_resources: {
        [EToolResources.image_edit]: { files: imageFiles },
      },
      definitionsOnly: true,
    });

    expect(result.dynamicToolContextMap?.mcp_image_context).toBeUndefined();
  });

  it('does not populate mcp_image_context when no images are present', async () => {
    const result = await loadAgentTools({
      req: createMockReq(capabilities),
      res: {},
      agent: { id: 'agent_mcp_no_img', tools: [mcpServerTool] },
      tool_resources: {},
      definitionsOnly: true,
    });

    expect(result.dynamicToolContextMap?.mcp_image_context).toBeUndefined();
  });
});
