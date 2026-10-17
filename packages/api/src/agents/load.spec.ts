import { EModelEndpoint, parseEphemeralAgentId } from 'librechat-data-provider';
import type { LoadAgentDeps } from './load';
import { loadEphemeralAgent } from './load';
import { resolveSender } from './sender';

const deps: LoadAgentDeps = {
  getAgent: async () => null,
  getMCPServerTools: async () => null,
};

const baseReq = {
  user: { id: 'user-1' },
  config: {
    modelSpecs: { list: [{ name: 'my-opus-spec', label: 'Spec Label' }] },
  },
  body: {},
} as unknown as Parameters<typeof loadEphemeralAgent>[0]['req'];

async function idFor(modelParameters: Record<string, unknown>) {
  const agent = await loadEphemeralAgent(
    {
      req: baseReq,
      spec: 'my-opus-spec',
      endpoint: 'my-custom-endpoint',
      model_parameters: modelParameters as never,
    },
    deps,
  );
  return agent?.id;
}

/**
 * Documents the #14253 Bug 2 mechanism: the ephemeral agent id (LangGraph node /
 * HITL checkpoint namespace) is derived from `sender = modelLabel ?? modelSpec.label`.
 * When the resume drops `modelLabel`, the id drifts and the paused checkpoint can't be
 * re-entered. The fix keeps `modelLabel` across resume (RESUME_CONTEXT_KEYS), so the
 * original and resumed ids stay equal.
 */
describe('loadEphemeralAgent ephemeral id stability (#14253 Bug 2)', () => {
  test('id changes when modelLabel is lost vs preserved', async () => {
    const withLabel = await idFor({ model: 'claude-opus-4', modelLabel: 'My Opus' });
    const withoutLabel = await idFor({ model: 'claude-opus-4' });
    expect(withLabel).toBeTruthy();
    expect(withoutLabel).toBeTruthy();
    // Original turn (has modelLabel) vs a resume that dropped it → different namespace.
    expect(withLabel).not.toEqual(withoutLabel);
  });

  test('id is stable when modelLabel is preserved across turns', async () => {
    const a = await idFor({ model: 'claude-opus-4', modelLabel: 'My Opus' });
    const b = await idFor({ model: 'claude-opus-4', modelLabel: 'My Opus' });
    expect(a).toEqual(b);
  });
});

/** Custom endpoints carry their configured name in `endpoint` at runtime,
 *  which `TEndpointOption` types as `EModelEndpoint`. */
const customEndpointOption = {
  endpoint: 'my-custom-endpoint' as EModelEndpoint,
  model: 'claude-opus-4',
};

describe('loadEphemeralAgent → resolveSender parity', () => {
  test('the persisted sender matches the spec label encoded into the agent id', async () => {
    const agent = await loadEphemeralAgent(
      {
        req: baseReq,
        spec: 'my-opus-spec',
        endpoint: 'my-custom-endpoint',
        model_parameters: { model: 'claude-opus-4' } as never,
      },
      deps,
    );
    expect(agent?.id).toBeTruthy();
    const sender = resolveSender({
      agent: { id: agent?.id },
      specLabel: 'Spec Label',
      endpointOption: customEndpointOption,
    });
    expect(sender).toBe('Spec Label');
    expect(sender).toBe(parseEphemeralAgentId(agent?.id ?? '')?.sender);
  });

  test('a user modelLabel wins over the spec label in the persisted sender', async () => {
    const agent = await loadEphemeralAgent(
      {
        req: baseReq,
        spec: 'my-opus-spec',
        endpoint: 'my-custom-endpoint',
        model_parameters: { model: 'claude-opus-4', modelLabel: 'My Opus' } as never,
      },
      deps,
    );
    const sender = resolveSender({
      agent: { id: agent?.id },
      specLabel: 'Spec Label',
      endpointOption: { ...customEndpointOption, modelLabel: 'My Opus' },
    });
    expect(sender).toBe('My Opus');
    expect(sender).toBe(parseEphemeralAgentId(agent?.id ?? '')?.sender);
  });
});

// 验证 modelSpec.preset.tools 可同时挂载内置工具（如 gemini_image_gen）与 MCP 服务且不污染 model_parameters
describe('loadEphemeralAgent — modelSpec.preset.tools', () => {
  test('equips built-in tools from modelSpec.preset.tools alongside mcpServers and strips tools from model_parameters', async () => {
    const reqWithSpecTools = {
      user: { id: 'user-1' },
      config: {
        modelSpecs: {
          list: [
            {
              name: 'zyb-design',
              label: '展厅设计',
              mcpServers: ['zyb_studio'],
              preset: {
                endpoint: 'custom',
                model: 'gemini-3.1-pro',
                tools: ['gemini_image_gen', { pluginKey: 'calculator' }],
              },
            },
          ],
        },
      },
      body: {},
    } as unknown as Parameters<typeof loadEphemeralAgent>[0]['req'];

    const agent = await loadEphemeralAgent(
      {
        req: reqWithSpecTools,
        spec: 'zyb-design',
        endpoint: 'custom',
        model_parameters: {
          model: 'gemini-3.1-pro',
          tools: ['gemini_image_gen'],
        } as never,
      },
      {
        ...deps,
        getMCPServerTools: async () => ({
          select_region_mcp_zyb_studio: {},
          compare_materials_mcp_zyb_studio: {},
        }),
      },
    );

    expect(agent?.tools).toEqual([
      'gemini_image_gen',
      'calculator',
      'select_region_mcp_zyb_studio',
      'compare_materials_mcp_zyb_studio',
    ]);
    expect(agent?.model_parameters).not.toHaveProperty('tools');
  });
});

