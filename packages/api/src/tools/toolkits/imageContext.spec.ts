import fs from 'node:fs';
import os from 'node:os';
import sharp from 'sharp';
import path from 'node:path';
import crypto from 'node:crypto';
import { Constants, FileContext } from 'librechat-data-provider';
import type { GeminiReferenceFileRecord } from './imageContext';
import {
  buildImageToolContext,
  buildMCPImageToolContext,
  isStudioReferenceId,
  readImageDimensions,
  deriveStudioBridgeFileId,
  processToolImageArtifacts,
  resolveGeminiReferenceImages,
  extractStudioArtifactImageIds,
  collectBranchImageCandidateIds,
  collectConversationBranchImageFiles,
} from './imageContext';
import { deriveStudioStorageUserId } from '~/utils/studioIdentity';

describe('buildImageToolContext', () => {
  it('returns empty string when imageFiles is empty', () => {
    expect(buildImageToolContext({ imageFiles: [], toolName: 'gemini_image_gen' })).toBe('');
  });

  it('formats file_id without filename when filename is absent', () => {
    const result = buildImageToolContext({
      imageFiles: [{ file_id: 'img-1' }, { file_id: 'img-2' }],
      toolName: 'gemini_image_gen',
    });

    expect(result).toContain('available for image context:');
    expect(result).toContain('\n\t- img-1\n\t- img-2\n\n');
    expect(result).toContain('when calling `gemini_image_gen`');
  });

  it('includes filename in parentheses alongside file_id when filename is present', () => {
    const result = buildImageToolContext({
      imageFiles: [
        { file_id: 'file-aaa', filename: 'booth.png' },
        { file_id: 'file-bbb' },
        { file_id: 'file-ccc', filename: 'altar.webp' },
      ],
      toolName: 'image_edit_oai',
      contextDescription: 'image editing',
    });

    expect(result).toContain('available for image editing:');
    expect(result).toContain('\n\t- file-aaa (booth.png)\n\t- file-bbb\n\t- file-ccc (altar.webp)');
    expect(result).toContain('when calling `image_edit_oai`');
  });
});

// 验证 MCP 图像上下文构建器输出中立列表及契约真实参数名
describe('buildMCPImageToolContext', () => {
  it('returns empty string when imageFiles is empty', () => {
    expect(buildMCPImageToolContext({ imageFiles: [] })).toBe('');
  });

  it('formats neutral "id (filename, kind, turn)" list and real contract arguments', () => {
    const result = buildMCPImageToolContext({
      imageFiles: [
        { file_id: 'file-up-1', filename: 'booth.png', kind: 'upload', turn: 1 },
        {
          file_id: 'file-gen-1',
          filename: 'concept_1.png',
          context: FileContext.image_generation,
          turn: 1,
        },
        { file_id: 'file-up-2', filename: 'slab.webp', kind: 'upload', turn: 2 },
      ],
    });

    expect(result).toContain('- file-up-1 (booth.png, upload, turn 1)');
    expect(result).toContain('- file-gen-1 (concept_1.png, generated, turn 1)');
    expect(result).toContain('- file-up-2 (slab.webp, upload, turn 2)');
    expect(result).toContain('`gemini_image_gen.image_ids`');
    expect(result).toContain('`select_region.image_id`');
    expect(result).toContain('`edit_region.base_id`');
    expect(result).toContain('`compare_materials.base_id`');
    expect(result).toContain('`references[].id`');
    expect(result).toContain('`references[].use`');
    expect(result).toContain('`create_3d_model.image_id`');
    expect(result).not.toContain('edit_image.base_id');
    expect(result).not.toContain('design_concepts.reference_ids');
    expect(result).not.toContain('reference_uses');
    expect(result).not.toContain('MCP image tools');
  });

  it('caps the list to the newest 20 images', () => {
    const imageFiles = Array.from({ length: 25 }, (_, i) => ({
      file_id: `img-${i + 1}`,
      filename: `file_${i + 1}.png`,
      kind: 'upload' as const,
      turn: i + 1,
    }));

    const result = buildMCPImageToolContext({ imageFiles });
    expect(result).not.toContain('- img-1 (');
    expect(result).not.toContain('- img-5 (');
    expect(result).toContain('- img-6 (file_6.png, upload, turn 6)');
    expect(result).toContain('- img-25 (file_25.png, upload, turn 25)');
  });
});

// 验证跨轮次分支图像提取与用户隔离
describe('collectConversationBranchImageFiles', () => {
  it('collects turn 1 uploaded and tool-generated images on turn 2 while excluding other users and sibling branches', () => {
    const messages = [
      {
        messageId: 'msg-u1',
        parentMessageId: Constants.NO_PARENT,
        isCreatedByUser: true,
        user: 'user-1',
        files: [
          { file_id: 'img-upload-t1', filename: 'sketch.png' },
          { file_id: 'img-other-user', filename: 'stolen.png' },
          { file_id: 'doc-t1', filename: 'spec.pdf' },
        ],
      },
      {
        messageId: 'msg-a1',
        parentMessageId: 'msg-u1',
        isCreatedByUser: false,
        user: 'user-1',
        attachments: [
          {
            file_id: 'img-gen-t1',
            filename: 'img-gen-t1-generate_image_mcp_zyb_studio.png',
            type: 'image/png',
            context: FileContext.image_generation,
          },
        ],
      },
      {
        messageId: 'msg-sibling',
        parentMessageId: Constants.NO_PARENT,
        isCreatedByUser: true,
        user: 'user-1',
        files: [{ file_id: 'img-sibling', filename: 'sibling.png' }],
      },
    ];

    const candidateIds = collectBranchImageCandidateIds({
      messages,
      parentMessageId: 'msg-a1',
      userId: 'user-1',
    });
    expect(candidateIds).toEqual(['img-upload-t1', 'img-other-user', 'doc-t1', 'img-gen-t1']);
    expect(candidateIds).not.toContain('img-sibling');

    const hydratedFilesById = new Map([
      [
        'img-upload-t1',
        {
          file_id: 'img-upload-t1',
          filename: 'sketch.png',
          type: 'image/png',
          user: 'user-1',
          context: FileContext.message_attachment,
        },
      ],
      [
        'img-gen-t1',
        {
          file_id: 'img-gen-t1',
          filename: 'img-gen-t1-generate_image_mcp_zyb_studio.png',
          type: 'image/png',
          user: 'user-1',
          context: FileContext.image_generation,
        },
      ],
      [
        'img-other-user',
        {
          file_id: 'img-other-user',
          filename: 'stolen.png',
          type: 'image/png',
          user: 'user-2',
          context: FileContext.message_attachment,
        },
      ],
      [
        'doc-t1',
        {
          file_id: 'doc-t1',
          filename: 'spec.pdf',
          type: 'application/pdf',
          user: 'user-1',
          context: FileContext.message_attachment,
        },
      ],
      [
        'img-sibling',
        {
          file_id: 'img-sibling',
          filename: 'sibling.png',
          type: 'image/png',
          user: 'user-1',
          context: FileContext.message_attachment,
        },
      ],
      [
        'img-upload-t2',
        {
          file_id: 'img-upload-t2',
          filename: 'ref_stone.webp',
          type: 'image/webp',
          user: 'user-1',
          context: FileContext.message_attachment,
        },
      ],
    ]);

    const resolved = collectConversationBranchImageFiles({
      messages,
      parentMessageId: 'msg-a1',
      requestFiles: [{ file_id: 'img-upload-t2', filename: 'ref_stone.webp', type: 'image/webp' }],
      hydratedFilesById,
      userId: 'user-1',
    });

    expect(resolved).toEqual([
      {
        file_id: 'img-upload-t1',
        filename: 'sketch.png',
        kind: 'upload',
        turn: 1,
        context: FileContext.message_attachment,
      },
      {
        file_id: 'img-gen-t1',
        filename: 'img-gen-t1-generate_image_mcp_zyb_studio.png',
        kind: 'generated',
        turn: 1,
        context: FileContext.image_generation,
      },
      {
        file_id: 'img-upload-t2',
        filename: 'ref_stone.webp',
        kind: 'upload',
        turn: 2,
        context: FileContext.message_attachment,
      },
    ]);
  });
});

// 验证 Gemini 图像生成引用解析：桥接 kb_ 与当前用户的 img_ 为授权 File 记录，拒绝跨用户 img_，并在无法解析时失败关闭
describe('resolveGeminiReferenceImages', () => {
  let tmpRoot: string;
  let kbRoot: string;
  let dataDir: string;
  const userA = '65f0000000000000000000a1';
  const userB = '65f0000000000000000000b2';
  const kbId = 'kb_0123456789abcdef';
  const kbBytes = Buffer.from('fake-kb-jpeg-bytes');
  const imgId = 'img_abcdef0123456789abcdef0123456789';
  const imgBytes = Buffer.from('fake-studio-png-bytes');

  beforeEach(() => {
    tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-bridge-test-'));
    kbRoot = path.join(tmpRoot, 'kb');
    dataDir = path.join(tmpRoot, 'data');

    fs.mkdirSync(path.join(kbRoot, 'records'), { recursive: true });
    fs.mkdirSync(path.join(kbRoot, 'assets', 'ref2048'), { recursive: true });
    fs.writeFileSync(
      path.join(kbRoot, 'records', '0123456789abcdef.yaml'),
      'id: 0123456789abcdef\n',
    );
    fs.writeFileSync(path.join(kbRoot, 'assets', 'ref2048', '0123456789abcdef.jpg'), kbBytes);

    const userAJobDir = path.join(dataDir, 'users', userA, 'jobs', imgId);
    fs.mkdirSync(userAJobDir, { recursive: true });
    fs.writeFileSync(
      path.join(userAJobDir, 'receipt.json'),
      JSON.stringify({ state: 'saved', file: 'original.png' }),
    );
    fs.writeFileSync(path.join(userAJobDir, 'original.png'), imgBytes);
  });

  afterEach(() => {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  });

  it('recognizes kb_<16-hex> and img_<32-hex> studio reference IDs and derives per-user bridge file_ids', () => {
    expect(isStudioReferenceId(kbId)).toBe(true);
    expect(isStudioReferenceId(imgId)).toBe(true);
    expect(isStudioReferenceId('bad-id')).toBe(false);

    const bridgeA = deriveStudioBridgeFileId(userA, kbId);
    const bridgeB = deriveStudioBridgeFileId(userB, kbId);
    expect(bridgeA).not.toBe(bridgeB);
    expect(deriveStudioBridgeFileId(userA, kbId)).toBe(bridgeA);
    expect(deriveStudioBridgeFileId(userA, kbId, 'tenant-a')).not.toBe(
      deriveStudioBridgeFileId(userA, kbId, 'tenant-b'),
    );
    expect(deriveStudioStorageUserId(userA, null)).toBe(userA);
    expect(deriveStudioStorageUserId(userA, 'tenant-a')).not.toBe(
      deriveStudioStorageUserId(userA, 'tenant-b'),
    );
  });

  it('loads kb_ and img_ bytes from the resolved MCP URL when the API has no Studio volume', async () => {
    const fetchImpl = Object.assign(
      jest.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        expect(url).toMatch(/^http:\/\/studio:8000\/files\/(?:kb_|img_)/);
        expect(init?.headers).toEqual({
          'X-Studio-Token': 'fixture-token',
          'X-LibreChat-User-ID': userA,
          'X-LibreChat-Raw-User-ID': userA,
        });
        const isKb = url.endsWith(kbId);
        return new Response(isKb ? kbBytes : imgBytes, {
          headers: { 'content-type': isKb ? 'image/jpeg' : 'image/png' },
        });
      }),
      { preconnect: jest.fn() },
    );
    const saved = new Map<string, Buffer>();
    const saveBase64Image = jest.fn(async (dataUrl: string, options: { file_id: string }) => {
      saved.set(options.file_id, Buffer.from(dataUrl.split(',')[1], 'base64'));
      return {
        file_id: options.file_id,
        filepath: options.file_id,
        source: 'local',
        type: dataUrl.startsWith('data:image/jpeg') ? 'image/jpeg' : 'image/png',
        user: userA,
      };
    });
    const req = {
      user: { id: userA },
      config: {
        mcpConfig: {
          zyb_studio: {
            url: 'http://studio:8000/mcp',
            headers: { 'X-Studio-Token': 'fixture-token' },
          },
        },
      },
    };
    const result = await resolveGeminiReferenceImages({
      image_ids: [kbId, imgId],
      req,
      kbRoot: path.join(tmpRoot, 'missing-kb'),
      dataDir: path.join(tmpRoot, 'missing-data'),
      getFiles: async () => [],
      saveBase64Image,
      getStrategyFunctions: () => ({
        getDownloadStream: async (_req, filepath) =>
          (async function* () {
            yield saved.get(filepath) ?? Buffer.alloc(0);
          })(),
      }),
      fetchImpl,
    });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(result).toEqual({
      ok: true,
      value: [
        { inlineData: { mimeType: 'image/jpeg', data: kbBytes.toString('base64') } },
        { inlineData: { mimeType: 'image/png', data: imgBytes.toString('base64') } },
      ],
    });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(saveBase64Image).toHaveBeenCalledTimes(2);
  });

  it('rejects supplied and fetched Files from another tenant and isolates Studio image jobs', async () => {
    const tenantA = 'tenant-a';
    const tenantB = 'tenant-b';
    const tenantAId = deriveStudioBridgeFileId(userA, imgId, tenantA);
    const tenantAFile: GeminiReferenceFileRecord = {
      file_id: tenantAId,
      filepath: '/images/tenant-a.png',
      source: 'local',
      type: 'image/png',
      user: userA,
      tenantId: tenantA,
      width: 32,
      height: 32,
    };
    const getDownloadStream = jest.fn(async () =>
      (async function* () {
        yield imgBytes;
      })(),
    );
    const getFiles = jest.fn(async () => [tenantAFile]);
    const foreignFile = await resolveGeminiReferenceImages({
      imageFiles: [tenantAFile],
      image_ids: [tenantAId],
      req: { user: { id: userA, tenantId: tenantB } },
      getFiles,
      getStrategyFunctions: () => ({ getDownloadStream }),
    });
    expect(foreignFile.ok).toBe(false);
    expect(getFiles).toHaveBeenCalledWith(
      expect.objectContaining({ user: userA, tenantId: tenantB }),
      {},
      {},
    );
    expect(getDownloadStream).not.toHaveBeenCalled();

    const tenantAJob = path.join(
      dataDir,
      'users',
      deriveStudioStorageUserId(userA, tenantA),
      'jobs',
      imgId,
    );
    fs.mkdirSync(tenantAJob, { recursive: true });
    fs.writeFileSync(
      path.join(tenantAJob, 'receipt.json'),
      JSON.stringify({ state: 'saved', file: 'original.png' }),
    );
    fs.writeFileSync(path.join(tenantAJob, 'original.png'), imgBytes);
    const crossTenantImage = await resolveGeminiReferenceImages({
      image_ids: [imgId],
      req: { user: { id: userA, tenantId: tenantB } },
      dataDir,
      getFiles: async () => [],
      saveBase64Image: jest.fn(),
      getStrategyFunctions: () => ({ getDownloadStream }),
    });
    expect(crossTenantImage.ok).toBe(false);
    expect(getDownloadStream).not.toHaveBeenCalled();
  });

  it('bridges search_knowledge kb_<16-hex> into an authorized LibreChat File record and returns inlineData', async () => {
    const fileStore = new Map<string, { record: GeminiReferenceFileRecord; bytes: Buffer }>();

    const getFiles = jest.fn(async (filter: Record<string, unknown>) => {
      const requested = (filter.file_id as { $in?: string[] })?.$in ?? [];
      return requested
        .map((id) => fileStore.get(id)?.record)
        .filter((f): f is GeminiReferenceFileRecord => Boolean(f && f.user === filter.user));
    });

    const saveBase64Image = jest.fn(
      async (
        dataUrl: string,
        options: { file_id: string; filename: string; req?: { user?: { id?: string } } },
      ) => {
        const match = /^data:([^;]+);base64,(.+)$/.exec(dataUrl);
        const mimeType = match?.[1] ?? 'image/jpeg';
        const buffer = Buffer.from(match?.[2] ?? '', 'base64');
        const filepath = `/images/${options.req?.user?.id}/${options.filename}.jpg`;
        const record: GeminiReferenceFileRecord = {
          file_id: options.file_id,
          filepath,
          type: mimeType,
          source: 'local',
          filename: options.filename,
          user: options.req?.user?.id,
          width: 1024,
          height: 1024,
        };
        fileStore.set(options.file_id, { record, bytes: buffer });
        return record;
      },
    );

    const getStrategyFunctions = jest.fn(() => ({
      getDownloadStream: jest.fn(async (_req: unknown, filepath: string) => {
        for (const entry of fileStore.values()) {
          if (entry.record.filepath === filepath) {
            return (async function* () {
              yield entry.bytes;
            })();
          }
        }
        return null;
      }),
    }));

    const firstResult = await resolveGeminiReferenceImages({
      image_ids: [kbId],
      req: { user: { id: userA } },
      kbRoot,
      dataDir,
      getFiles,
      saveBase64Image,
      getStrategyFunctions,
    });

    expect(firstResult).toEqual({
      ok: true,
      value: [
        {
          inlineData: {
            mimeType: 'image/jpeg',
            data: kbBytes.toString('base64'),
          },
        },
      ],
    });
    expect(saveBase64Image).toHaveBeenCalledTimes(1);
    expect(saveBase64Image.mock.calls[0][1].file_id).toBe(deriveStudioBridgeFileId(userA, kbId));

    // 同一用户再次引用同一 kb_ 时直接命中已桥接的授权 File 记录，无需重复写入
    const secondResult = await resolveGeminiReferenceImages({
      image_ids: [kbId],
      req: { user: { id: userA } },
      kbRoot,
      dataDir,
      getFiles,
      saveBase64Image,
      getStrategyFunctions,
    });
    expect(secondResult.ok).toBe(true);
    expect(saveBase64Image).toHaveBeenCalledTimes(1);
  });

  it('bridges current user img_<32-hex> and rejects another user img_<32-hex>', async () => {
    const fileStore = new Map<string, { record: GeminiReferenceFileRecord; bytes: Buffer }>();
    const saveBase64Image = jest.fn(
      async (
        dataUrl: string,
        options: { file_id: string; filename: string; req?: { user?: { id?: string } } },
      ) => {
        const match = /^data:([^;]+);base64,(.+)$/.exec(dataUrl);
        const mimeType = match?.[1] ?? 'image/png';
        const buffer = Buffer.from(match?.[2] ?? '', 'base64');
        const filepath = `/images/${options.req?.user?.id}/${options.filename}.png`;
        const record: GeminiReferenceFileRecord = {
          file_id: options.file_id,
          filepath,
          type: mimeType,
          source: 'local',
          filename: options.filename,
          user: options.req?.user?.id,
          width: 1024,
          height: 1024,
        };
        fileStore.set(options.file_id, { record, bytes: buffer });
        return record;
      },
    );
    const getStrategyFunctions = jest.fn(() => ({
      getDownloadStream: jest.fn(async (_req: unknown, filepath: string) => {
        for (const entry of fileStore.values()) {
          if (entry.record.filepath === filepath) {
            return (async function* () {
              yield entry.bytes;
            })();
          }
        }
        return null;
      }),
    }));

    const ownerResult = await resolveGeminiReferenceImages({
      image_ids: [imgId],
      req: { user: { id: userA } },
      kbRoot,
      dataDir,
      getFiles: async () => [],
      saveBase64Image,
      getStrategyFunctions,
    });

    expect(ownerResult).toEqual({
      ok: true,
      value: [
        {
          inlineData: {
            mimeType: 'image/png',
            data: imgBytes.toString('base64'),
          },
        },
      ],
    });

    // 另一用户请求 userA 的 img_<32-hex> 时必须被拒绝且不创建桥接记录
    saveBase64Image.mockClear();
    const crossUserResult = await resolveGeminiReferenceImages({
      image_ids: [imgId],
      req: { user: { id: userB } },
      kbRoot,
      dataDir,
      getFiles: async () => [],
      saveBase64Image,
      getStrategyFunctions,
    });

    expect(crossUserResult.ok).toBe(false);
    if (!crossUserResult.ok) {
      expect(crossUserResult.error.code).toBe('UNRESOLVED_IMAGE_REFERENCES');
      expect(crossUserResult.error.unresolvedIds).toEqual([imgId]);
    }
    expect(saveBase64Image).not.toHaveBeenCalled();
  });

  it('fails closed when any requested image_ids entry cannot be resolved', async () => {
    const validFile: GeminiReferenceFileRecord = {
      file_id: 'file-valid-1',
      filepath: '/images/userA/valid.png',
      type: 'image/png',
      source: 'local',
      user: userA,
    };

    const result = await resolveGeminiReferenceImages({
      imageFiles: [validFile],
      image_ids: ['file-valid-1', 'kb_deadbeefdeadbeef'],
      req: { user: { id: userA } },
      kbRoot,
      dataDir,
      getFiles: async () => [],
      saveBase64Image: jest.fn(),
      getStrategyFunctions: () => ({
        getDownloadStream: async () =>
          (async function* () {
            yield Buffer.from('valid-bytes');
          })(),
      }),
    });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.error.code).toBe('UNRESOLVED_IMAGE_REFERENCES');
      expect(result.error.unresolvedIds).toEqual(['kb_deadbeefdeadbeef']);
      expect(result.error.message).toContain('kb_deadbeefdeadbeef');
    }
  });

  // 校验 image_ids 数量超限时返回 TOO_MANY_IMAGE_REFERENCES，并对重复 ID 及同一资产的原始 ID 与桥接 UUID 去重
  it('enforces maxItems limit (TOO_MANY_IMAGE_REFERENCES) and deduplicates repeated or aliased image_ids', async () => {
    const overLimitIds = Array.from({ length: 15 }, (_, idx) => `file-${idx}`);
    const overLimitResult = await resolveGeminiReferenceImages({
      image_ids: overLimitIds,
      req: { user: { id: userA } },
    });
    expect(overLimitResult.ok).toBe(false);
    if (!overLimitResult.ok) {
      expect(overLimitResult.error.code).toBe('TOO_MANY_IMAGE_REFERENCES');
      expect(overLimitResult.error.message).toContain('TOO_MANY_IMAGE_REFERENCES');
    }

    const bridgedId = deriveStudioBridgeFileId(userA, imgId);
    const createFile = jest.fn(async (data: Record<string, unknown>) => ({
      ...(data as unknown as GeminiReferenceFileRecord),
    }));
    const saveBuffer = jest.fn(
      async ({ userId: u, fileName }: { userId: string; fileName: string }) => {
        return `/images/${u}/${fileName}`;
      },
    );
    const getDownloadStream = jest.fn(async () =>
      (async function* () {
        yield imgBytes;
      })(),
    );

    const dedupResult = await resolveGeminiReferenceImages({
      image_ids: [imgId, imgId.toUpperCase(), bridgedId],
      req: { user: { id: userA } },
      kbRoot,
      dataDir,
      getFiles: async () => [],
      createFile,
      getStrategyFunctions: () => ({
        saveBuffer,
        getDownloadStream,
      }),
    });

    expect(dedupResult.ok).toBe(true);
    if (dedupResult.ok) {
      expect(dedupResult.value).toHaveLength(1);
    }
    expect(createFile).toHaveBeenCalledTimes(1);
    expect(getDownloadStream).toHaveBeenCalledTimes(1);
  });

  // 校验单图与总字节上限（IMAGE_REFERENCE_TOO_LARGE）以及桥接真实图片尺寸测量
  it('enforces per-image and total byte limits (IMAGE_REFERENCE_TOO_LARGE) and records measured image dimensions', async () => {
    const realPng = await sharp({
      create: {
        width: 320,
        height: 180,
        channels: 4,
        background: { r: 100, g: 120, b: 140, alpha: 1 },
      },
    })
      .png()
      .toBuffer();

    const realImgId = 'img_99999999999999999999999999999999';
    const jobDir = path.join(dataDir, 'users', userA, 'jobs', realImgId);
    fs.mkdirSync(jobDir, { recursive: true });
    fs.writeFileSync(
      path.join(jobDir, 'receipt.json'),
      JSON.stringify({ state: 'saved', file: 'original.png' }),
    );
    fs.writeFileSync(path.join(jobDir, 'original.png'), realPng);

    const dims = await readImageDimensions(realPng);
    expect(dims).toEqual({ width: 320, height: 180 });

    const createFile = jest.fn(async (data: Record<string, unknown>) => ({
      ...(data as unknown as GeminiReferenceFileRecord),
    }));
    const dimRes = await resolveGeminiReferenceImages({
      image_ids: [realImgId],
      req: { user: { id: userA } },
      kbRoot,
      dataDir,
      getFiles: async () => [],
      createFile,
      getStrategyFunctions: () => ({
        saveBuffer: async ({ userId: u, fileName }) => `/images/${u}/${fileName}`,
        getDownloadStream: async () =>
          (async function* () {
            yield realPng;
          })(),
      }),
    });
    expect(dimRes.ok).toBe(true);
    expect(createFile).toHaveBeenCalledWith(
      expect.objectContaining({
        width: 320,
        height: 180,
        bytes: realPng.length,
      }),
      true,
    );

    // 单图超过 maxImageBytes 时返回 IMAGE_REFERENCE_TOO_LARGE
    const largeBuffer = crypto.randomBytes(2048);
    fs.writeFileSync(path.join(jobDir, 'original.png'), largeBuffer);
    const tooLargeSingle = await resolveGeminiReferenceImages({
      image_ids: [realImgId],
      req: { user: { id: userA } },
      kbRoot,
      dataDir,
      limits: { maxImageBytes: 1024 },
      getFiles: async () => [],
      createFile,
      getStrategyFunctions: () => ({
        saveBuffer: async () => '/images/x.png',
        getDownloadStream: async () => null,
      }),
    });
    expect(tooLargeSingle.ok).toBe(false);
    if (!tooLargeSingle.ok) {
      expect(tooLargeSingle.error.code).toBe('IMAGE_REFERENCE_TOO_LARGE');
      expect(tooLargeSingle.error.unresolvedIds).toEqual([realImgId]);
    }

    // 多图累计超过 maxTotalBytes 时返回 IMAGE_REFERENCE_TOO_LARGE
    const file1: GeminiReferenceFileRecord = {
      file_id: 'f-1',
      filepath: '/images/f1.png',
      type: 'image/png',
      source: 'local',
      user: userA,
    };
    const file2: GeminiReferenceFileRecord = {
      file_id: 'f-2',
      filepath: '/images/f2.png',
      type: 'image/png',
      source: 'local',
      user: userA,
    };
    const tooLargeTotal = await resolveGeminiReferenceImages({
      imageFiles: [file1, file2],
      image_ids: ['f-1', 'f-2'],
      req: { user: { id: userA } },
      limits: { maxImageBytes: 2048, maxTotalBytes: 1500 },
      getStrategyFunctions: () => ({
        getDownloadStream: async () =>
          (async function* () {
            yield Buffer.alloc(1000, 1);
          })(),
      }),
    });
    expect(tooLargeTotal.ok).toBe(false);
    if (!tooLargeTotal.ok) {
      expect(tooLargeTotal.error.code).toBe('IMAGE_REFERENCE_TOO_LARGE');
      expect(tooLargeTotal.error.unresolvedIds).toEqual(['f-2']);
    }
  });
});

// 验证 processToolImageArtifacts 落盘 >10 MiB 规范 Studio 原图以及失败关闭时抹除未落盘图像 ID
describe('processToolImageArtifacts', () => {
  const userA = '65f0000000000000000000a1';
  let tmpRoot: string;
  let dataDir: string;

  beforeEach(() => {
    tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'studio-artifact-barrier-'));
    dataDir = path.join(tmpRoot, 'data');
  });

  afterEach(() => {
    fs.rmSync(tmpRoot, { recursive: true, force: true });
  });

  it('extracts Studio image IDs from single and multi-variant tool outputs', () => {
    const singleId = 'img_11111111111111111111111111111111';
    const v1 = 'img_22222222222222222222222222222222';
    const v2 = 'img_33333333333333333333333333333333';

    expect(
      extractStudioArtifactImageIds({
        content: [
          { type: 'text', text: JSON.stringify({ id: singleId, width: 2048, height: 2048 }) },
        ],
      }),
    ).toEqual([singleId]);

    expect(
      extractStudioArtifactImageIds({
        content: [
          {
            type: 'text',
            text: JSON.stringify({
              variants: [{ image_id: v1 }, { image_id: v2 }],
            }),
          },
        ],
      }),
    ).toEqual([v1, v2]);
  });

  it('persists canonical >10 MiB lossless Studio PNG instead of lightweight inline preview', async () => {
    const studioImgId = 'img_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
    const jobDir = path.join(dataDir, 'users', userA, 'jobs', studioImgId);
    fs.mkdirSync(jobDir, { recursive: true });

    // 构造超过 10 MiB 的规范原图字节与轻量预览字节
    const canonicalLargePng = crypto.randomBytes(11 * 1024 * 1024);
    fs.writeFileSync(
      path.join(jobDir, 'receipt.json'),
      JSON.stringify({ state: 'saved', file: 'original.png' }),
    );
    fs.writeFileSync(path.join(jobDir, 'original.png'), canonicalLargePng);

    const smallPreviewDataUrl = `data:image/jpeg;base64,${Buffer.from('small-jpeg-preview').toString('base64')}`;
    const output = {
      name: 'edit_region_mcp_zyb_studio',
      tool_call_id: 'call-er-1',
      content: [
        {
          type: 'text',
          text: JSON.stringify({
            id: studioImgId,
            width: 2048,
            height: 2048,
            outside_changed_pixels: 0,
          }),
        },
      ],
      artifact: {
        content: [
          {
            type: 'image_url',
            image_url: { url: smallPreviewDataUrl },
          },
        ],
        file_ids: ['initial-random-uuid'],
      },
    };

    let savedBuffer: Buffer | null = null;
    const uploadImageBuffer = jest.fn(
      async ({
        metadata,
      }: {
        metadata: {
          buffer: Buffer;
          file_id: string;
          filename: string;
          type: string;
          width: number;
          height: number;
          bytes: number;
        };
      }) => {
        savedBuffer = metadata.buffer;
        return {
          file_id: metadata.file_id,
          filepath: `/images/${userA}/${metadata.file_id}-${metadata.filename}`,
          filename: metadata.filename,
          type: metadata.type,
          width: metadata.width,
          height: metadata.height,
          bytes: metadata.bytes,
        };
      },
    );
    const onFileSaved = jest.fn();
    const artifactPromises: Array<Promise<Record<string, unknown> | null>> = [];

    await processToolImageArtifacts({
      output,
      req: { user: { id: userA }, config: {} },
      metadata: { run_id: 'run-1', thread_id: 'thread-1', provider: 'google' },
      artifactPromises,
      dataDir,
      uploadImageBuffer,
      buildMetadata: (file) => ({ ...file, messageId: 'run-1', conversationId: 'thread-1' }),
      onFileSaved,
    });

    const results = await Promise.all(artifactPromises);
    expect(results).toHaveLength(1);
    expect(savedBuffer).not.toBeNull();
    expect(savedBuffer!.length).toBe(canonicalLargePng.length);
    expect(savedBuffer!.equals(canonicalLargePng)).toBe(true);
    expect(onFileSaved).toHaveBeenCalledTimes(1);
    expect(uploadImageBuffer).toHaveBeenCalledWith(
      expect.objectContaining({ resize: false, context: FileContext.image_generation }),
    );
  });

  it('publishes every comparison variant as a separate canonical File', async () => {
    const ids = ['img_11111111111111111111111111111111', 'img_22222222222222222222222222222222'];
    const originals = ids.map((_, index) => Buffer.from(`canonical-png-${index}`));
    for (const [index, id] of ids.entries()) {
      const jobDir = path.join(dataDir, 'users', userA, 'jobs', id);
      fs.mkdirSync(jobDir, { recursive: true });
      fs.writeFileSync(
        path.join(jobDir, 'receipt.json'),
        JSON.stringify({ state: 'saved', file: 'original.png' }),
      );
      fs.writeFileSync(path.join(jobDir, 'original.png'), originals[index]);
    }
    const output = {
      name: 'compare_materials_mcp_zyb_studio',
      content: JSON.stringify({
        variants: ids.map((image_id, index) => ({ image_id, preview_image_index: index + 1 })),
      }),
      artifact: {
        content: ids.map((_, index) => ({
          type: 'image_url',
          image_url: {
            url: `data:image/jpeg;base64,${Buffer.from(`preview-${index}`).toString('base64')}`,
          },
        })),
        file_ids: [] as string[],
      },
    };
    const uploadImageBuffer = jest.fn(async ({ metadata }) => ({
      file_id: metadata.file_id,
      filepath: `/images/${metadata.file_id}.png`,
      filename: metadata.filename,
      type: metadata.type,
    }));
    const artifactPromises: Array<Promise<Record<string, unknown> | null>> = [];
    const onFileSaved = jest.fn();
    const published = await processToolImageArtifacts({
      output,
      req: { user: { id: userA }, config: {} },
      artifactPromises,
      dataDir,
      uploadImageBuffer,
      buildMetadata: (file) => file,
      onFileSaved,
    });
    expect(published).toBe(true);
    expect(uploadImageBuffer).toHaveBeenCalledTimes(2);
    expect(uploadImageBuffer.mock.calls.map(([call]) => call.metadata.buffer)).toEqual(originals);
    expect(output.artifact.file_ids).toHaveLength(2);
    expect(new Set(output.artifact.file_ids).size).toBe(2);
    expect(await Promise.all(artifactPromises)).toHaveLength(2);
    expect(onFileSaved).toHaveBeenCalledTimes(2);
  });

  it('scrubs generated_image_id and clears artifact.file_ids when image persistence fails', async () => {
    const unpersistedId = 'unpersisted-file-uuid-1234';
    const output = {
      name: 'gemini_image_gen',
      tool_call_id: 'call-gen-fail',
      content: `Gemini displayed an image.\n\ngenerated_image_id: "${unpersistedId}"`,
      artifact: {
        content: [
          {
            type: 'image_url',
            image_url: { url: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB' },
          },
        ],
        file_ids: [unpersistedId],
      },
    };

    const artifactPromises: Array<Promise<Record<string, unknown> | null>> = [];
    const onFileSaved = jest.fn();

    await processToolImageArtifacts({
      output,
      req: { user: { id: userA }, config: {} },
      metadata: { run_id: 'run-1', thread_id: 'thread-1', provider: 'google' },
      artifactPromises,
      saveBase64Image: jest.fn().mockRejectedValue(new Error('disk full')),
      buildMetadata: (file) => file,
      onFileSaved,
    });

    const results = await Promise.all(artifactPromises);
    expect(results).toEqual([null]);
    expect(onFileSaved).not.toHaveBeenCalled();
    expect(output.artifact.file_ids).toEqual([]);
    expect(String(output.content)).toContain('IMAGE_PERSISTENCE_FAILED');
    expect(String(output.content)).not.toContain(unpersistedId);
  });

  it('withholds every attachment when a later Studio variant fails to publish', async () => {
    const ids = ['img_11111111111111111111111111111111', 'img_22222222222222222222222222222222'];
    for (const id of ids) {
      const jobDir = path.join(dataDir, 'users', userA, 'jobs', id);
      fs.mkdirSync(jobDir, { recursive: true });
      fs.writeFileSync(
        path.join(jobDir, 'receipt.json'),
        JSON.stringify({ state: 'saved', file: 'original.png' }),
      );
      fs.writeFileSync(path.join(jobDir, 'original.png'), Buffer.from('canonical-original'));
    }
    const output = {
      name: 'compare_materials_mcp_zyb_studio',
      content: JSON.stringify({ variants: ids.map((image_id) => ({ image_id })) }),
      artifact: {
        content: ids.map(() => ({
          type: 'image_url',
          image_url: { url: 'data:image/jpeg;base64,cHJldmlldw==' },
        })),
        file_ids: [...ids],
      },
    };
    const uploadImageBuffer = jest
      .fn()
      .mockImplementationOnce(async ({ metadata }) => ({
        file_id: metadata.file_id,
        filepath: `/images/${metadata.file_id}.png`,
      }))
      .mockRejectedValueOnce(new Error('disk failure'));
    const onFileSaved = jest.fn();
    const artifactPromises: Array<Promise<Record<string, unknown> | null>> = [];
    const published = await processToolImageArtifacts({
      output,
      req: { user: { id: userA }, config: {} },
      artifactPromises,
      dataDir,
      uploadImageBuffer,
      buildMetadata: (file) => file,
      onFileSaved,
      logger: { error: jest.fn() },
    });
    expect(published).toBe(false);
    expect(uploadImageBuffer).toHaveBeenCalledTimes(2);
    expect(onFileSaved).not.toHaveBeenCalled();
    expect(await Promise.all(artifactPromises)).toEqual([null]);
    expect(output.content).toContain('IMAGE_PERSISTENCE_FAILED');
    expect(JSON.stringify(output)).not.toMatch(/img_111111|img_222222/);
    expect(output.artifact.file_ids).toEqual([]);
  });
});
