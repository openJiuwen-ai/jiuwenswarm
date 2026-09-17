import { useEffect, useRef, useState } from 'react';
import { MediaItem } from '../../types';
import { FileIcon, getFileExtensionLabel } from '../FileIcon';
import { stripUploadDocumentBlocks } from '../../utils/documentMessage';

export { stripUploadDocumentBlocks };

interface MediaRendererProps {
  items: MediaItem[];
  /** Align file cards to the end for user messages */
  align?: 'start' | 'end';
  /** Place attachments above the bubble (chat message layout) */
  variant?: 'inline' | 'above';
}

const VISIBLE_FILE_COUNT = 2;

function isImageItem(item: MediaItem): boolean {
  // Attachment routing owns the media/document distinction. MIME metadata alone
  // must not turn ordinary files such as SVG documents back into image previews.
  return item.type === 'image';
}

function isCardItem(item: MediaItem): boolean {
  // Images + documents share the same card row / overflow menu.
  return item.type !== 'audio' && item.type !== 'video';
}

function mediaSrc(item: MediaItem): string | undefined {
  const mimeType = item.mimeType || item.mime_type || 'application/octet-stream';
  const base64Data = item.base64Data || item.base64_data;
  if (base64Data) {
    return `data:${mimeType};base64,${base64Data}`;
  }
  if (item.url) return item.url;
  // Documents keep local absolute paths for agent @path refs; raw-file is project-scoped.
  if (item.path && isImageItem(item)) {
    return `/file-api/raw-file?path=${encodeURIComponent(item.path)}`;
  }
  return undefined;
}

function FileCard({ item }: { item: MediaItem }) {
  const filename = item.filename || 'file';
  const extLabel = getFileExtensionLabel(filename);
  const src = mediaSrc(item);
  const showThumb = isImageItem(item) && Boolean(src);

  const body = (
    <>
      <span className="chat-msg-file-card__icon">
        {showThumb ? (
          <img src={src} alt="" className="chat-msg-file-card__thumb" />
        ) : (
          <FileIcon fileName={filename} size={28} />
        )}
      </span>
      <span className="chat-msg-file-card__meta">
        <span className="chat-msg-file-card__name" title={filename}>
          {filename}
        </span>
        {extLabel ? <span className="chat-msg-file-card__ext">{extLabel}</span> : null}
      </span>
    </>
  );

  if (src) {
    return (
      <a
        className="chat-msg-file-card"
        data-testid="chat-panel-msg-file-card"
        data-variant={filename}
        href={src}
        download={filename}
        title={filename}
      >
        {body}
      </a>
    );
  }

  return (
    <div className="chat-msg-file-card" data-testid="chat-panel-msg-file-card" data-variant={filename} title={filename}>
      {body}
    </div>
  );
}

function OverflowMenu({ items }: { items: MediaItem[] }) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return (
    <div className="chat-msg-file-more" data-testid="chat-panel-msg-file-more" ref={rootRef}>
      <button
        type="button"
        className="chat-msg-file-more__btn"
        data-testid="chat-panel-msg-file-more-btn"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((prev) => !prev)}
      >
        +{items.length}
      </button>
      {open && (
        <div className="chat-msg-file-more__menu" data-testid="chat-panel-msg-file-more-menu" role="menu">
          {items.map((item, index) => {
            const filename = item.filename || 'file';
            const src = mediaSrc(item);
            const showThumb = isImageItem(item) && Boolean(src);
            const content = (
              <>
                {showThumb ? (
                  <img src={src} alt="" className="chat-msg-file-more__thumb" />
                ) : (
                  <FileIcon fileName={filename} size={20} />
                )}
                <span className="chat-msg-file-more__name" title={filename}>
                  {filename}
                </span>
              </>
            );
            if (src) {
              return (
                <a
                  key={`${filename}-${index}`}
                  className="chat-msg-file-more__item"
                  data-testid="chat-panel-msg-file-more-item"
                  data-variant={filename}
                  href={src}
                  download={filename}
                  role="menuitem"
                  onClick={() => setOpen(false)}
                >
                  {content}
                </a>
              );
            }
            return (
              <div key={`${filename}-${index}`} className="chat-msg-file-more__item" data-testid="chat-panel-msg-file-more-item" data-variant={filename} role="menuitem">
                {content}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function FileAttachmentBar({
  items,
  align = 'end',
}: {
  items: MediaItem[];
  align?: 'start' | 'end';
}) {
  if (!items.length) return null;
  const visible = items.slice(0, VISIBLE_FILE_COUNT);
  const overflow = items.slice(VISIBLE_FILE_COUNT);

  return (
    <div className={`chat-msg-file-row chat-msg-file-row--${align}`} data-testid="chat-panel-msg-file-row">
      {visible.map((item, index) => (
        <FileCard key={`${item.filename}-${index}`} item={item} />
      ))}
      {overflow.length > 0 ? <OverflowMenu items={overflow} /> : null}
    </div>
  );
}

function MediaItemView({ item }: { item: MediaItem }) {
  const mimeType = item.mimeType || item.mime_type || 'application/octet-stream';
  const src = mediaSrc(item);

  if (!src) {
    return null;
  }

  switch (item.type) {
    case 'audio':
      return (
        <audio controls className="w-full" data-testid="chat-panel-msg-media-item" data-variant="audio">
          <source src={src} type={mimeType} />
        </audio>
      );
    case 'video':
      return (
        <video controls className="chat-msg-media-image" data-testid="chat-panel-msg-media-item" data-variant="video">
          <source src={src} type={mimeType} />
        </video>
      );
    default:
      return null;
  }
}

export function MediaRenderer({ items, align = 'end', variant = 'inline' }: MediaRendererProps) {
  if (!items.length) {
    return null;
  }

  const cardItems = items.filter(isCardItem);
  const richItems = items.filter((item) => !isCardItem(item));

  return (
    <div className={variant === 'above' ? 'chat-msg-attachments chat-msg-attachments--above' : 'chat-msg-attachments'} data-testid="chat-panel-msg-attachments">
      {cardItems.length > 0 && <FileAttachmentBar items={cardItems} align={align} />}
      {richItems.length > 0 && (
        <div className={`chat-msg-media-rich chat-msg-media-rich--${align}`} data-testid="chat-panel-msg-media-rich">
          {richItems.map((item, index) => (
            <MediaItemView key={`${item.filename}-${index}`} item={item} />
          ))}
        </div>
      )}
    </div>
  );
}
