import { useState } from 'react';
import { ChevronLeft, Download, MessageSquareQuote, MoreHorizontal, PencilLine, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from '../../components/ui/DropdownMenu/DropdownMenu';
import { downloadPreviewFile } from '../../components/ui/FilePreviewPanel/filePreviewShared';
import { FileIcon } from '../../components/FileIcon';
import { artifactDownloadUrl } from '../../components/ArtifactsPanel/filePreviewModel';
import { FilePreview } from '../../components/ArtifactsPanel/FilePreview';
import { formatFileSize, safeAttachmentUrl } from './state';

export type GroupFile = Record<string, unknown>;

/** Display name, workspace path and download URL shared by file rows, attachments and the preview panel. */
export function groupFileTarget(file: GroupFile, fallbackName: string) {
  const path = typeof file.path === 'string' ? file.path : undefined;
  const name = String(file.name || file.filename || path || fallbackName);
  const url = safeAttachmentUrl(file.download_url ?? file.url) ?? artifactDownloadUrl({ path }) ?? undefined;
  return { path, name, url };
}

/** Side-panel file preview that replaces the modal dialog. */
export function GroupFilePreviewPanel({ file, onBack }: { file: GroupFile; onBack: () => void }) {
  const { t } = useTranslation();
  const { path, name, url } = groupFileTarget(file, t('groupChat.attachment'));
  return <div className="group-chat-preview" data-testid="group-chat-file-preview-panel">
    <header className="group-chat-preview-header">
      <button type="button" className="group-chat-icon-button" aria-label={t('groupChat.backToPanel')}
        title={t('groupChat.backToPanel')} onClick={onBack} data-testid="group-chat-file-preview-back">
        <ChevronLeft size={18} aria-hidden />
      </button>
      <h2 title={name} data-testid="group-chat-file-preview-title">{name}</h2>
      <button type="button" className="group-chat-icon-button" disabled={!url} aria-label={t('groupChat.download')}
        title={t('groupChat.download')} onClick={() => void downloadPreviewFile({ filename: name, downloadUrl: url })}
        data-testid="group-chat-file-preview-download"><Download size={16} aria-hidden /></button>
    </header>
    <div className="group-chat-preview-body" data-testid="group-chat-file-preview-body">
      <FilePreview key={path || url || name} artifact={{ id: path || url || name, name, path, downloadUrl: url }} />
    </div>
  </div>;
}

/** One row in the "all files" panel: click to preview, hover to download, quote, rename or delete. */
export function GroupFileRow({ file, onPreview, onQuote, onRename, onDelete }: {
  file: GroupFile;
  onPreview: (file: GroupFile) => void;
  onQuote: (reference: string) => void;
  onRename: (path: string, name: string) => void;
  onDelete: (path: string, name: string) => void;
}) {
  const { t } = useTranslation();
  const [menuOpen, setMenuOpen] = useState(false);
  const { path, name, url } = groupFileTarget(file, t('groupChat.attachment'));
  const size = typeof file.size === 'number' && file.size > 0 ? formatFileSize(file.size) : '';
  return <div className={`group-chat-file-row${menuOpen ? ' is-active' : ''}`} data-testid="group-chat-file-row"
    data-variant={path || name}>
    <button type="button" className="group-chat-file-row-open" onClick={() => onPreview(file)}
      title={t('artifacts.openPreview', { name })} data-testid="group-chat-file-open">
      <FileIcon fileName={name} size={18} />
      <span className="group-chat-file-row-name">{name}</span>
      {size && <span className="group-chat-file-row-size" data-testid="group-chat-file-size">{size}</span>}
    </button>
    <span className="group-chat-file-row-actions">
      <button type="button" className="group-chat-icon-button" disabled={!url} aria-label={t('groupChat.download')}
        title={t('groupChat.download')} onClick={() => void downloadPreviewFile({ filename: name, downloadUrl: url })}
        data-testid="group-chat-file-download"><Download size={15} aria-hidden /></button>
      <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
        <DropdownMenuTrigger className="group-chat-icon-button" aria-label={t('groupChat.moreActions')}
          title={t('groupChat.moreActions')} data-testid="group-chat-file-more">
          <MoreHorizontal size={15} aria-hidden />
        </DropdownMenuTrigger>
        <DropdownMenuContent side="bottom" align="end" className="group-chat-row-menu" data-testid="group-chat-file-menu">
          <DropdownMenuItem icon={<MessageSquareQuote size={15} />} onSelect={() => onQuote(path || name)}
            data-testid="group-chat-file-quote">{t('groupChat.quoteToChat')}</DropdownMenuItem>
          <DropdownMenuItem icon={<PencilLine size={15} />} disabled={!path} onSelect={() => path && onRename(path, name)}
            data-testid="group-chat-file-rename">{t('groupChat.rename')}</DropdownMenuItem>
          <DropdownMenuItem icon={<Trash2 size={15} />} danger disabled={!path} onSelect={() => path && onDelete(path, name)}
            data-testid="group-chat-file-delete">{t('groupChat.deleteShort')}</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </span>
  </div>;
}
