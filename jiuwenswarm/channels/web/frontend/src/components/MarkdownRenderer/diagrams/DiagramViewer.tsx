import clsx from 'clsx';
import { Copy, Download, Ellipsis, ImageDown } from 'lucide-react';
import { useEffect, useRef, useState, type HTMLAttributes, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { type DiagramExportConfig, useDiagramExportActions } from './useDiagramExportActions';

export type DiagramViewMode = 'image' | 'code';

export interface DiagramToolbarAction {
  id: string;
  title: string;
  icon: ReactNode;
  onClick: () => void;
}

interface DiagramMenuItem {
  id: string;
  icon: ReactNode;
  label: string;
  disabled?: boolean;
  onSelect: () => void;
}

interface DiagramViewerProps extends Omit<HTMLAttributes<HTMLDivElement>, 'children'> {
  viewMode: DiagramViewMode;
  onViewModeChange: (mode: DiagramViewMode) => void;
  exportConfig: DiagramExportConfig;
  toolbarActions?: DiagramToolbarAction[];
  statusText?: string;
  statusTone?: 'default' | 'danger' | 'warning';
  feedbackPosition?: 'start' | 'end';
  children: ReactNode;
}

interface ToolbarButtonProps {
  title: string;
  onClick: () => void;
  children: ReactNode;
  ariaHasPopup?: 'menu';
  ariaExpanded?: boolean;
}

function ToolbarButton({ title, onClick, children, ariaHasPopup, ariaExpanded }: ToolbarButtonProps): JSX.Element {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      aria-haspopup={ariaHasPopup}
      aria-expanded={ariaExpanded}
      onClick={onClick}
      className="markdown-toolbar-btn"
    >
      {children}
    </button>
  );
}

interface TogglePillProps {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}

function TogglePill({ active, onClick, children }: TogglePillProps): JSX.Element {
  return (
    <button type="button" aria-pressed={active} onClick={onClick} className={clsx('markdown-toggle-pill', active && 'markdown-toggle-pill--active')}>
      {children}
    </button>
  );
}

interface DiagramMoreMenuProps {
  title: string;
  items: DiagramMenuItem[];
}

function DiagramMoreMenu({ title, items }: DiagramMoreMenuProps): JSX.Element {
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('mousedown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [open]);

  return (
    <div className="diagram-more" ref={rootRef}>
      <ToolbarButton title={title} ariaHasPopup="menu" ariaExpanded={open} onClick={() => setOpen(current => !current)}>
        <Ellipsis size={17} />
      </ToolbarButton>
      {open && (
        <div className="diagram-menu" role="menu">
          {items.map(item => (
            <button
              key={item.id}
              type="button"
              className="diagram-menu__item"
              role="menuitem"
              disabled={item.disabled}
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function DiagramViewer({
  viewMode,
  onViewModeChange,
  exportConfig,
  toolbarActions = [],
  statusText,
  statusTone = 'default',
  feedbackPosition = 'end',
  className,
  children,
  ...rootProps
}: DiagramViewerProps): JSX.Element {
  const { t } = useTranslation();
  const { feedback, copyCode, downloadSource, downloadImage } = useDiagramExportActions(exportConfig);
  const downloadsDisabled = exportConfig.downloadEnabled === false;
  const menuItems: DiagramMenuItem[] = [
    {
      id: 'download-source',
      icon: <Download size={17} />,
      label: t('diagram.downloadSource'),
      disabled: downloadsDisabled,
      onSelect: () => void downloadSource(),
    },
    {
      id: 'download-image',
      icon: <ImageDown size={17} />,
      label: t('diagram.downloadImage'),
      disabled: downloadsDisabled,
      onSelect: () => void downloadImage(),
    },
    {
      id: 'copy-code',
      icon: <Copy size={17} />,
      label: t('diagram.copyCode'),
      onSelect: () => void copyCode(),
    },
  ];

  const feedbackStatus = feedback && (
    <span className="diagram-toolbar-status" role="status" aria-live="polite">
      {feedback}
    </span>
  );

  return (
    <div {...rootProps} className={clsx('diagram-container', className)} data-markdown-block="wide">
      <div className="diagram-container__toolbar">
        <div className="diagram-toolbar-start">
          <div className="diagram-view-toggle">
            <TogglePill active={viewMode === 'image'} onClick={() => onViewModeChange('image')}>
              {t('diagram.image')}
            </TogglePill>
            <TogglePill active={viewMode === 'code'} onClick={() => onViewModeChange('code')}>
              {t('diagram.code')}
            </TogglePill>
          </div>
          {statusText && (
            <span
              className={clsx('diagram-toolbar-status', {
                'diagram-toolbar-status--error': statusTone === 'danger',
                'diagram-toolbar-status--warning': statusTone === 'warning',
              })}
            >
              {statusText}
            </span>
          )}
          {feedbackPosition === 'start' && feedbackStatus}
        </div>
        <div className="diagram-toolbar-actions">
          {feedbackPosition === 'end' && feedbackStatus}
          {toolbarActions.map(action => (
            <ToolbarButton key={action.id} title={action.title} onClick={action.onClick}>
              {action.icon}
            </ToolbarButton>
          ))}
          {toolbarActions.length > 0 && <div className="diagram-toolbar-divider" />}
          <DiagramMoreMenu title={t('diagram.moreActions')} items={menuItems} />
        </div>
      </div>
      {children}
    </div>
  );
}
