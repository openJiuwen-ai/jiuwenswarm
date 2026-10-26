import { useEffect, useRef, useState } from 'react';
import { MediaItem } from '../../types';
import {
  FileTypeIcon,
  getFileTypeIconKeyFromFilename,
  splitFilenameParts,
} from './FileTypeIcon';
import { stripUploadDocumentBlocks } from '../../utils/documentMessage';
import { resolveApiUrl } from '../../utils/env';

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
  if (item.type === 'image') return true;
  const mime = (item.mimeType || item.mime_type || '').toLowerCase();
  return mime.startsWith('image/');
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
  // 服务端返回的相对地址需拼接接口前缀；绝对地址由 resolveApiUrl 原样返回。
  if (item.url) return resolveApiUrl(item.url);
  // Documents keep local absolute paths for agent @path refs; raw-file is project-scoped.
  if (item.path && isImageItem(item)) {
    return resolveApiUrl(`/file-api/raw-file?path=${encodeURIComponent(item.path)}`);
  }
  return undefined;
}

function FileCard({ item }: { item: MediaItem }) {
  const filename = item.filename || 'file';
  const { extLabel } = splitFilenameParts(filename);
  const typeKey = getFileTypeIconKeyFromFilename(filename, item.type);
  const src = mediaSrc(item);
  const showThumb = isImageItem(item) && Boolean(src);

  const body = (
    <>
      <span className="chat-msg-file-card__icon">
        {showThumb ? (
          <img src={src} alt="" className="chat-msg-file-card__thumb" />
        ) : (
          <FileTypeIcon typeKey={typeKey} size={28} />
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
        href={src}
        download={filename}
        title={filename}
      >
        {body}
      </a>
    );
  }

  return (
    <div className="chat-msg-file-card" title={filename}>
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
    <div className="chat-msg-file-more" ref={rootRef}>
      <button
        type="button"
        className="chat-msg-file-more__btn"
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((prev) => !prev)}
      >
        +{items.length}
      </button>
      {open && (
        <div className="chat-msg-file-more__menu" role="menu">
          {items.map((item, index) => {
            const filename = item.filename || 'file';
            const typeKey = getFileTypeIconKeyFromFilename(filename, item.type);
            const src = mediaSrc(item);
            const showThumb = isImageItem(item) && Boolean(src);
            const content = (
              <>
                {showThumb ? (
                  <img src={src} alt="" className="chat-msg-file-more__thumb" />
                ) : (
                  <FileTypeIcon typeKey={typeKey} size={20} />
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
              <div key={`${filename}-${index}`} className="chat-msg-file-more__item" role="menuitem">
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
    <div className={`chat-msg-file-row chat-msg-file-row--${align}`}>
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
        <audio controls className="w-full">
          <source src={src} type={mimeType} />
        </audio>
      );
    case 'video':
      return (
        <video controls className="chat-msg-media-image">
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
    <div className={variant === 'above' ? 'chat-msg-attachments chat-msg-attachments--above' : 'chat-msg-attachments'}>
      {cardItems.length > 0 && <FileAttachmentBar items={cardItems} align={align} />}
      {richItems.length > 0 && (
        <div className={`chat-msg-media-rich chat-msg-media-rich--${align}`}>
          {richItems.map((item, index) => (
            <MediaItemView key={`${item.filename}-${index}`} item={item} />
          ))}
        </div>
      )}
    </div>
  );
}
