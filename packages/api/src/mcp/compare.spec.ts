import { AIMessage, ToolMessage } from '@langchain/core/messages';
import { calculateMaxToolResultChars, projectArtifactPayload } from '@librechat/agents';
import type { MCPToolCallResponse } from './types';
import { formatToolContent } from './parsers';

describe('Studio comparison image context', () => {
  it('passes each labeled preview to the provider vision message in variant order', () => {
    const imageIds = [
      'img_11111111111111111111111111111111',
      'img_22222222222222222222222222222222',
      'img_33333333333333333333333333333333',
    ];
    const previews = imageIds.map((_, index) =>
      Buffer.alloc(48 * 1024, index + 1).toString('base64'),
    );
    const result: MCPToolCallResponse = {
      content: [
        {
          type: 'text',
          text: JSON.stringify({
            variants: imageIds.map((image_id, index) => ({
              image_id,
              material: `stone-${index + 1}`,
              preview_image_index: index + 1,
            })),
            image_context: 'Inspect each image before describing its colors or edited parts.',
          }),
        },
        ...imageIds.map((_, index) => ({
          type: 'image' as const,
          mimeType: 'image/png',
          data: previews[index],
        })),
      ],
      isError: false,
    };
    const [content, artifact] = formatToolContent(result, 'google');
    const toolCallId = 'comparison-call';
    const messages = projectArtifactPayload(
      [
        new AIMessage({
          content: '',
          tool_calls: [{ id: toolCallId, name: 'compare_materials', args: {} }],
        }),
        new ToolMessage({ content, artifact, tool_call_id: toolCallId }),
      ],
      calculateMaxToolResultChars(200_000),
    );
    const visionContent = messages[messages.length - 1]?.content;
    expect(Array.isArray(visionContent)).toBe(true);
    expect(visionContent).toEqual([
      expect.objectContaining({ type: 'text', text: expect.stringContaining(imageIds[0]) }),
      ...imageIds.map((_, index) => ({
        type: 'image_url',
        image_url: {
          url: `data:image/png;base64,${previews[index]}`,
        },
      })),
    ]);
  });
});
