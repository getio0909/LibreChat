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
  processMCPEnv,
  buildMCPImageToolContext,
  applyModelSpecPreset,
  initializeAgent,
  isChatSelectableMCPServer,
} = require('@librechat/api');

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

// 契约允许的工具名与参数字段集合（来自 p1-contract.md §2.3、WP-P2a 与 WP-P2b 扩展）。
const CONTRACT_IDENTIFIERS = new Set([
  'design_concepts',
  'generate_image',
  'edit_image',
  'list_images',
  'search_library',
  'compare_materials',
  'create_3d_model',
  'check_3d_model',
  'prompt',
  'base_id',
  'references',
  'id',
  'use',
  'reference_ids',
  'aspect_ratio',
  'resolution',
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

test('default spec is Gemini 4 Argon High and fast spec is OpenRouter Haiku 5.5', () => {
  const config = configSchema.parse(parsedYaml);
  assert.equal(config.modelSpecs.enforce, true);
  assert.equal(config.modelSpecs.prioritize, true);
  assert.equal(config.modelSpecs.list.length, 2);

  const [defaultSpec, fastSpec] = config.modelSpecs.list;
  assert.equal(defaultSpec.name, 'zyb-design');
  assert.equal(defaultSpec.default, true);
  assert.equal(defaultSpec.iconURL, '/assets/zybc-seal.svg');
  assert.equal(defaultSpec.hideBadgeRow, true);
  assert.equal(defaultSpec.preset.endpoint, 'google');
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
  assert.equal(fastSpec.preset.endpoint, 'OpenRouter');
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
      const endpointType =
        endpoint === EModelEndpoint.google ? EModelEndpoint.google : EModelEndpoint.custom;
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
        agent_id: _a,
        chatProjectId: _c,
        ...model_parameters
      } = parsedBody;
      const agent = {
        id: EModelEndpoint.agents,
        name: spec.label,
        provider: endpoint,
        model: spec.preset.model,
        instructions: spec.preset.promptPrefix,
        tools: [],
        model_parameters,
      };
      const initialized = await initializeAgent(
        {
          req: { user: { id: 'user-1' }, config: appConfig, body: {} },
          res: {},
          agent,
          loadTools,
          endpointOption: { endpoint, endpointType, model_parameters },
          allowedProviders: new Set([endpoint, 'google', 'openAI', 'custom']),
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

test('only zyb_studio is attached and no CAD servers or prompts remain', () => {
  const config = configSchema.parse(parsedYaml);
  assert.deepEqual(Object.keys(config.mcpServers), ['zyb_studio']);
  for (const spec of config.modelSpecs.list) {
    assert.deepEqual(spec.mcpServers, ['zyb_studio']);
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
    assert.doesNotMatch(spec.preset.promptPrefix || '', VENDOR_NAME_PATTERN);

    // 验证中文提示词精简且包含八个核心工具名。
    const prefix = spec.preset.promptPrefix || '';
    const chineseChars = (prefix.match(/[\u4e00-\u9fff]/g) || []).length;
    assert.ok(
      chineseChars > 0 && chineseChars <= 250,
      `Expected <= 250 Chinese characters, got ${chineseChars}`,
    );
    assert.ok(prefix.length <= 400, `Expected <= 400 total characters, got ${prefix.length}`);
    for (const toolName of [
      'design_concepts',
      'generate_image',
      'edit_image',
      'list_images',
      'search_library',
      'compare_materials',
      'create_3d_model',
      'check_3d_model',
    ]) {
      assert.ok(prefix.includes(toolName), `promptPrefix must reference ${toolName}`);
    }
  }
});

test('promptPrefix and MCP image context mention only tool and argument names present in the contract', () => {
  const config = configSchema.parse(parsedYaml);
  for (const spec of config.modelSpecs.list) {
    const prefix = spec.preset.promptPrefix || '';
    const identifiers = prefix.match(/[a-z_][a-z0-9_]*/g) || [];
    for (const identifier of identifiers) {
      assert.ok(
        CONTRACT_IDENTIFIERS.has(identifier),
        `Non-contract identifier "${identifier}" found in promptPrefix of ${spec.name}`,
      );
    }
    assert.ok(prefix.includes('base_id'), 'promptPrefix must mention base_id');
    assert.ok(prefix.includes('references'), 'promptPrefix must mention references');
    assert.ok(prefix.includes('reference_ids'), 'promptPrefix must mention reference_ids');
    assert.ok(prefix.includes('kb_'), 'promptPrefix must mention kb_ IDs');
    assert.ok(prefix.includes('target'), 'promptPrefix must mention compare_materials target');
    assert.ok(
      prefix.includes('materials'),
      'promptPrefix must mention compare_materials materials',
    );
    assert.ok(prefix.includes('image_id'), 'promptPrefix must mention create_3d_model image_id');
    assert.ok(prefix.includes('detail'), 'promptPrefix must mention create_3d_model detail');
    assert.ok(prefix.includes('job_id'), 'promptPrefix must mention check_3d_model job_id');
    assert.doesNotMatch(prefix, /\b(reference_uses|image_ids)\b/);
  }

  const mcpContext = buildMCPImageToolContext({
    imageFiles: [{ file_id: 'img-1', filename: 'booth.png', kind: 'upload', turn: 1 }],
  });
  assert.ok(mcpContext.includes('- img-1 (booth.png, upload, turn 1)'));
  const backtickTokens = [...mcpContext.matchAll(/`([^`]+)`/g)].map((m) => m[1]);
  assert.deepEqual(backtickTokens, [
    'edit_image.base_id',
    'references[].id',
    'references[].use',
    'design_concepts.reference_ids',
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
  assert.equal(studioServer.title, '庄严宝刹设计工坊');
  assert.equal(studioServer.chatMenu, false);
  assert.equal(studioServer.requiresOAuth, false);
  // 超时时间须大于规划（540s）与生图（240s）之和 780,000ms。
  assert.equal(studioServer.timeout, 900000);
  assert.ok(studioServer.timeout >= (540 + 240) * 1000);
  assert.equal(isChatSelectableMCPServer(studioServer), false);
  assert.deepEqual(studioServer.headers, {
    'X-LibreChat-User-ID': '{{LIBRECHAT_USER_ID}}',
    'X-LibreChat-Conversation-ID': '{{LIBRECHAT_BODY_CONVERSATIONID}}',
    'X-Studio-Token': '${STUDIO_SHARED_TOKEN}',
  });

  // 验证 processMCPEnv 能正确替换用户 ID、会话 ID 与共享令牌环境变量。
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
      'X-LibreChat-Conversation-ID': 'conv-456',
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

test('fileConfig permits image uploads on both google and OpenRouter endpoints', () => {
  const config = configSchema.parse(parsedYaml);
  const merged = mergeFileConfig(config.fileConfig);

  for (const endpoint of ['google', 'OpenRouter']) {
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
  assert.equal(studio.mem_limit, '1g');
  assert.deepEqual(studio.ports, ['127.0.0.1:8000:8000']);
  assert.doesNotMatch(rawCompose, /STUDIO_PORT/);
  assert.ok(studio.volumes.includes('./studio.json:/run/secrets/studio.json:ro'));
  assert.ok(studio.volumes.includes('images:/librechat/images:ro'));
  assert.ok(studio.volumes.includes('studio-data:/data'));
  assert.ok(studio.healthcheck, 'studio healthcheck must be defined');

  const api = services.api;
  assert.equal(api.environment.ENDPOINTS, 'custom,google');
  assert.equal(api.environment.GOOGLE_MODELS, 'gemini-4-argon');
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

