import { Constants, FileContext } from 'librechat-data-provider';
import {
  buildImageToolContext,
  buildMCPImageToolContext,
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
