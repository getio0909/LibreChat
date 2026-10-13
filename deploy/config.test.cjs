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
