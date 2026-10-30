import type { CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import { CodePreview } from '../../../../../components/ArtifactsPanel/CodePreview';
import { previewKind } from '../../../../../components/ArtifactsPanel/filePreviewModel';
import type { WebUIFileContent, WebUIFileReadFailure } from '../../fileResources.ts';
import './FilePreview.css';

export interface FilePreviewProps {
  file: WebUIFileContent | null;
  status?: 'ready' | WebUIFileReadFailure;
  emptyText?: string;
  className?: string;
  style?: CSSProperties;
}

export function FilePreview({ file, status: sourceStatus = 'ready', emptyText, className, style }: FilePreviewProps) {
  const { t } = useTranslation();
  const kind = file ? previewKind(file) : 'unsupported';
  const status = sourceStatus !== 'ready' ? sourceStatus : !file ? 'empty'
    : ['markdown', 'text', 'code', 'json', 'jsonl', 'html'].includes(kind) ? 'ready' : 'unsupported';
  return (
    <section className={`webui-file-preview${className ? ` ${className}` : ''}`} data-testid="webui-file-preview"
      style={style}>
      <header className="webui-file-preview__header" title={file?.path}
        data-testid="webui-file-preview-title">{file?.name ?? t('webuiFiles.previewTitle')}</header>
      <div className="webui-file-preview__body" data-testid="webui-file-preview-body">
        {status !== 'ready' ? <div className="webui-file-preview__notice" data-testid="webui-file-preview-state"
          data-variant={status}>{status === 'empty' ? emptyText ?? t('webuiFiles.preview.empty')
            : t(`webuiFiles.preview.${status}`)}</div> :
          kind === 'code' && file ? <CodePreview content={file.content} name={file.name} mimeType={file.mimeType} /> :
          <pre className="m-0 h-full w-full overflow-auto whitespace-pre text-xs text-text"
            data-testid="webui-file-preview-text">{file?.content ?? ''}</pre>}
      </div>
    </section>
  );
}
