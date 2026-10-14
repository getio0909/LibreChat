const fs = require('node:fs');
const test = require('node:test');
const assert = require('node:assert/strict');
const yaml = require('js-yaml');
const {
  configSchema,
  fileConfig: defaults,
  fullMimeTypesList,
  mergeFileConfig,
  getEndpointFileConfig,
  resolveUploadDestination,
  resolveUploadLLMDeliveryPath,
} = require('librechat-data-provider');

test('drawing-sheet tool schema remains valid for Claude after LibreChat conversion', async () => {
  const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
  const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
  const { InMemoryTransport } = require('@modelcontextprotocol/sdk/inMemory.js');
  const { formatMCPServerTools } = require('@librechat/api');
  const sdkRequire = require('node:module').createRequire(
    require.resolve('@modelcontextprotocol/sdk/client/index.js'),
  );
  const Ajv2020 = sdkRequire('ajv/dist/2020').default;
  const { drawingSheetInputSchema } = await import('./design/sheet.ts');
  const server = new McpServer({ name: 'schema-regression', version: '1' });
  server.registerTool(
    'compile_drawing_sheet',
    { inputSchema: drawingSheetInputSchema },
    async () => ({ content: [] }),
  );
  const client = new Client({ name: 'schema-regression', version: '1' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  try {
    await server.connect(serverTransport);
    await client.connect(clientTransport);
    const { tools } = await client.listTools();
    const normalized = Object.values(formatMCPServerTools('design_tools', tools));
    const validator = new Ajv2020({ strict: false, allErrors: true });
    for (const tool of normalized) {
      assert.equal(
        validator.validateSchema(tool.function.parameters),
        true,
        `${tool.function.name}: ${JSON.stringify(validator.errors)}`,
      );
    }
    const view = { id: 'front', title: 'Front', scale: 1, origin_mm: [10, 20] };
    const detail = { id: 'joint', title: 'Joint', x_mm: 10, y_mm: 20, w_mm: 30, h_mm: 25 };
    for (const { point, valid } of [
      { point: [10, 20], valid: true },
      { point: [10], valid: false },
      { point: [10, 20, 30], valid: false },
      { point: ['10', 20], valid: false },
      { point: [10, '20'], valid: false },
      { point: null, valid: false },
    ]) {
      assert.equal(
        drawingSheetInputSchema.views.safeParse([{ ...view, origin_mm: point }]).success,
        valid,
      );
      const polylines = [{ points: [point, [30, 40]] }];
      assert.equal(
        drawingSheetInputSchema.views.safeParse([{ ...view, polylines }]).success,
        valid,
      );
      assert.equal(
        drawingSheetInputSchema.details.safeParse([{ ...detail, polylines }]).success,
        valid,
      );
    }
  } finally {
    await client.close();
    await server.close();
  }
});

test('CAD modes enforce effort and output budgets over saved aliases while titles stay lightweight', () => {
  const { getOpenAILLMConfig } = require('@librechat/api');
  const { ChatOpenRouter } = require('@librechat/agents/llm/openrouter');
  const config = configSchema.parse(yaml.load(fs.readFileSync('librechat.yaml', 'utf8')));
  assert.equal(config.modelSpecs.enforce, true);
  assert.equal(config.interface.modelSelect, false);
  for (const spec of config.modelSpecs.list) {
    const endpoint = config.endpoints.custom.find((item) => item.name === spec.preset.endpoint);
    const titleEndpoint = config.endpoints.custom.find(
      (item) => item.name === endpoint.titleEndpoint,
    );
    for (const saved of [
      {},
      { max_tokens: 4096 },
      { reasoning_effort: 'low', temperature: 0.7, top_p: 0.9, max_tokens: 128 },
    ]) {
      const { llmConfig } = getOpenAILLMConfig({
        apiKey: 'fixture-only',
        streaming: true,
        endpoint: endpoint.name,
        baseURL: endpoint.baseURL,
        useOpenRouter: true,
        modelOptions: { model: spec.preset.model, ...saved },
        addParams: endpoint.addParams,
        dropParams: endpoint.dropParams,
      });
      const request = new ChatOpenRouter(llmConfig).invocationParams();
      assert.deepEqual(request.reasoning, {
        effort: spec.name === 'haiku-design-max' ? 'max' : 'high',
        enabled: true,
      });
      assert.equal(request.max_tokens, 128000);
      assert.ok(spec.preset.maxContextTokens + request.max_tokens <= 1000000);
      assert.equal(request.temperature, undefined);
      assert.equal(request.top_p, undefined);
      assert.equal(request.include_reasoning, undefined);
      assert.equal(request.tool_choice, undefined);
      const { llmConfig: titleConfig } = getOpenAILLMConfig({
        apiKey: 'fixture-only',
        streaming: false,
        endpoint: titleEndpoint.name,
        baseURL: titleEndpoint.baseURL,
        useOpenRouter: true,
        modelOptions: llmConfig,
        addParams: titleEndpoint.addParams,
        dropParams: titleEndpoint.dropParams,
      });
      const titleRequest = new ChatOpenRouter(titleConfig).invocationParams();
      assert.deepEqual(titleRequest.reasoning, {
        effort: 'low',
        enabled: false,
      });
      assert.equal(titleRequest.max_tokens, 256);
    }
  }
});

test('manual CAD uploads are stored without parsing binary files as model context', () => {
  const config = configSchema.parse(yaml.load(fs.readFileSync('librechat.yaml', 'utf8')));
  const fileConfig = mergeFileConfig(config.fileConfig);
  for (const endpoint of ['OpenRouter', 'OpenRouterMax']) {
    const endpointConfig = getEndpointFileConfig({ fileConfig, endpoint });
    for (const mimeType of [
      'model/step',
      'model/stl',
      'application/octet-stream',
      'application/x-blender',
    ]) {
      assert.ok(fileConfig.checkType(mimeType, endpointConfig.supportedMimeTypes));
      const deliveryPath = resolveUploadLLMDeliveryPath({
        mimeType,
        endpoint,
        endpointConfig,
        fileConfig,
      });
      assert.equal(deliveryPath, 'none');
      const destination = resolveUploadDestination({
        deliveryPath,
        mimeType,
        hasAgent: false,
        isMessageAttachment: true,
        allowUnknownMessageConsumer: true,
      });
      assert.equal(destination.rejection, undefined);
    }
    for (const mimeType of fullMimeTypesList.filter((type) => defaults.checkType(type))) {
      assert.ok(fileConfig.checkType(mimeType, endpointConfig.supportedMimeTypes), mimeType);
    }
    assert.equal(
      resolveUploadLLMDeliveryPath({
        mimeType: 'image/png',
        endpoint,
        endpointConfig,
        fileConfig,
      }),
      'provider',
    );
  }
});

test('CAD MCP transports use streamable-http and single-replica deployment arms schedules cleanly', () => {
  const config = configSchema.parse(yaml.load(fs.readFileSync('librechat.yaml', 'utf8')));
  assert.equal(config.mcpServers.freecad.type, 'streamable-http');
  assert.equal(config.mcpServers.freecad.url, 'http://freecad-{{LIBRECHAT_USER_ID}}:8000/mcp');
  assert.ok(config.mcpServers.freecad.sseReadTimeout >= 600000);
  assert.equal(config.mcpServers.blender.type, 'streamable-http');
  assert.equal(config.mcpServers.design_tools.type, 'streamable-http');

  const compose = yaml.load(fs.readFileSync('deploy/compose.yml', 'utf8'));
  assert.equal(compose.services.api.environment.SCHEDULES_SINGLE_PROCESS, 'true');

  const dockerfile = fs.readFileSync('deploy/freecad.Dockerfile', 'utf8');
  assert.match(dockerfile, /"--outputTransport",\s*"streamableHttp",\s*"--stateful"/);
  assert.match(dockerfile, /COPY deploy\/cad_check\.py \/opt\/freecad-mcp\/cad_check\.py/);

  const designDockerfile = fs.readFileSync('deploy/design.Dockerfile', 'utf8');
  assert.match(designDockerfile, /deploy\/design\/sheet\.ts/);
  assert.match(config.mcpServers.freecad.serverInstructions, /cad_check\.audit_document/);
  assert.match(config.mcpServers.design_tools.serverInstructions, /compile_drawing_sheet/);
});
