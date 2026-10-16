import { buildImageToolContext } from './imageContext';

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
      toolName: 'MCP image tools',
      contextDescription: 'MCP tools',
    });

    expect(result).toContain('available for MCP tools:');
    expect(result).toContain('\n\t- file-aaa (booth.png)\n\t- file-bbb\n\t- file-ccc (altar.webp)');
    expect(result).toContain('when calling `MCP image tools`');
  });
});
