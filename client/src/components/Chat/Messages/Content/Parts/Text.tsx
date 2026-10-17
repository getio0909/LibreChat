import { memo, useMemo, ReactElement } from 'react';
import MarkdownLite from '~/components/Chat/Messages/Content/MarkdownLite';
import { useMessagePartsHost } from '~/Providers/MessagePartsHostContext';
import useSmoothStreaming from '~/hooks/Messages/useSmoothStreaming';
import Markdown from '~/components/Chat/Messages/Content/Markdown';
import { cn, unwrapLegacyUIPromptText } from '~/utils';
import CollapsibleText from './CollapsibleText';

type TextPartProps = {
  text: string;
  showCursor: boolean;
  isCreatedByUser: boolean;
};

type ContentType =
  | ReactElement<React.ComponentProps<typeof Markdown>>
  | ReactElement<React.ComponentProps<typeof MarkdownLite>>
  | ReactElement;

const TextPart = memo(function TextPart({ text, isCreatedByUser, showCursor }: TextPartProps) {
  const { useMessage, useUserTextPreferences } = useMessagePartsHost();
  const { isSubmitting = false, isLatestMessage = false } = useMessage();
  const { enableUserMsgMarkdown, collapseLongUserMessages } = useUserTextPreferences();
  const smoothStreaming = useSmoothStreaming();
  const displayText = useMemo(
    () => (isCreatedByUser ? unwrapLegacyUIPromptText(text) : text),
    [isCreatedByUser, text],
  );
  // The word fade itself indicates streaming, so the trailing block cursor
  // only shows when the fade is unavailable (setting off or reduced motion).
  const showCursorState = useMemo(
    () => showCursor && isSubmitting && !(smoothStreaming && !isCreatedByUser),
    [showCursor, isSubmitting, smoothStreaming, isCreatedByUser],
  );

  const content: ContentType = useMemo(() => {
    if (!isCreatedByUser) {
      return <Markdown content={displayText} isLatestMessage={isLatestMessage} />;
    } else if (enableUserMsgMarkdown) {
      return <MarkdownLite content={displayText} />;
    } else {
      return <>{displayText}</>;
    }
  }, [isCreatedByUser, enableUserMsgMarkdown, displayText, isLatestMessage]);

  return (
    <CollapsibleText enabled={isCreatedByUser && collapseLongUserMessages}>
      <div
        className={cn(
          isSubmitting ? 'submitting' : '',
          showCursorState && !!displayText.length ? 'result-streaming' : '',
          'markdown prose message-content dark:prose-invert light w-full break-words',
          isCreatedByUser && !enableUserMsgMarkdown && 'whitespace-pre-wrap',
          'text-text-primary',
        )}
      >
        {content}
      </div>
    </CollapsibleText>
  );
});
TextPart.displayName = 'TextPart';

export default TextPart;
