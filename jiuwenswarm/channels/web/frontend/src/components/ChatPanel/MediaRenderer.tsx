import { useEffect, useRef, useState } from 'react';
import { MediaItem } from '../../types';
import { FileIcon, getFileExtensionLabel } from '../FileIcon';
import { displayFileName, normalizePath } from '../../features/sessionAssets/sessionAssets';
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

/** 卡片展示名字永远带扩展名：displayName 存在时是去掉扩展名的引用名（和素材列表同一套
 *  定义，见 sessionAssets.displayFileName），这里把真实文件名的扩展名拼回去；没有
 *  displayName 时文件名本身已经带扩展名，原样返回即可。 */
function fileCardDisplayName(item: MediaItem, filename: string): string {
  if (!item.displayName) return filename;
  return displayFileName({ name: item.displayName, path: filename });
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
  // 素材改过名的话（比如上传后在卡片上重命名过），这里显示新名字；图标类型、下载文件名
  // 仍然按真实文件名走，重命名只影响这一处展示文字。displayName 本身不带扩展名（素材引用
  // 名的定义就是去掉扩展名），所以这里要把扩展名拼回去，两种情况下展示的都是完整文件名。
  const displayLabel = fileCardDisplayName(item, filename);
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
        <span className="chat-msg-file-card__name" title={displayLabel}>
          {displayLabel}
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
        title={displayLabel}
      >
        {body}
      </a>
    );
  }

  return (
    <div className="chat-msg-file-card" data-testid="chat-panel-msg-file-card" data-variant={filename} title={displayLabel}>
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
            const displayLabel = fileCardDisplayName(item, filename);
            const src = mediaSrc(item);
            const showThumb = isImageItem(item) && Boolean(src);
            const content = (
              <>
                {showThumb ? (
                  <img src={src} alt="" className="chat-msg-file-more__thumb" />
                ) : (
                  <FileIcon fileName={filename} size={14} />
                )}
                <span className="chat-msg-file-more__name" title={displayLabel}>
                  {displayLabel}
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
              <div
                key={`${filename}-${index}`}
                className="chat-msg-file-more__item"
                data-testid="chat-panel-msg-file-more-item"
                data-variant={filename}
                role="menuitem"
              >
                {content}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function FileAttachmentBar({ items, align = 'end' }: { items: MediaItem[]; align?: 'start' | 'end' }) {
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

/**
 * 同一份素材只留一张卡片。
 *
 * 一张附件会被两条路径各补一张：消息自带的 mediaItems 是一张，@素材 引用再按名字解析
 * 一张（发送端 useWebSocket 和渲染端 MessageItem 各自都做了一次去重）。任一侧因为路径
 * 写法不同没对上，气泡上就会并排出现两张一样的卡片。渲染是最后一道关，这里再收一次：
 * 优先按路径判重（Windows 路径忽略大小写/斜杠方向），路径缺失时退一步按展示名——用户
 * 看到的就是名字，两张同名的卡片正是用户口中的“重复素材”。
 */
function dedupeMediaItems(items: MediaItem[]): MediaItem[] {
  if (items.length < 2) return items;
  const seenPaths = new Set<string>();
  const seenLooseNames = new Set<string>();
  const deduped: MediaItem[] = [];
  for (const item of items) {
    const label = item.displayName || item.filename || '';
    const nameKey = label
      .split(/[\\/]/)
      .pop()!
      .replace(/\.[^.]+$/, '')
      .trim()
      .toLowerCase();
    const path = normalizePath(item.path ?? '').trim();
    // 新任务里这条消息的第一个附件往往还没落盘：那条记录只有 base64 和文件名，没有路径，
    // 而 @素材 引用解析出来的那条带着落盘路径。两者的 src 也各不相同（data: 对 raw-file），
    // 所以只比路径或只比 src 都认不出是同一张卡片——名字才是两边都有的身份。
    // 两条都带路径时只认路径，避免把两个不同目录下的同名文件并成一张。
    if (path) {
      if (seenPaths.has(path) || (nameKey && seenLooseNames.has(nameKey))) continue;
      seenPaths.add(path);
    } else {
      if (nameKey && seenLooseNames.has(nameKey)) continue;
      if (nameKey) seenLooseNames.add(nameKey);
    }
    deduped.push(item);
  }
  return deduped.length === items.length ? items : deduped;
}

export function MediaRenderer({ items, align = 'end', variant = 'inline' }: MediaRendererProps) {
  if (!items.length) {
    return null;
  }

  const dedupedItems = dedupeMediaItems(items);
  const cardItems = dedupedItems.filter(isCardItem);
  const richItems = dedupedItems.filter((item) => !isCardItem(item));

  return (
    <div
      className={variant === 'above' ? 'chat-msg-attachments chat-msg-attachments--above' : 'chat-msg-attachments'}
      data-testid="chat-panel-msg-attachments"
    >
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
