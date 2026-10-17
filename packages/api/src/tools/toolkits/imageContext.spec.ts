import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { Constants, FileContext } from 'librechat-data-provider';
import type { GeminiReferenceFileRecord } from './imageContext';
import {
  buildImageToolContext,
  buildMCPImageToolContext,
  isStudioReferenceId,
  deriveStudioBridgeFileId,
  resolveGeminiReferenceImages,
  collectBranchImageCandidateIds,
  collectConversationBranchImageFiles,
} from './imageContext';

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
});
