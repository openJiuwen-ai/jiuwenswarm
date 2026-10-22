import { Pause, Play, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileIcon } from '../FileIcon';
import FolderIcon from '../../assets/work-mode/folder.svg?react';
import DownloadOpenIcon from '../../assets/download-open.svg?react';
import type { ElectronDownloadRecord } from '../../types/electron';
import './DownloadPanel.css';

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const text = unit === 0 || value >= 100 ? Math.round(value).toString() : value.toFixed(1);
  return `${text} ${units[unit]}`;
}

function formatRelativeTime(startTime: number, t: (key: string, options?: Record<string, unknown>) => string): string {
  const diffMinutes = Math.floor((Date.now() - startTime) / 60000);
  if (diffMinutes < 1) return t('browser.downloads.justNow');
  if (diffMinutes < 60) return t('browser.downloads.minutesAgo', { count: diffMinutes });
  if (diffMinutes < 1440) return t('browser.downloads.hoursAgo', { count: Math.floor(diffMinutes / 60) });
  return t('browser.downloads.daysAgo', { count: Math.floor(diffMinutes / 1440) });
}

// Chrome shows "x of y" only when the total size is known; an unknown size
// (totalBytes 0) falls back to the received bytes alone instead of "0 B/0 B".
function formatProgress(item: ElectronDownloadRecord): string {
  return item.totalBytes > 0
    ? `${formatBytes(item.receivedBytes)}/${formatBytes(item.totalBytes)}`
    : formatBytes(item.receivedBytes);
}

function formatDownloadMeta(item: ElectronDownloadRecord, t: (key: string, options?: Record<string, unknown>) => string): string {
  // Chrome 式"已删除"占据文件名下方的元信息位，替代大小/时间。
  if (item.state === 'completed' && item.fileMissing) return t('browser.downloads.fileDeleted');
  if (item.state === 'paused') {
    return `${formatProgress(item)} · ${t('browser.downloads.paused')}`;
  }
  if (item.state === 'downloading') {
    return `${formatProgress(item)} · ${t('browser.downloads.downloading')}`;
  }
  if (item.state === 'cancelled') return t('browser.downloads.cancelled');
  if (item.state === 'interrupted') return t('browser.downloads.failed');
  return `${formatBytes(item.receivedBytes)} · ${formatRelativeTime(item.endTime || item.startTime, t)}`;
}

export function DownloadPanel({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const desktop = window.jiuwenDesktop;
  const [downloads, setDownloads] = useState<ElectronDownloadRecord[]>([]);
  // Relative times ("just now" / "N minutes ago") only recompute on renders,
  // and an idle list receives no pushes — nudge a re-render on a slow tick so
  // they age forward without waiting for the next downloads-changed event.
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!desktop) return;
    const timer = window.setInterval(() => setTick(value => value + 1), 30_000);
    return () => window.clearInterval(timer);
  }, [desktop]);

  useEffect(() => {
    if (!desktop) return;
    let active = true;
    void desktop.browser.listDownloads().then(list => {
      if (active) setDownloads(list);
    });
    const unsubscribe = desktop.browser.onDownloadsChanged(list => {
      if (active) setDownloads(list);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [desktop]);

  const handleItemActivate = (item: ElectronDownloadRecord) => {
    if (!desktop) return;
    if (item.state === 'completed') {
      void desktop.browser.openDownload(item.id);
      return;
    }
    if (item.state === 'paused') {
      void desktop.browser.downloadAction(item.id, 'resume');
      return;
    }
    if (item.state === 'downloading') {
      void desktop.browser.downloadAction(item.id, 'pause');
    }
  };

  return (
    <div className="desktop-browser-download-panel" data-testid="desktop-browser-download-panel">
      <header className="desktop-browser-download-header">
        <h2>{t('browser.downloads.recentTitle')}</h2>
        <button
          type="button"
          className="desktop-browser-download-close"
          onClick={onClose}
          aria-label={t('browser.downloads.close')}
          data-testid="desktop-browser-download-close"
        >
          <X aria-hidden size={18} />
        </button>
      </header>
      {downloads.length === 0 ? (
        <div className="desktop-browser-download-empty" data-testid="desktop-browser-download-empty">
          <span>{t('browser.downloads.empty')}</span>
        </div>
      ) : (
        <ul className="desktop-browser-download-list" data-testid="desktop-browser-download-list">
          {downloads.map(item => (
            <li
              key={item.id}
              className="desktop-browser-download-item"
              data-testid="desktop-browser-download-item"
              data-variant={item.id}
              data-state={item.state}
            >
              <span className="desktop-browser-download-item-icon">
                {item.icon ? (
                  <img src={item.icon} alt="" width={22} height={22} data-testid="desktop-browser-download-item-icon" data-variant={item.id} />
                ) : (
                  // 系统图标解析失败（如文件已被删除）时，复用聊天面板文件下载
                  // 预览同一套文件类型 SVG 规则（FileIcon 按扩展名映射）。
                  <FileIcon fileName={item.filename} size={22} />
                )}
              </span>
              <span className="desktop-browser-download-item-content">
                <button
                  type="button"
                  className="desktop-browser-download-item-name"
                  title={item.filename}
                  onClick={() => handleItemActivate(item)}
                  data-testid="desktop-browser-download-item-name"
                  data-variant={item.id}
                >
                  {item.filename}
                </button>
                <span
                  className="desktop-browser-download-item-meta"
                  data-testid="desktop-browser-download-item-meta"
                  data-variant={item.id}
                >
                  {formatDownloadMeta(item, t)}
                </span>
              </span>
              {item.state === 'completed' && (
                <span className="desktop-browser-download-item-actions">
                  {!item.fileMissing && (
                    <>
                      <button
                        type="button"
                        className="desktop-browser-download-item-action"
                        onClick={() => void desktop?.browser.showDownloadInFolder(item.id)}
                        aria-label={t('browser.downloads.showInFolder')}
                        title={t('browser.downloads.showInFolder')}
                        data-testid="desktop-browser-download-item-reveal"
                        data-variant={item.id}
                      >
                        <FolderIcon aria-hidden width={16} height={16} />
                      </button>
                      <button
                        type="button"
                        className="desktop-browser-download-item-action"
                        onClick={() => void desktop?.browser.openDownload(item.id)}
                        aria-label={t('browser.downloads.openFile')}
                        title={t('browser.downloads.openFile')}
                        data-testid="desktop-browser-download-item-open"
                        data-variant={item.id}
                      >
                        <DownloadOpenIcon aria-hidden width={16} height={16} />
                      </button>
                    </>
                  )}
                  {/* 每条已完成记录都可从列表移除（Chrome "remove from list"），样式与取消按钮一致 */}
                  <button
                    type="button"
                    className="desktop-browser-download-item-cancel"
                    onClick={() => void desktop?.browser.removeDownload(item.id)}
                    aria-label={t('browser.downloads.removeRecord')}
                    title={t('browser.downloads.removeRecord')}
                    data-testid="desktop-browser-download-item-remove"
                    data-variant={item.id}
                  >
                    <X aria-hidden size={16} />
                  </button>
                </span>
              )}
              {(item.state === 'downloading' || item.state === 'paused') && (
                <span className="desktop-browser-download-item-actions">
                  <button
                    type="button"
                    className="desktop-browser-download-item-action"
                    onClick={() => void desktop?.browser.downloadAction(item.id, item.state === 'paused' ? 'resume' : 'pause')}
                    aria-label={item.state === 'paused' ? t('browser.downloads.resumeDownload') : t('browser.downloads.pauseDownload')}
                    data-testid="desktop-browser-download-item-toggle"
                    data-variant={item.id}
                  >
                    {item.state === 'paused' ? <Play aria-hidden size={16} /> : <Pause aria-hidden size={16} />}
                  </button>
                  <button
                    type="button"
                    className="desktop-browser-download-item-cancel"
                    onClick={() => void desktop?.browser.downloadAction(item.id, 'cancel')}
                    aria-label={t('browser.downloads.cancelDownload')}
                    title={t('browser.downloads.cancelDownload')}
                    data-testid="desktop-browser-download-item-cancel"
                    data-variant={item.id}
                  >
                    <X aria-hidden size={16} />
                  </button>
                </span>
              )}
              {/* 已取消/已中断的记录无法继续，但必须能像 Chrome 一样从列表移除，
                  否则会永远滞留在历史里 */}
              {(item.state === 'cancelled' || item.state === 'interrupted') && (
                <span className="desktop-browser-download-item-actions">
                  <button
                    type="button"
                    className="desktop-browser-download-item-cancel"
                    onClick={() => void desktop?.browser.removeDownload(item.id)}
                    aria-label={t('browser.downloads.removeRecord')}
                    title={t('browser.downloads.removeRecord')}
                    data-testid="desktop-browser-download-item-remove"
                    data-variant={item.id}
                  >
                    <X aria-hidden size={16} />
                  </button>
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
