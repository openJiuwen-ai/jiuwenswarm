import type { ReactNode } from 'react';
import './FilePreviewPanel.css';

export interface FilePreviewPanelProps {
  left: ReactNode;
  right: ReactNode;
  className?: string;
  testId?: string;
}

export function FilePreviewPanel({ left, right, className, testId }: FilePreviewPanelProps) {
  return (
    <div className={`file-preview-panel${className ? ` ${className}` : ''}`} data-testid={testId}>
      <aside className="file-preview-tree">
        <div className="file-preview-tree__body">{left}</div>
      </aside>
      <section className="file-preview-content" aria-live="polite">
        {right}
      </section>
    </div>
  );
}
