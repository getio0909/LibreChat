const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const express = require('express');
const request = require('supertest');
const sharp = require('sharp');
const { CallToolResultSchema } = require('@modelcontextprotocol/sdk/types.js');

jest.mock('~/models', () => {
  const files = new Map();
  return {
    __files: files,
    createFile: jest.fn(async (record) => {
      files.set(record.file_id, record);
      return record;
    }),
    getFiles: jest.fn(async (filter) => {
      const file = files.get(filter.file_id);
      return file ? [file] : [];
    }),
    getAgents: jest.fn(async () => []),
  };
});

jest.mock('~/config/paths', () => {
  const root = require('node:path').join(
    require('node:os').tmpdir(),
    `studio-download-${process.pid}`,
  );
  return {
    root,
    publicPath: require('node:path').join(root, 'public'),
    imageOutput: require('node:path').join(root, 'public', 'images'),
    uploads: require('node:path').join(root, 'uploads'),
  };
});

jest.mock('~/server/services/Files/retention', () => ({
  getRetentionExpiry: jest.fn(async () => ({})),
  getAgentFileRetentionExpiry: jest.fn(async () => ({})),
}));

jest.mock('~/server/services/Files/Citations', () => ({
  processFileCitations: jest.fn(),
}));

jest.mock('~/server/services/Files/Code/process', () => ({
  processCodeOutput: jest.fn(),
  runPreviewFinalize: jest.fn(),
}));

jest.mock('~/server/services/Files/Code/preflight', () => ({
  preflightCodeOutputBatch: jest.fn(async () => []),
}));

jest.mock('~/server/services/Config', () => ({
  checkCapability: jest.fn(),
}));

jest.mock('~/server/services/PermissionService', () => ({
  getEffectivePermissions: jest.fn(async () => 0),
  checkPermission: jest.fn(async () => false),
}));

jest.mock('~/server/services/Files/routing', () => ({
  resolveEffectiveToolResource: jest.fn(),
  resolveUploadEndpoint: jest.fn(),
  resolveUploadAgent: jest.fn(),
}));

jest.mock('~/server/services/Tools/credentials', () => ({
  loadAuthValues: jest.fn(),
}));

jest.mock('~/cache', () => ({
  getLogStores: jest.fn(() => ({ get: jest.fn(), set: jest.fn() })),
}));

const db = require('~/models');
const paths = require('~/config/paths');
const { formatToolContent } = require('../../../../../packages/api/src/mcp/parsers.ts');
const { createToolEndCallback } = require('../callbacks');
const filesRouter = require('~/server/routes/files/files');

describe('Studio edit download through the real tool callback and File route', () => {
  const userId = '65f0000000000000000000a1';
  const imageId = 'img_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  let dataDir;
  let previousDataDir;

  beforeEach(() => {
    db.__files.clear();
    dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-download-receipt-'));
    previousDataDir = process.env.STUDIO_DATA_DIR;
    process.env.STUDIO_DATA_DIR = dataDir;
  });

  afterEach(() => {
    if (previousDataDir === undefined) {
      delete process.env.STUDIO_DATA_DIR;
    } else {
      process.env.STUDIO_DATA_DIR = previousDataDir;
    }
    fs.rmSync(dataDir, { recursive: true, force: true });
    fs.rmSync(paths.root, { recursive: true, force: true });
  });

  it('downloads the byte-identical lossless PNG despite a JPEG preview and UI marker', async () => {
    const pixels = crypto.randomBytes(320 * 240 * 4);
    const canonicalPng = await sharp(pixels, {
      raw: { width: 320, height: 240, channels: 4 },
    })
      .png({ compressionLevel: 0 })
      .toBuffer();
    const jobDir = path.join(dataDir, 'users', userId, 'jobs', imageId);
    fs.mkdirSync(jobDir, { recursive: true });
    fs.writeFileSync(
      path.join(jobDir, 'receipt.json'),
      JSON.stringify({ state: 'saved', file: 'original.png' }),
    );
    fs.writeFileSync(path.join(jobDir, 'original.png'), canonicalPng);

    const toolResult = CallToolResultSchema.parse({
      content: [
        { type: 'text', text: JSON.stringify({ image_id: imageId, outside_changed_pixels: 0 }) },
        {
          type: 'resource',
          resource: {
            uri: `ui://zyb_studio/compare/${imageId}`,
            mimeType: 'text/html',
            text: '<html><body>Comparison</body></html>',
          },
        },
        {
          type: 'image',
          mimeType: 'image/jpeg',
          data: Buffer.from('lossy-preview').toString('base64'),
        },
      ],
      isError: false,
    });
    const [content, artifact] = formatToolContent(toolResult, 'google', {
      mcpApps: { enabled: true, legacyHtmlEnabled: true },
    });
    const output = {
      name: 'edit_region_mcp_zyb_studio',
      tool_call_id: 'call-edit',
      content,
      artifact,
    };
    const config = {
      fileStrategy: 'local',
      paths: { imageOutput: paths.imageOutput, uploads: paths.uploads },
    };
    const artifactPromises = [];
    const callback = createToolEndCallback({
      req: { user: { id: userId }, config },
      res: { headersSent: false, writableEnded: false },
      artifactPromises,
    });
    await callback({ output }, { run_id: 'run-1', thread_id: 'thread-1', provider: 'google' });
    const files = await Promise.all(artifactPromises);
    const saved = files.find((file) => file?.file_id);
    expect(saved).toBeDefined();
    expect(saved.type).toBe('image/png');
    expect(saved.filename).toMatch(/\.png$/);
    expect(output.artifact.file_ids).toEqual([saved.file_id]);

    const app = express();
    app.use((req, _res, next) => {
      req.user = { id: req.get('X-Test-User') || userId, role: 'USER' };
      req.config = config;
      next();
    });
    app.use('/files', filesRouter);
    const downloaded = await request(app).get(`/files/download/${userId}/${saved.file_id}`);
    expect(downloaded.status).toBe(200);
    expect(downloaded.headers['content-disposition']).toContain('.png');
    expect(downloaded.body.equals(canonicalPng)).toBe(true);
    const denied = await request(app)
      .get(`/files/download/${userId}/${saved.file_id}`)
      .set('X-Test-User', '65f0000000000000000000b2');
    expect(denied.status).toBe(403);
  });
});
