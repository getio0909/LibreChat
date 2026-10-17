/**
 * These tests verify the proxy wrapper behavior for GeminiImageGen.
 * Instead of loading the full module (which has many dependencies),
 * we directly test the wrapper logic that would be applied.
 */
describe('GeminiImageGen Proxy Configuration', () => {
  let originalEnv;
  let originalFetch;

  beforeAll(() => {
    originalEnv = { ...process.env };
    originalFetch = globalThis.fetch;
  });

  beforeEach(() => {
    process.env = { ...originalEnv };
    globalThis.fetch = originalFetch;
  });

  afterEach(() => {
    process.env = originalEnv;
    globalThis.fetch = originalFetch;
  });

  /**
   * Simulates the proxy wrapper that GeminiImageGen applies at module load.
   * This is the same logic from GeminiImageGen.js lines 30-42.
   */
  function applyProxyWrapper() {
    const proxyDispatcher = process.env.PROXY ? { type: 'proxy-dispatcher' } : undefined;
    if (proxyDispatcher) {
      const _originalFetch = globalThis.fetch;

      globalThis.fetch = function (url, options = {}) {
        const urlString = url.toString();
        if (urlString.includes('googleapis.com')) {
          options = { ...options, dispatcher: proxyDispatcher };
        }
        return _originalFetch.call(this, url, options);
      };
    }
  }

  it('should wrap globalThis.fetch when PROXY env is set', () => {
    process.env.PROXY = 'http://proxy.example.com:8080';

    const fetchBeforeWrap = globalThis.fetch;

    applyProxyWrapper();

    expect(globalThis.fetch).not.toBe(fetchBeforeWrap);
  });

  it('should not wrap globalThis.fetch when PROXY env is not set', () => {
    delete process.env.PROXY;

    const fetchBeforeWrap = globalThis.fetch;

    applyProxyWrapper();

    expect(globalThis.fetch).toBe(fetchBeforeWrap);
  });

  it('should add dispatcher to googleapis.com URLs', async () => {
    process.env.PROXY = 'http://proxy.example.com:8080';

    let capturedOptions = null;
    const mockFetch = jest.fn((url, options) => {
      capturedOptions = options;
      return Promise.resolve({ ok: true });
    });
    globalThis.fetch = mockFetch;

    applyProxyWrapper();

    await globalThis.fetch('https://generativelanguage.googleapis.com/v1/models', {});

    expect(capturedOptions).toBeDefined();
    expect(capturedOptions.dispatcher).toEqual({ type: 'proxy-dispatcher' });
  });

  it('should not add dispatcher to non-googleapis.com URLs', async () => {
    process.env.PROXY = 'http://proxy.example.com:8080';

    let capturedOptions = null;
    const mockFetch = jest.fn((url, options) => {
      capturedOptions = options;
      return Promise.resolve({ ok: true });
    });
    globalThis.fetch = mockFetch;

    applyProxyWrapper();

    await globalThis.fetch('https://api.openai.com/v1/images', {});

    expect(capturedOptions).toBeDefined();
    expect(capturedOptions.dispatcher).toBeUndefined();
  });

  it('should preserve existing options when adding dispatcher', async () => {
    process.env.PROXY = 'http://proxy.example.com:8080';

    let capturedOptions = null;
    const mockFetch = jest.fn((url, options) => {
      capturedOptions = options;
      return Promise.resolve({ ok: true });
    });
    globalThis.fetch = mockFetch;

    applyProxyWrapper();

    const customHeaders = { 'X-Custom-Header': 'test' };
    await globalThis.fetch('https://aiplatform.googleapis.com/v1/models', {
      headers: customHeaders,
      method: 'POST',
    });

    expect(capturedOptions).toBeDefined();
    expect(capturedOptions.dispatcher).toEqual({ type: 'proxy-dispatcher' });
    expect(capturedOptions.headers).toEqual(customHeaders);
    expect(capturedOptions.method).toBe('POST');
  });
});

// 验证 createGeminiImageTool 桥接 kb_ 与当前用户 img_ 并传给 generateContent，且跨用户或无法解析的 image_ids 在调用 generateContent 前失败关闭
describe('GeminiImageGen Studio reference bridging and fail-closed validation', () => {
  const fs = require('fs');
  const os = require('os');
  const path = require('path');
  const sharp = require('sharp');
  const { deriveStudioBridgeFileId } = require('@librechat/api');

  let tmpRoot;
  let kbRoot;
  let dataDir;
  let samplePngBuffer;
  let sampleJpgBuffer;
  const userA = '65f0000000000000000000a1';
  const userB = '65f0000000000000000000b2';
  const kbId = 'kb_0123456789abcdef';
  const imgId = 'img_abcdef0123456789abcdef0123456789';

  beforeAll(async () => {
    samplePngBuffer = await sharp({
      create: { width: 16, height: 16, channels: 4, background: { r: 10, g: 20, b: 30, alpha: 1 } },
    })
      .png()
      .toBuffer();
    sampleJpgBuffer = await sharp({
      create: { width: 16, height: 16, channels: 3, background: { r: 40, g: 50, b: 60 } },
    })
      .jpeg()
      .toBuffer();
  });

  beforeEach(() => {
    jest.resetModules();
    tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'gemini-tool-bridge-'));
    kbRoot = path.join(tmpRoot, 'kb');
    dataDir = path.join(tmpRoot, 'data');
    process.env.STUDIO_KB_ROOT = kbRoot;
    process.env.STUDIO_DATA_DIR = dataDir;

    fs.mkdirSync(path.join(kbRoot, 'records'), { recursive: true });
    fs.mkdirSync(path.join(kbRoot, 'assets', 'ref2048'), { recursive: true });
    fs.writeFileSync(
      path.join(kbRoot, 'records', '0123456789abcdef.yaml'),
      'id: 0123456789abcdef\n',
    );
    fs.writeFileSync(
      path.join(kbRoot, 'assets', 'ref2048', '0123456789abcdef.jpg'),
      sampleJpgBuffer,
    );

    const jobDir = path.join(dataDir, 'users', userA, 'jobs', imgId);
    fs.mkdirSync(jobDir, { recursive: true });
    fs.writeFileSync(
      path.join(jobDir, 'receipt.json'),
      JSON.stringify({ state: 'saved', file: 'original.png' }),
    );
    fs.writeFileSync(path.join(jobDir, 'original.png'), samplePngBuffer);
  });

  afterEach(() => {
    delete process.env.STUDIO_KB_ROOT;
    delete process.env.STUDIO_DATA_DIR;
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  });

  function setupToolForUser(activeUserId) {
    const fileStore = new Map();
    const generateContentMock = jest.fn(async () => ({
      candidates: [
        {
          content: {
            parts: [
              {
                inlineData: {
                  mimeType: 'image/png',
                  data: samplePngBuffer.toString('base64'),
                },
              },
            ],
          },
        },
      ],
    }));

    jest.doMock('@google/genai', () => ({
      GoogleGenAI: jest.fn(() => ({
        models: {
          generateContent: generateContentMock,
        },
      })),
    }));

    const createFileMock = jest.fn(async (data) => {
      const existing = fileStore.get(data.file_id);
      const record = { ...data };
      if (existing) {
        existing.record = record;
      }
      return record;
    });

    jest.doMock('~/models', () => ({
      spendTokens: jest.fn(),
      createFile: createFileMock,
      getFiles: jest.fn(async (filter) => {
        const ids = filter?.file_id?.$in ?? [];
        return ids
          .map((id) => fileStore.get(id)?.record)
          .filter((rec) => Boolean(rec && rec.user === filter.user));
      }),
    }));

    jest.doMock('~/server/services/Files/strategies', () => ({
      getStrategyFunctions: jest.fn(() => ({
        saveBuffer: jest.fn(async ({ userId, fileName, buffer }) => {
          const filepath = `/images/${userId}/${fileName}`;
          const fileId = fileName.slice(0, 36);
          fileStore.set(fileId, {
            record: { file_id: fileId, filepath, user: userId },
            bytes: buffer,
          });
          return filepath;
        }),
        getDownloadStream: jest.fn(async (_req, filepath) => {
          for (const entry of fileStore.values()) {
            if (entry.record.filepath === filepath) {
              return (async function* () {
                yield entry.bytes;
              })();
            }
          }
          return null;
        }),
      })),
    }));

    const { createGeminiImageTool } = require('../GeminiImageGen');
    const toolInstance = createGeminiImageTool({
      isAgent: true,
      GEMINI_API_KEY: 'test-gemini-key',
      userId: activeUserId,
      fileStrategy: 'local',
      req: { user: { id: activeUserId }, config: {} },
    });

    return { toolInstance, generateContentMock, createFileMock };
  }

  it('bridges kb_<16-hex> and owned img_<32-hex> and sends inlineData to Gemini generateContent', async () => {
    const { toolInstance, generateContentMock, createFileMock } = setupToolForUser(userA);

    await toolInstance.func(
      {
        prompt: '使用参考石材与造型生成供桌',
        image_ids: [kbId, imgId],
        aspectRatio: '4:3',
        imageSize: '2K',
      },
      undefined,
      { configurable: { thread_id: 'conv-1' } },
    );

    expect(createFileMock).toHaveBeenCalledTimes(2);
    expect(createFileMock.mock.calls[0][0].file_id).toBe(deriveStudioBridgeFileId(userA, kbId));
    expect(createFileMock.mock.calls[1][0].file_id).toBe(deriveStudioBridgeFileId(userA, imgId));
    expect(generateContentMock).toHaveBeenCalledTimes(1);
    const callArg = generateContentMock.mock.calls[0][0];
    expect(callArg.contents).toEqual([
      { text: '使用参考石材与造型生成供桌' },
      {
        inlineData: {
          mimeType: 'image/jpeg',
          data: sampleJpgBuffer.toString('base64'),
        },
      },
      {
        inlineData: {
          mimeType: 'image/png',
          data: samplePngBuffer.toString('base64'),
        },
      },
    ]);
  });

  it('rejects another user img_<32-hex> and fails closed without calling ai.models.generateContent', async () => {
    const { toolInstance, generateContentMock } = setupToolForUser(userB);

    const [contentResult, artifact] = await toolInstance.func(
      {
        prompt: '尝试跨用户引用他人图像',
        image_ids: [imgId],
      },
      undefined,
      { configurable: { thread_id: 'conv-2' } },
    );

    expect(generateContentMock).not.toHaveBeenCalled();
    expect(artifact).toEqual({ content: [], file_ids: [] });
    expect(contentResult[0].text).toContain(imgId);
  });

  it('fails closed without calling ai.models.generateContent when image_ids contains an unresolved ID', async () => {
    const { toolInstance, generateContentMock } = setupToolForUser(userA);

    const [contentResult, artifact] = await toolInstance.func(
      {
        prompt: '引用不存在的图像ID',
        image_ids: [kbId, 'unresolved-id-999'],
      },
      undefined,
      { configurable: { thread_id: 'conv-3' } },
    );

    expect(generateContentMock).not.toHaveBeenCalled();
    expect(artifact).toEqual({ content: [], file_ids: [] });
    expect(contentResult[0].text).toContain('unresolved-id-999');
  });
});
