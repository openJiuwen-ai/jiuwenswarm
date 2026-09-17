interface StreamingContentProps {
  content: string;
}

export function StreamingContent({ content }: StreamingContentProps) {
  return (
    <div className="chat-text" data-testid="chat-panel-streaming-content">
      <span className="whitespace-pre-wrap">{content}</span>
    </div>
  );
}
