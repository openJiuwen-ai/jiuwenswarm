/**
 * StreamingContent 组件
 *
 * 流式内容显示
 */

interface StreamingContentProps {
  content: string;
  isStreaming: boolean;
}

export function StreamingContent({ content, isStreaming }: StreamingContentProps) {
  return (
    <div className="chat-text">
      <span className="whitespace-pre-wrap">{content}</span>
      {isStreaming && <span className="streaming-cursor" />}
    </div>
  );
}
