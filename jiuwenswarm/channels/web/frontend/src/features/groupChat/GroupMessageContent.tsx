import { MarkdownRenderer } from '../../components/MarkdownRenderer';
import { useProgressiveText } from '../../hooks/useProgressiveText';
import type { GroupMessage } from './client';
import { groupMessageContent } from './state';

/** Animate new public replies, including servers that publish only a complete message. */
export function GroupMessageContent({ message, animate }: { message: GroupMessage; animate: boolean }) {
  const text = groupMessageContent(message);
  const content = useProgressiveText(text, animate && message.sender !== 'user');
  return <MarkdownRenderer content={content} isStreaming={content !== text} />;
}
