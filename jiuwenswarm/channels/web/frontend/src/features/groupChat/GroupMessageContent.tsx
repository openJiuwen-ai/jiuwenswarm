import { MarkdownRenderer } from '../../components/MarkdownRenderer';
import { useProgressiveText } from '../../hooks/useProgressiveText';
import type { GroupMember, GroupMessage } from './client';
import { groupMessageContent, splitMentionParts } from './state';

/** Animate new public replies, including servers that publish only a complete message. */
export function GroupMessageContent({ message, animate, members }: {
  message: GroupMessage; animate: boolean; members?: GroupMember[];
}) {
  const text = groupMessageContent(message);
  const content = useProgressiveText(text, animate && message.sender !== 'user');
  if (message.sender === 'user') {
    // 用户消息里的 @成员 高亮为 chip；含提及时按纯文本渲染，避免 markdown 块级元素拆散行内 chip。
    const parts = splitMentionParts(content, members ?? []);
    if (parts.some((part) => part.kind === 'mention')) {
      return <div className="group-chat-message-text" data-testid="group-chat-message-text">
        {parts.map((part, index) => part.kind === 'mention'
          ? <span key={index} className="group-chat-mention-chip" data-testid="group-chat-mention-chip"
            data-variant={part.label}>@{part.label}</span>
          : <span key={index}>{part.text}</span>)}
      </div>;
    }
  }
  return <MarkdownRenderer content={content} isStreaming={content !== text} />;
}
