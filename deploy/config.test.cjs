const fs = require('node:fs');
const { test } = require('node:test');
const assert = require('node:assert/strict');
const yaml = require('js-yaml');
const {
  configSchema,
  EModelEndpoint,
  fileConfig: defaultFileConfig,
  mergeFileConfig,
  getEndpointFileConfig,
  validateVisionModel,
} = require('librechat-data-provider');
const {
  getGoogleConfig,
  geminiToolkit,
  processMCPEnv,
  buildMCPImageToolContext,
  applyModelSpecPreset,
  initializeAgent,
  loadAgent,
  loadToolDefinitions,
  requiresEphemeralUserConnection,
  isChatSelectableMCPServer,
} = require('@librechat/api');
const {
  STUDIO_AGENT_PLACEHOLDERS,
  STUDIO_AGENT_PROVIDERS,
  STUDIO_AGENT_TOOLS,
  buildStudioAgentDefinitions,
  isStudioOwnedAgent,
  findStudioOwnedAgent,
  collectPaginatedAgents,
} = require('./studio-agents.cjs');

const rawYaml = fs.readFileSync('librechat.yaml', 'utf8');
const parsedYaml = yaml.load(rawYaml);
const rawCompose = fs.readFileSync('deploy/compose.yml', 'utf8');
const parsedCompose = yaml.load(rawCompose);
const zhHansTranslations = JSON.parse(
  fs.readFileSync('client/src/locales/zh-Hans/translation.json', 'utf8'),
);

// 禁止在面向用户的名称、描述或提示词中出现的厂商与模型标识。
const VENDOR_NAME_PATTERN =
  /gemini|argon|claude|haiku|sonnet|opus|openrouter|seedream|seed3d|volcengine|google|anthropic|deepseek|doubao|\bark\b|openai|gpt/i;

// 禁止残留的旧版 CAD 服务或容器标识。
const LEGACY_CAD_PATTERN = /freecad|blender|design_tools|dufs|compile_drawing_sheet/i;

// 契约允许的内置生图工具、专精 MCP 工具名与参数字段集合（内置生图参数从真实 geminiToolkit schema 同步）。
const GEMINI_SCHEMA_KEYS = Object.keys(geminiToolkit.gemini_image_gen.schema.properties || {});
const CONTRACT_IDENTIFIERS = new Set([
  'gemini_image_gen',
  'select_region',
  'edit_region',
  'search_library',
  'compare_materials',
  'create_3d_model',
  'check_3d_model',
  ...GEMINI_SCHEMA_KEYS,
  'base_id',
  'selection_id',
  'instruction',
  'references',
  'id',
  'use',
  'query',
  'library_type',
  'role',
  'region',
  'limit',
  'include_flagged',
  'target',
  'materials',
  'kb_',
  'image_id',
  'detail',
  'note',
  'job_id',
]);

test('librechat.yaml passes LibreChat configSchema validation', () => {
  const result = configSchema.safeParse(parsedYaml);
  assert.equal(
    result.success,
    true,
    result.success ? '' : JSON.stringify(result.error.issues, null, 2),
  );
});

test('default spec is Gemini 4 Argon High and fast spec is OpenRouter Haiku 5.5 via saved Studio agents', () => {
  const config = configSchema.parse(parsedYaml);
  assert.equal(config.interface.agents.use, true);
  assert.equal(config.endpoints.agents.disableBuilder, true);
  assert.equal(config.modelSpecs.enforce, true);
  assert.equal(config.modelSpecs.prioritize, true);
  assert.equal(config.modelSpecs.list.length, 2);

  const [defaultSpec, fastSpec] = config.modelSpecs.list;
  assert.equal(defaultSpec.name, 'zyb-design');
  assert.equal(defaultSpec.default, true);
  assert.equal(defaultSpec.iconURL, '/assets/zybc-seal.svg');
  assert.equal(defaultSpec.hideBadgeRow, true);
  assert.equal(defaultSpec.preset.endpoint, 'agents');
  assert.equal(defaultSpec.preset.agent_id, STUDIO_AGENT_PLACEHOLDERS['zyb-design']);
  assert.equal(defaultSpec.preset.model, 'gemini-4-argon');
  assert.equal(defaultSpec.preset.iconURL, '/assets/zybc-seal.svg');
  assert.equal(defaultSpec.preset.thinking, true);
  assert.equal(defaultSpec.preset.thinkingLevel, 'high');
  assert.equal(defaultSpec.preset.thinkingBudget, undefined);
  assert.equal(defaultSpec.preset.maxContextTokens, 200000);
  assert.equal(defaultSpec.preset.maxOutputTokens, 65536);

  // 验证 Google LLM 配置为 Gemini 4 Argon 生成 HIGH thinkingLevel 且不发送 thinkingBudget。
  const { llmConfig } = getGoogleConfig(
    'fixture-google-key',
    {
      modelOptions: {
        model: defaultSpec.preset.model,
        thinking: defaultSpec.preset.thinking,
        thinkingLevel: defaultSpec.preset.thinkingLevel,
      },
    },
    true,
  );
  assert.deepEqual(llmConfig.thinkingConfig, {
    thinkingLevel: 'HIGH',
    includeThoughts: true,
  });
  assert.equal(llmConfig.thinkingConfig.thinkingBudget, undefined);

  assert.equal(fastSpec.name, 'zyb-design-fast');
  assert.equal(fastSpec.default, false);
  assert.equal(fastSpec.iconURL, '/assets/zybc-seal.svg');
  assert.equal(fastSpec.hideBadgeRow, true);
  assert.equal(fastSpec.preset.endpoint, 'agents');
  assert.equal(fastSpec.preset.agent_id, STUDIO_AGENT_PLACEHOLDERS['zyb-design-fast']);
  assert.equal(fastSpec.preset.model, 'anthropic/claude-haiku-5.5');
  assert.equal(fastSpec.preset.iconURL, '/assets/zybc-seal.svg');
  assert.equal(fastSpec.preset.maxContextTokens, 200000);
  assert.equal(fastSpec.preset.max_tokens, 32000);

  // 验证 OpenRouter 与轻量级标题端点配置。
  const openRouterEndpoint = config.endpoints.custom.find((ep) => ep.name === 'OpenRouter');
  const titleEndpoint = config.endpoints.custom.find((ep) => ep.name === 'OpenRouterTitles');
  assert.ok(openRouterEndpoint, 'OpenRouter custom endpoint must exist');
  assert.ok(titleEndpoint, 'OpenRouterTitles custom endpoint must exist');
  assert.equal(openRouterEndpoint.iconURL, '/assets/zybc-seal.svg');
  assert.equal(titleEndpoint.iconURL, '/assets/zybc-seal.svg');
  assert.equal(openRouterEndpoint.titleConvo, true);
  assert.equal(openRouterEndpoint.titleEndpoint, 'OpenRouterTitles');
  assert.deepEqual(openRouterEndpoint.models.default, ['anthropic/claude-haiku-5.5']);
  assert.ok(config.endpoints.all?.titlePrompt?.includes('{convo}'));
  assert.ok(config.endpoints.google?.titlePrompt?.includes('{convo}'));
  assert.ok(openRouterEndpoint.titlePrompt?.includes('{convo}'));
});

test('both modelSpecs compute large effective context budgets via initializeAgent and never collapse to 1024', async () => {
  const config = configSchema.parse(parsedYaml);
  const studioAgentDefs = new Map(
    buildStudioAgentDefinitions(config).map((entry) => [entry.placeholder, entry.agent]),
  );
  const previousGoogleKey = process.env.GOOGLE_KEY;
  const previousOpenRouterKey = process.env.OPENROUTER_API_KEY;
  process.env.GOOGLE_KEY = 'fixture-google-key';
  process.env.OPENROUTER_API_KEY = 'fixture-openrouter-key';

  try {
    const appConfig = {
      endpoints: {
        ...config.endpoints,
        custom: config.endpoints.custom,
      },
    };
    const db = {
      getProjectFiles: async () => [],
      getFiles: async () => [],
      getConvoFiles: async () => [],
      updateFilesUsage: async () => [],
      getUserKey: async () => 'fixture-key',
      getUserKeyValues: async () => ({ apiKey: 'fixture-key' }),
      getToolFilesByIds: async () => [],
    };
    const loadTools = async () => ({
      tools: [],
      toolContextMap: {},
      dynamicToolContextMap: {},
      toolDefinitions: [],
      hasDeferredTools: false,
    });

    // 预估系统提示词与 8 个 MCP 工具 schema 的总词元开销上限（实测约 1,813 tokens，此处按 5,000 保守预留）。
    const instructionAndToolTokensReserve = 5000;

    for (const spec of config.modelSpecs.list) {
      const endpoint = spec.preset.endpoint;
      const endpointType = EModelEndpoint.agents;
      const { parsedBody } = applyModelSpecPreset({
        modelSpec: spec,
        parsedBody: { spec: spec.name },
        endpoint,
        endpointType,
        includePresetDefaults: true,
      });
      const {
        spec: _s,
        iconURL: _i,
        agent_id,
        chatProjectId: _c,
        ...model_parameters
      } = parsedBody;
      const agent = await loadAgent(
        {
          req: { user: { id: 'user-1' }, config: appConfig, body: {} },
          spec: spec.name,
          agent_id,
          endpoint,
          model_parameters,
        },
        {
          getAgent: async ({ id }) => structuredClone(studioAgentDefs.get(id) ?? null),
          getMCPServerTools: async () => ({}),
        },
      );
      assert.ok(agent, `loadAgent must resolve saved Studio agent for ${spec.name}`);
      assert.equal(agent.provider, STUDIO_AGENT_PROVIDERS[spec.name]);

      const initialized = await initializeAgent(
        {
          req: { user: { id: 'user-1' }, config: appConfig, body: {} },
          res: {},
          agent,
          loadTools,
          endpointOption: { endpoint, endpointType, model_parameters: {} },
          allowedProviders: new Set([agent.provider, 'google', 'openAI', 'custom', 'OpenRouter']),
          isInitialAgent: true,
        },
        db,
      );

      assert.notEqual(
        initialized.maxContextTokens,
        1024,
        `${spec.name} maxContextTokens must not collapse to the 1024 floor`,
      );
      assert.ok(
        initialized.baseContextTokens >= 100000,
        `${spec.name} baseContextTokens must remain >= 100k, got ${initialized.baseContextTokens}`,
      );
      const availableForMessages = initialized.maxContextTokens - instructionAndToolTokensReserve;
      assert.ok(
        availableForMessages >= 100000,
        `${spec.name} availableForMessages must be at least 100k, got ${availableForMessages}`,
      );

      if (spec.name === 'zyb-design') {
        // gemini-4-argon：上下文上限 200,000 - 最大输出 65,536 = 134,464 可用于输入
        assert.equal(initialized.maxContextTokens, 200000);
        assert.equal(initialized.baseContextTokens, 134464);
        assert.equal(initialized.model_parameters.maxOutputTokens, 65536);
      } else if (spec.name === 'zyb-design-fast') {
        // anthropic/claude-haiku-5.5：上下文上限 200,000 - 最大输出 32,000 = 168,000 可用于输入
        assert.equal(initialized.maxContextTokens, 200000);
        assert.equal(initialized.baseContextTokens, 168000);
        assert.equal(initialized.model_parameters.maxTokens, 32000);
      }
    }
  } finally {
    if (previousGoogleKey === undefined) {
      delete process.env.GOOGLE_KEY;
    } else {
      process.env.GOOGLE_KEY = previousGoogleKey;
    }
    if (previousOpenRouterKey === undefined) {
      delete process.env.OPENROUTER_API_KEY;
    } else {
      process.env.OPENROUTER_API_KEY = previousOpenRouterKey;
    }
  }
});

test('saved Studio agents carry gemini_image_gen and sys__all__sys_mcp_zyb_studio and no CAD servers or prompts remain', async () => {
  const config = configSchema.parse(parsedYaml);
  assert.deepEqual(Object.keys(config.mcpServers), ['zyb_studio']);
  const studioAgentDefs = new Map(
    buildStudioAgentDefinitions(config).map((entry) => [entry.placeholder, entry.agent]),
  );
  const mockMcpTools = {
    select_region_mcp_zyb_studio: {
      function: { name: 'select_region_mcp_zyb_studio', description: 'select', parameters: {} },
    },
    edit_region_mcp_zyb_studio: {
      function: { name: 'edit_region_mcp_zyb_studio', description: 'edit', parameters: {} },
    },
    search_library_mcp_zyb_studio: {
      function: { name: 'search_library_mcp_zyb_studio', description: 'search', parameters: {} },
    },
    compare_materials_mcp_zyb_studio: {
      function: { name: 'compare_materials_mcp_zyb_studio', description: 'compare', parameters: {} },
    },
    create_3d_model_mcp_zyb_studio: {
      function: { name: 'create_3d_model_mcp_zyb_studio', description: '3d', parameters: {} },
    },
    check_3d_model_mcp_zyb_studio: {
      function: { name: 'check_3d_model_mcp_zyb_studio', description: 'check3d', parameters: {} },
    },
  };

  for (const spec of config.modelSpecs.list) {
    assert.equal(spec.preset.endpoint, 'agents');
    assert.equal(spec.preset.agent_id, STUDIO_AGENT_PLACEHOLDERS[spec.name]);
    const loadedAgent = await loadAgent(
      {
        req: { user: { id: 'user-1' }, config: { modelSpecs: config.modelSpecs }, body: {} },
        spec: spec.name,
        agent_id: spec.preset.agent_id,
        endpoint: spec.preset.endpoint,
        model_parameters: { model: spec.preset.model },
      },
      {
        getAgent: async ({ id }) => structuredClone(studioAgentDefs.get(id) ?? null),
        getMCPServerTools: async () => mockMcpTools,
      },
    );
    assert.ok(loadedAgent);
    assert.deepEqual(loadedAgent.tools, [...STUDIO_AGENT_TOOLS]);

    const { toolDefinitions } = await loadToolDefinitions(
      {
        userId: 'user-1',
        agentId: loadedAgent.id,
        tools: loadedAgent.tools,
        mcpServerNames: ['zyb_studio'],
        rawServerNames: ['zyb_studio'],
      },
      {
        getOrFetchMCPServerTools: async (_userId, serverName) =>
          serverName === 'zyb_studio' ? mockMcpTools : null,
        isBuiltInTool: (toolName) => toolName === 'gemini_image_gen',
      },
    );
    const resolvedNames = toolDefinitions.map((def) => def.name);
    assert.ok(resolvedNames.includes('gemini_image_gen'));
    assert.ok(resolvedNames.includes('select_region_mcp_zyb_studio'));
    assert.ok(resolvedNames.includes('edit_region_mcp_zyb_studio'));
  }
  assert.doesNotMatch(rawYaml, LEGACY_CAD_PATTERN);
  assert.doesNotMatch(rawCompose, LEGACY_CAD_PATTERN);
});

test('customer-facing labels, descriptions, welcome text, and promptPrefix contain no vendor names', () => {
  const config = configSchema.parse(parsedYaml);
  assert.doesNotMatch(config.interface.customWelcome || '', VENDOR_NAME_PATTERN);
  assert.equal(parsedYaml.interface.customFooter, '庄严宝刹 AI 设计助手 · 仅供内部概念设计参考');
  assert.equal(
    parsedCompose.services.api.environment.CUSTOM_FOOTER,
    '庄严宝刹 AI 设计助手 · 仅供内部概念设计参考',
  );
  assert.doesNotMatch(parsedYaml.interface.customFooter, VENDOR_NAME_PATTERN);
  assert.doesNotMatch(parsedYaml.interface.customFooter, /librechat|every ai for everyone/i);
  assert.doesNotMatch(config.interface.mcpServers?.placeholder || '', VENDOR_NAME_PATTERN);
  assert.doesNotMatch(config.mcpServers.zyb_studio.title || '', VENDOR_NAME_PATTERN);
  assert.doesNotMatch(config.endpoints.all?.titlePrompt || '', VENDOR_NAME_PATTERN);
  assert.doesNotMatch(config.endpoints.google?.titlePrompt || '', VENDOR_NAME_PATTERN);
  assert.match(
    zhHansTranslations.com_endpoint_message_placeholder || '',
    /[\u4e00-\u9fff]/,
    'zh-Hans composer placeholder must be Chinese',
  );
  assert.notEqual(zhHansTranslations.com_endpoint_message_placeholder, 'Message');

  for (const spec of config.modelSpecs.list) {
    assert.doesNotMatch(spec.label, VENDOR_NAME_PATTERN);
    assert.doesNotMatch(spec.description || '', VENDOR_NAME_PATTERN);
    assert.doesNotMatch(spec.preset.modelLabel || '', VENDOR_NAME_PATTERN);
    const prefix = spec.preset.promptPrefix || '';
    // 剔除固定内置工具标识符 gemini_image_gen 后，校验提示词正文不包含任何厂商或底层模型名称。
    assert.doesNotMatch(prefix.replaceAll('gemini_image_gen', ''), VENDOR_NAME_PATTERN);

    const chineseChars = (prefix.match(/[\u4e00-\u9fff]/g) || []).length;
    assert.ok(
      chineseChars > 0 && chineseChars <= 380,
      `Expected <= 380 Chinese characters, got ${chineseChars}`,
    );
    assert.ok(prefix.length <= 520, `Expected <= 520 total characters, got ${prefix.length}`);
    for (const domainRule of [
      '物体数量',
      '空间位置关系',
      '宗教符号',
      '文化身份',
      '资料依据',
      '可制造工艺',
    ]) {
      assert.ok(prefix.includes(domainRule), `promptPrefix must retain domain rule: ${domainRule}`);
    }
    for (const toolName of [
      'gemini_image_gen',
      'select_region',
      'edit_region',
      'search_library',
      'compare_materials',
      'create_3d_model',
      'check_3d_model',
    ]) {
      assert.ok(prefix.includes(toolName), `promptPrefix must reference ${toolName}`);
    }
    for (const removedToolName of [
      'design_concepts',
      'generate_image',
      'edit_image',
      'list_images',
    ]) {
      assert.ok(
        !prefix.includes(removedToolName),
        `promptPrefix must not reference removed duplicate tool ${removedToolName}`,
      );
    }
  }
});

test('promptPrefix and MCP image context mention only tool and argument names present in the contract', () => {
  const config = configSchema.parse(parsedYaml);
  assert.deepEqual(
    [...GEMINI_SCHEMA_KEYS].sort(),
    ['aspectRatio', 'imageSize', 'image_ids', 'prompt'],
  );
  for (const spec of config.modelSpecs.list) {
    const prefix = spec.preset.promptPrefix || '';
    const identifiers = prefix.match(/[A-Za-z_][A-Za-z0-9_]*/g) || [];
    for (const identifier of identifiers) {
      assert.ok(
        CONTRACT_IDENTIFIERS.has(identifier),
        `Non-contract identifier "${identifier}" found in promptPrefix of ${spec.name}`,
      );
    }
    assert.ok(prefix.includes('prompt'), 'promptPrefix must mention prompt');
    assert.ok(prefix.includes('image_ids'), 'promptPrefix must mention image_ids');
    assert.ok(prefix.includes('aspectRatio'), 'promptPrefix must mention aspectRatio');
    assert.ok(prefix.includes('imageSize'), 'promptPrefix must mention imageSize');
    assert.ok(!prefix.includes('aspect_ratio'), 'promptPrefix must not mention aspect_ratio');
    assert.ok(!prefix.includes('resolution'), 'promptPrefix must not mention resolution');
    assert.ok(prefix.includes('base_id'), 'promptPrefix must mention base_id');
    assert.ok(prefix.includes('selection_id'), 'promptPrefix must mention selection_id');
    assert.ok(prefix.includes('instruction'), 'promptPrefix must mention instruction');
    assert.ok(prefix.includes('references'), 'promptPrefix must mention references');
    assert.ok(prefix.includes('kb_'), 'promptPrefix must mention kb_ IDs');
    assert.ok(prefix.includes('target'), 'promptPrefix must mention compare_materials target');
    assert.ok(
      prefix.includes('materials'),
      'promptPrefix must mention compare_materials materials',
    );
    assert.ok(prefix.includes('image_id'), 'promptPrefix must mention image_id');
    assert.ok(prefix.includes('create_3d_model'), 'promptPrefix must mention create_3d_model');
    assert.ok(prefix.includes('check_3d_model'), 'promptPrefix must mention check_3d_model');
    assert.doesNotMatch(prefix, /\b(reference_uses|reference_ids|aspect_ratio|resolution)\b/);
  }

  const mcpContext = buildMCPImageToolContext({
    imageFiles: [{ file_id: 'img-1', filename: 'booth.png', kind: 'upload', turn: 1 }],
  });
  assert.ok(mcpContext.includes('- img-1 (booth.png, upload, turn 1)'));
  const backtickTokens = [...mcpContext.matchAll(/`([^`]+)`/g)].map((m) => m[1]);
  assert.deepEqual(backtickTokens, [
    'gemini_image_gen.image_ids',
    'select_region.image_id',
    'edit_region.base_id',
    'compare_materials.base_id',
    'references[].id',
    'references[].use',
    'create_3d_model.image_id',
  ]);
  for (const token of backtickTokens) {
    for (const part of token.split(/[.[\]]+/).filter(Boolean)) {
      assert.ok(
        CONTRACT_IDENTIFIERS.has(part),
        `Non-contract identifier "${part}" found in buildMCPImageToolContext`,
      );
    }
  }
});

test('zyb_studio MCP headers, placeholders, and allowedAddresses match the contract', () => {
  const config = configSchema.parse(parsedYaml);
  assert.ok(config.mcpSettings.allowedAddresses.includes('studio:8000'));
  assert.equal(config.mcpSettings.apps, true);

  const studioServer = config.mcpServers.zyb_studio;
  assert.equal(studioServer.type, 'streamable-http');
  assert.equal(studioServer.url, 'http://studio:8000/mcp');
  assert.equal(studioServer.title, '设计工具');
  assert.equal(studioServer.chatMenu, false);
  assert.equal(studioServer.requiresOAuth, false);
  assert.equal(studioServer.timeout, 900000);
  assert.equal(isChatSelectableMCPServer(studioServer), false);
  assert.equal(requiresEphemeralUserConnection(studioServer), false);
  assert.deepEqual(studioServer.headers, {
    'X-LibreChat-User-ID': '{{LIBRECHAT_USER_ID}}',
    'X-Studio-Token': '${STUDIO_SHARED_TOKEN}',
  });

  // 验证 processMCPEnv 能正确替换用户 ID 与共享令牌环境变量，且不含请求体占位符。
  const previousToken = process.env.STUDIO_SHARED_TOKEN;
  process.env.STUDIO_SHARED_TOKEN = 'test-shared-secret-token';
  try {
    const resolved = processMCPEnv({
      options: studioServer,
      user: { id: 'user-123' },
      body: { conversationId: 'conv-456' },
    });
    assert.deepEqual(resolved.headers, {
      'X-LibreChat-User-ID': 'user-123',
      'X-Studio-Token': 'test-shared-secret-token',
    });
  } finally {
    if (previousToken === undefined) {
      delete process.env.STUDIO_SHARED_TOKEN;
    } else {
      process.env.STUDIO_SHARED_TOKEN = previousToken;
    }
  }
});

test('fileConfig permits image uploads on agents, google, and OpenRouter endpoints and preserves 100% generated image resolution', async () => {
  const config = configSchema.parse(parsedYaml);
  const merged = mergeFileConfig(config.fileConfig);

  assert.deepEqual(config.fileConfig.imageGeneration, { percentage: 100 });

  // 验证 2400×1792 的 2K 生图在配置了 imageGeneration.percentage: 100 后不会再被缩放到 1029×768。
  const sharp = require('sharp');
  const { resizeImageBuffer } = require('../api/server/services/Files/images/resize');
  const fixtureBuffer = await sharp({
    create: {
      width: 2400,
      height: 1792,
      channels: 4,
      background: { r: 120, g: 90, b: 60, alpha: 1 },
    },
  })
    .png()
    .toBuffer();
  const resized = await resizeImageBuffer(
    fixtureBuffer,
    config.fileConfig.imageGeneration,
    EModelEndpoint.agents,
  );
  assert.equal(resized.width, 2400);
  assert.equal(resized.height, 1792);
  assert.equal(resized.type, 'image/png');

  for (const endpoint of ['agents', 'google', 'OpenRouter']) {
    const endpointConfig = getEndpointFileConfig({
      fileConfig: merged,
      endpoint,
    });
    assert.equal(endpointConfig.disabled, false);
    for (const mimeType of ['image/jpeg', 'image/png', 'image/webp', 'image/gif']) {
      assert.equal(
        defaultFileConfig.checkType(mimeType, endpointConfig.supportedMimeTypes),
        true,
        `${endpoint} should support ${mimeType}`,
      );
    }
  }
});

test('deploy/compose.yml configures hardened studio service, IPv6 network, and api endpoint wiring', () => {
  const { services, volumes, networks } = parsedCompose;
  assert.ok(services.studio, 'studio service must be defined in deploy/compose.yml');
  assert.ok('studio-data' in volumes, 'studio-data volume must be defined in deploy/compose.yml');
  assert.ok(networks && networks.default, 'default network must be defined in deploy/compose.yml');
  assert.equal(
    networks.default.enable_ipv6,
    true,
    'default compose network must enable IPv6 for Ark reachability',
  );
  assert.equal(
    studioNetworkUsesDefault(services.studio),
    true,
    'studio service must use the IPv6-enabled default compose network',
  );

  const studio = services.studio;
  assert.equal(studio.image, '${STUDIO_IMAGE:?Set STUDIO_IMAGE in image.env}');
  assert.equal(studio.user, '10001:10001');
  assert.equal(studio.read_only, true);
  assert.deepEqual(studio.tmpfs, ['/tmp']);
  assert.deepEqual(studio.cap_drop, ['ALL']);
  assert.deepEqual(studio.security_opt, ['no-new-privileges:true']);
  assert.equal(studio.mem_limit, '3g');
  assert.deepEqual(studio.ports, ['127.0.0.1:8000:8000']);
  assert.doesNotMatch(rawCompose, /STUDIO_PORT/);
  assert.ok(studio.volumes.includes('./studio.json:/run/secrets/studio.json:ro'));
  assert.ok(studio.volumes.includes('images:/librechat/images:ro'));
  assert.ok(studio.volumes.includes('studio-data:/data'));
  assert.ok(studio.healthcheck, 'studio healthcheck must be defined');

  const api = services.api;
  assert.equal(api.environment.ENDPOINTS, 'agents,custom,google');
  assert.equal(api.environment.GOOGLE_MODELS, 'gemini-4-argon');
  assert.equal(
    api.environment.GEMINI_IMAGE_MODEL,
    '${GEMINI_IMAGE_MODEL:-gemini-nano-banana-2.1}',
  );
  assert.equal(api.depends_on.studio.condition, 'service_healthy');
});

// 校验服务未挂载到禁用IPv6的独立网络而是使用默认IPv6网络
function studioNetworkUsesDefault(serviceConfig) {
  if (!serviceConfig.networks) {
    return true;
  }
  if (Array.isArray(serviceConfig.networks)) {
    return serviceConfig.networks.includes('default');
  }
  return Object.prototype.hasOwnProperty.call(serviceConfig.networks, 'default');
}

test('deploy/nginx.conf routes /studio-files/ to studio:8000/files/ with Range forwarding and matches compose host port', () => {
  const rawNginx = fs.readFileSync('deploy/nginx.conf', 'utf8');
  assert.match(rawNginx, /location \/studio-files\/\s*\{/);
  assert.match(rawNginx, /proxy_pass http:\/\/127\.0\.0\.1:8000\/files\/;/);
  assert.match(rawNginx, /proxy_set_header Range \$http_range;/);

  // 验证 compose.yml 绑定的宿主机回环地址与 nginx.conf 的反向代理上游地址完全一致且不可被环境变量偏移。
  const composePortBinding = parsedCompose.services.studio.ports[0];
  const nginxProxyMatch = rawNginx.match(/proxy_pass http:\/\/(127\.0\.0\.1:\d+)\/files\/;/);
  assert.ok(nginxProxyMatch, 'nginx.conf must proxy /studio-files/ to 127.0.0.1:<port>/files/');
  assert.equal(composePortBinding, `${nginxProxyMatch[1]}:8000`);
});

test('studio Dockerfile copies knowledge/ to /kb while LibreChat app image excludes knowledge/', () => {
  const studioDockerfile = fs.readFileSync('deploy/studio/Dockerfile', 'utf8');
  assert.match(studioDockerfile, /COPY --chmod=0555 knowledge\/records \/kb\/records/);
  assert.match(
    studioDockerfile,
    /COPY --chmod=0555 knowledge\/assets\/ref2048 \/kb\/assets\/ref2048/,
  );
  assert.match(
    studioDockerfile,
    /COPY --chmod=0555 knowledge\/assets\/thumb512 \/kb\/assets\/thumb512/,
  );
  assert.match(studioDockerfile, /COPY --chmod=0555 knowledge\/catalog \/kb\/catalog/);
  assert.match(studioDockerfile, /facebook\/sam2\.1-hiera-tiny/);
  assert.match(studioDockerfile, /de431c4043854a71d8101e17995dfe596bf101a5/);
  assert.match(
    studioDockerfile,
    /48c14467e5cf9e51870511feb72c89688e82dd74523142c0538b663e193ac2a7/,
  );
  assert.doesNotMatch(studioDockerfile, /kb\.lock|zybc-knowledge|git clone|curl|wget/i);

  const studioDockerignore = fs.readFileSync('deploy/studio/Dockerfile.dockerignore', 'utf8');
  assert.match(studioDockerignore, /^!knowledge\/records\/\*\*$/m);
  assert.match(studioDockerignore, /^!knowledge\/assets\/ref2048\/\*\*$/m);
  assert.match(studioDockerignore, /^!knowledge\/assets\/thumb512\/\*\*$/m);
  assert.match(studioDockerignore, /^!knowledge\/catalog\/\*\*$/m);

  const rootDockerignore = fs.readFileSync('.dockerignore', 'utf8');
  assert.match(rootDockerignore, /^knowledge$/m);

  const appDockerfileMulti = fs.readFileSync('Dockerfile.multi', 'utf8');
  assert.doesNotMatch(appDockerfileMulti, /\bknowledge\b/);
});

test('CI workflow builds studio image with root context, runs Python 3.13 and knowledge checks, and runs smoke test', () => {
  const rawWorkflow = fs.readFileSync('.github/workflows/development.yml', 'utf8');
  const workflow = yaml.load(rawWorkflow);
  assert.doesNotMatch(rawWorkflow, LEGACY_CAD_PATTERN);
  assert.equal(
    fs.existsSync('knowledge/.github'),
    false,
    'knowledge/.github must be removed after moving KB checks to root CI',
  );
  assert.ok(
    workflow.on.pull_request.paths.includes('knowledge/**'),
    'CI pull_request paths must include knowledge/**',
  );

  const checkSteps = workflow.jobs.checks.steps;
  const pythonSetup = checkSteps.find(
    (step) => step.uses && step.uses.startsWith('actions/setup-python@'),
  );
  assert.ok(pythonSetup, 'CI checks must set up Python');
  assert.equal(pythonSetup.with['python-version'], '3.13');
  assert.ok(
    checkSteps.some((step) => step.run && step.run.includes('pytest deploy/studio')),
    'CI checks must run pytest deploy/studio',
  );
  assert.ok(
    checkSteps.some((step) => step.run && step.run.includes('python knowledge/tools/validate.py')),
    'CI checks must run python knowledge/tools/validate.py',
  );
  assert.ok(
    checkSteps.some((step) => step.run && step.run.includes('pytest knowledge/tests')),
    'CI checks must run pytest knowledge/tests',
  );
  assert.ok(
    checkSteps.some((step) => step.run && step.run.includes('node --test deploy/*.test.cjs')),
    'CI checks must run node --test deploy/*.test.cjs',
  );

  const imageSteps = workflow.jobs.image.steps;
  const studioBuild = imageSteps.find((step) => step.id === 'studio');
  assert.ok(studioBuild, 'CI image job must build studio image');
  assert.equal(studioBuild.with.context, '.');
  assert.equal(studioBuild.with.file, 'deploy/studio/Dockerfile');
  assert.match(
    studioBuild.with.tags,
    /ghcr\.io\/getio0909\/librechat-studio:sha-\$\{\{ github\.sha \}\}/,
  );
  assert.ok(
    imageSteps.some((step) => step.run && step.run.includes('bash deploy/runtime.test.sh')),
    'CI image job must run studio runtime smoke test',
  );

  const rawSmokeTest = fs.readFileSync('deploy/runtime.test.sh', 'utf8');
  assert.match(rawSmokeTest, /assert health == "OK"/);
  assert.match(rawSmokeTest, /valid_user_id = "0123456789abcdef01234567"/);
  assert.match(rawSmokeTest, /"X-Studio-Token": "wrong-studio-token"/);
  assert.match(rawSmokeTest, /"X-LibreChat-User-ID": "smoke-user-1"/);
  assert.match(rawSmokeTest, /assert exc\.code == 403/);
  assert.match(rawSmokeTest, /"search_library"/);
  assert.match(rawSmokeTest, /"compare_materials"/);
  assert.match(rawSmokeTest, /"create_3d_model"/);
  assert.match(rawSmokeTest, /"check_3d_model"/);

  // 验证部署工作流显式传递 STUDIO_ENABLE_3D（J5005 测试环境默认 true），且仅在启用 3D 时强制要求 ARK_API_KEY。
  const deployStep = workflow.jobs.deploy.steps.find(
    (step) => step.name === 'Deploy the published image',
  );
  assert.ok(deployStep, 'CI deploy job must have Deploy the published image step');
  assert.equal(deployStep.env.STUDIO_ENABLE_3D, "${{ vars.STUDIO_ENABLE_3D || 'true' }}");
  assert.doesNotMatch(deployStep.run, /for name in [^\n]*\bARK_API_KEY\b/);
  assert.match(deployStep.run, /STUDIO_ENABLE_3D.*==.*"true".*ARK_API_KEY/);
});

test('gemini-4-argon is recognized as multimodal and formats image blocks, tool image results, and thought signatures without throwing', async () => {
  // 校验 data-provider 视觉模型白名单与 @librechat/agents 补丁挂载
  assert.equal(
    validateVisionModel({ model: 'gemini-4-argon' }),
    true,
    'validateVisionModel must recognize gemini-4-argon',
  );

  const rootPkg = JSON.parse(fs.readFileSync('package.json', 'utf8'));
  const apiPkg = JSON.parse(fs.readFileSync('api/package.json', 'utf8'));
  const packagesApiPkg = JSON.parse(fs.readFileSync('packages/api/package.json', 'utf8'));
  assert.equal(rootPkg.scripts.postinstall, 'patch-package --error-on-fail');
  assert.ok(rootPkg.dependencies['patch-package'], 'patch-package must be in root dependencies');
  assert.equal(apiPkg.dependencies['@librechat/agents'], '4.0.4');
  assert.equal(packagesApiPkg.peerDependencies['@librechat/agents'], '4.0.4');
  assert.equal(fs.existsSync('patches/@librechat+agents+4.0.4.patch'), true);

  const dockerfile = fs.readFileSync('Dockerfile', 'utf8');
  const dockerfileMulti = fs.readFileSync('Dockerfile.multi', 'utf8');
  assert.match(dockerfile, /COPY --chown=node:node patches \.\/patches/);
  assert.match(dockerfileMulti, /COPY patches \.\/patches/);

  // 校验 CustomChatGoogleGenerativeAI 对 gemini-4-argon 的多模态请求、工具图像结果与 thoughtSignature 处理
  const { CustomChatGoogleGenerativeAI } = require('@librechat/agents/llm/google');
  const { HumanMessage, AIMessage, ToolMessage } = require('@langchain/core/messages');

  const llm = new CustomChatGoogleGenerativeAI({
    model: 'gemini-4-argon',
    apiKey: 'test-api-key',
  });
  assert.equal(llm._isMultimodalModel, true);

  let capturedRequest = null;
  llm.client.generateContent = async (req) => {
    capturedRequest = req;
    return {
      response: {
        candidates: [
          {
            content: { role: 'model', parts: [{ text: 'ok' }] },
            finishReason: 'STOP',
          },
        ],
        usageMetadata: { promptTokenCount: 10, candidatesTokenCount: 5, totalTokenCount: 15 },
      },
    };
  };

  const userImg = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
  const toolImg = 'UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAwA0JaQAA3AA/vuUAAA=';

  await llm._generate(
    [
      new HumanMessage({
        content: [
          { type: 'text', text: '分析上传图并检索知识库' },
          { type: 'image_url', image_url: `data:image/png;base64,${userImg}` },
        ],
      }),
      new AIMessage({
        content: '',
        tool_calls: [
          {
            id: 'call_1',
            name: 'search_library_mcp_zyb_studio',
            args: { query: '须弥座' },
            type: 'tool_call',
          },
          {
            id: 'call_2',
            name: 'generate_image_mcp_zyb_studio',
            args: { prompt: '供桌' },
            type: 'tool_call',
          },
        ],
        additional_kwargs: {
          __gemini_function_call_thought_signatures__: {
            call_1: 'explicit_thought_sig_1',
          },
        },
      }),
      new ToolMessage({
        tool_call_id: 'call_1',
        name: 'search_library_mcp_zyb_studio',
        content: [
          { type: 'text', text: '{"count":1}' },
          { type: 'image_url', image_url: `data:image/webp;base64,${toolImg}` },
        ],
      }),
      new ToolMessage({
        tool_call_id: 'call_2',
        name: 'generate_image_mcp_zyb_studio',
        content: [
          { type: 'text', text: '{"id":"img_1234"}' },
          { type: 'image_url', image_url: `data:image/webp;base64,${toolImg}` },
        ],
      }),
    ],
    {},
  );

  assert.ok(capturedRequest, 'generateContent must be invoked with formatted request');
  assert.equal(capturedRequest.contents.length, 4);
  assert.deepEqual(capturedRequest.contents[0].parts[1], {
    inlineData: { mimeType: 'image/png', data: userImg },
  });
  assert.equal(capturedRequest.contents[1].parts[0].thoughtSignature, 'explicit_thought_sig_1');
  assert.ok(
    typeof capturedRequest.contents[1].parts[1].thoughtSignature === 'string' &&
      capturedRequest.contents[1].parts[1].thoughtSignature.length > 20,
    'missing thoughtSignature on gemini-4-argon must fall back to DUMMY_SIGNATURE',
  );
  assert.deepEqual(capturedRequest.contents[2].parts[0].functionResponse.response.result[1], {
    inlineData: { mimeType: 'image/webp', data: toolImg },
  });
});

test('zh-Hans locale translates designer-visible Projects sidebar and upstream model error strings', () => {
  // 校验简体中文语言包覆盖项目侧栏与上游模型错误提示键
  const zhTranslation = JSON.parse(
    fs.readFileSync('client/src/locales/zh-Hans/translation.json', 'utf8'),
  );
  assert.equal(zhTranslation.com_ui_projects, '项目');
  assert.equal(zhTranslation.com_ui_project, '项目');
  assert.equal(zhTranslation.com_ui_create_project, '创建项目');
  assert.equal(zhTranslation.com_ui_new_chat_in_project, '在 {{name}} 中新建对话');
  assert.equal(zhTranslation.com_error_upstream_model, '模型服务暂时无法完成此请求。');
  assert.equal(
    zhTranslation.com_error_upstream_model_status,
    '模型服务暂时无法完成此请求（状态码 {{0}}）。',
  );
});

test('Studio agent discovery paginates past 100 agents and never matches a non-Studio same-named agent', async () => {
  const page1Agents = Array.from({ length: 100 }, (_, idx) => ({
    id: `agent_other_${idx}`,
    name: idx === 0 ? '庄严设计助手' : `其他助手 ${idx}`,
    tools: ['web_search'],
  }));
  const studioMain = {
    id: 'agent_studio_main_owned',
    name: '庄严设计助手',
    tools: [...STUDIO_AGENT_TOOLS],
  };
  const page2Agents = [studioMain];

  const allAgents = await collectPaginatedAgents(async ({ cursor }) => {
    if (!cursor) {
      return { data: page1Agents, has_more: true, after: 'cursor_100' };
    }
    return { data: page2Agents, has_more: false, after: null };
  });

  assert.equal(allAgents.length, 101);
  assert.equal(isStudioOwnedAgent(page1Agents[0], '庄严设计助手'), false);
  assert.equal(isStudioOwnedAgent(studioMain, '庄严设计助手'), true);

  const matched = findStudioOwnedAgent(allAgents, '庄严设计助手');
  assert.equal(matched?.id, 'agent_studio_main_owned');

  // 若仅有同名非 Studio 智能体，必须返回 null 以便新建而非覆盖
  assert.equal(findStudioOwnedAgent(page1Agents, '庄严设计助手'), null);
});
