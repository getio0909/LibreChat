const fs = require('node:fs');
const path = require('node:path');
const yaml = require('js-yaml');
const { configSchema } = require('librechat-data-provider');

const mockGetConnection = jest.fn();
const mockUpdateMCPServerTools = jest.fn();

jest.mock('~/config', () => ({
  getMCPManager: jest.fn(() => ({
    getConnection: mockGetConnection,
    getToolPublicationGeneration: jest.fn(() => 'studio-generation'),
  })),
  getMCPServersRegistry: jest.fn(() => ({})),
  getFlowStateManager: jest.fn(() => ({})),
}));

jest.mock('~/models', () => ({
  findToken: jest.fn(),
  createToken: jest.fn(),
  updateToken: jest.fn(),
  deleteTokens: jest.fn(),
  findPluginAuthsByKeys: jest.fn(),
}));

jest.mock('~/server/services/Config', () => ({
  updateMCPServerTools: mockUpdateMCPServerTools,
  getMCPToolsCacheGeneration: jest.fn(async () => 'studio-generation'),
  getMCPServerTools: jest.fn(),
  cacheMCPServerTools: jest.fn(),
  invalidateCachedTools: jest.fn(),
}));

jest.mock('~/cache', () => ({ getLogStores: jest.fn(() => ({})) }));
jest.mock('~/cache/getLogStores', () => jest.fn(() => ({})));
jest.mock('~/server/services/MCPAuthorizationFenceRetry', () => ({
  persistMCPAuthorizationFenceRetry: jest.fn(),
  clearMCPAuthorizationFenceRetry: jest.fn(),
}));
jest.mock('~/server/services/Schedules', () => ({
  recordMCPToolAuthFailure: jest.fn(),
}));

const { reinitMCPServer } = require('./mcp');

it('reinitializes the configured Studio server and returns its tools without a chat body', async () => {
  const raw = yaml.load(
    fs.readFileSync(path.resolve(__dirname, '../../../../librechat.yaml'), 'utf8'),
  );
  const serverConfig = configSchema.parse(raw).mcpServers.zyb_studio;
  const tools = [
    { name: 'edit_region', description: 'Edit', inputSchema: { type: 'object' } },
    { name: 'compare_materials', description: 'Compare', inputSchema: { type: 'object' } },
  ];
  mockGetConnection.mockResolvedValue({
    fetchToolsSnapshot: jest.fn(async () => ({ tools, complete: true })),
  });
  mockUpdateMCPServerTools.mockResolvedValue({ edit_region: {}, compare_materials: {} });

  const result = await reinitMCPServer({
    user: { id: 'studio-user' },
    serverName: 'zyb_studio',
    serverConfig,
    _flowManager: {},
  });

  expect(result.success).toBe(true);
  expect(result.connectionDeferred).toBeUndefined();
  expect(result.tools).toEqual(tools);
  expect(mockGetConnection).toHaveBeenCalledTimes(1);
  expect(mockUpdateMCPServerTools).toHaveBeenCalledWith(
    expect.objectContaining({ serverName: 'zyb_studio', tools }),
  );
});
