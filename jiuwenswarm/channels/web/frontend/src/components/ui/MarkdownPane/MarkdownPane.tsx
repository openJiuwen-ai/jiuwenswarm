import type { ReactNode } from 'react';
import { MarkdownRenderer } from '../../MarkdownRenderer';

export interface MarkdownPaneProps {
  content: string | null;
  emptyText?: ReactNode;
  className?: string;
  testId?: string;
}

export function MarkdownPane({ content, emptyText, className, testId }: MarkdownPaneProps) {
  return (
    <div
      data-testid={testId}
      className={`flex-1 min-h-[150px] overflow-y-auto text-sm text-text rounded-md p-3${className ? ` ${className}` : ''}`}
    >
      {content != null ? <MarkdownRenderer content={content} className="chat-text chat-markdown" /> : emptyText}
    </div>
  );
}
