import type { ReactNode } from 'react';
import './FilePreviewPanel.css';

export interface FilePreviewPanelProps {
  left: ReactNode;
  right: ReactNode;
  treeTitle?: ReactNode;
  onRefresh?: () => void;
  refreshLabel?: string;
  className?: string;
  testId?: string;
}

export function FilePreviewPanel({
  left,
  right,
  treeTitle,
  onRefresh,
  refreshLabel,
  className,
  testId,
}: FilePreviewPanelProps) {
  const showTreeHeader = treeTitle != null || onRefresh != null;
  return (
    <div className={`file-preview-panel${className ? ` ${className}` : ''}`} data-testid={testId}>
      <aside className="file-preview-tree">
        {showTreeHeader ? (
          <header className="file-preview-tree__header">
            <span className="file-preview-tree__title">{treeTitle}</span>
            <div className="file-preview-tree__header-actions">
              {onRefresh ? (
                <button
                  type="button"
                  className="file-preview-tree__refresh-btn"
                  onClick={onRefresh}
                  disabled={false}
                >
                  {refreshLabel || 'Refresh'}
                </button>
              ) : null}
            </div>
          </header>
        ) : null}
        <div className="file-preview-tree__body">{left}</div>
      </aside>
      <section className="file-preview-content" aria-live="polite">
        {right}
      </section>
    </div>
  );
}
