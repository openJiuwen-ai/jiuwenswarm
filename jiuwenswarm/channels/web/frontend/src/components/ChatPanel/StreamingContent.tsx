interface StreamingContentProps {
  content: string;
}

export function StreamingContent({ content }: StreamingContentProps) {
  return (
    <div className="chat-text">
      <span className="whitespace-pre-wrap">{content}</span>
    </div>
  );
}
