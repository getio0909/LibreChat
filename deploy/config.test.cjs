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

test('manual CAD uploads are stored without parsing binary files as model context', () => {
  const config = configSchema.parse(yaml.load(fs.readFileSync('librechat.yaml', 'utf8')));
  const fileConfig = mergeFileConfig(config.fileConfig);
  const endpointConfig = getEndpointFileConfig({ fileConfig, endpoint: 'OpenRouter' });
  for (const mimeType of [
    'model/step',
    'model/stl',
    'application/octet-stream',
    'application/x-blender',
  ]) {
    assert.ok(fileConfig.checkType(mimeType, endpointConfig.supportedMimeTypes));
    const deliveryPath = resolveUploadLLMDeliveryPath({
      mimeType,
      endpoint: 'OpenRouter',
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
      endpoint: 'OpenRouter',
      endpointConfig,
      fileConfig,
    }),
    'provider',
  );
});
