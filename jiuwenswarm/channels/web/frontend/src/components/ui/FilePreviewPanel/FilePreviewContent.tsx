import { useMemo, useState } from 'react';
import { ArrowDownToLine } from 'lucide-react';
import { MarkdownRenderer } from '../../MarkdownRenderer';
import { CodePreview } from '../../ArtifactsPanel/CodePreview';
import FileCopyIcon from '../../../assets/agent-management/file-copy.svg?react';
import {
  downloadPreviewFile,
  formatJsonContent,
  getPreviewFileLabel,
  isJsonFilePath,
  isMarkdownFilePath,
  isPreviewableImagePath,
  isPythonFilePath,
  splitMarkdownFrontMatter,
  type FilePreviewStatus,
} from './filePreviewShared';

export type FilePreviewContentFile = {
  path: string;
  /** 文本内容；null/undefined 表示二进制/图片等无法按文本渲染的文件 */
  content?: string | null;
  /** 二进制/图片下载地址（图片预览也用它作为 src） */
  downloadUrl?: string | null;
};

export type FilePreviewContentLabels = {
  selectPrompt: string;
  notPreviewable: string;
  loading: string;
  readError: string;
  copy: string;
  copied: string;
  copyFailed: string;
  download: string;
  downloadFailed: string;
  noPreview: string;
  binaryDownload: string;
};

export type FilePreviewContentProps = {
  selected: boolean;
  selectedPreviewable: boolean;
  file: FilePreviewContentFile | null;
  status: FilePreviewStatus;
  errorText?: string | null;
  labels: FilePreviewContentLabels;
  testId?: string;
};

/**
 * 共享预览内容区：头部（标题 + 复制/下载）+ 内容分发
 * （Markdown 含 front matter / Python 代码高亮 / JSON 美化 / 图片 / 纯文本 / 二进制提示）。
 * 文案经 labels 由调用方注入，组件自身不耦合 i18n。
 */
export function FilePreviewContent({
  selected,
  selectedPreviewable,
  file,
  status,
  errorText,
  labels,
  testId = 'file-preview-content',
}: FilePreviewContentProps) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const isMarkdown = file ? isMarkdownFilePath(file.path) : false;
  const isPython = file ? isPythonFilePath(file.path) : false;
  const isJson = file ? isJsonFilePath(file.path) : false;
  const isImage = file ? isPreviewableImagePath(file.path) : false;
  const markdownParts = useMemo(() => splitMarkdownFrontMatter(file?.content || ''), [file]);
  const formattedContent = useMemo(
    () => (file && isJson ? formatJsonContent(file.content || '') : file?.content || ''),
    [file, isJson],
  );

  const handleCopy = async () => {
    if (!file?.content) return;
    try {
      await navigator.clipboard.writeText(file.content);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
    window.setTimeout(() => setCopyState('idle'), 1600);
  };

  const handleDownload = async () => {
    if (!file) return;
    const ok = await downloadPreviewFile({
      filename: getPreviewFileLabel(file.path),
      content: file.content,
      downloadUrl: file.downloadUrl,
    });
    if (!ok) window.alert(labels.downloadFailed);
  };

  const stateTestId = `${testId}-state`;
  return (
    <div className="file-preview-content__main" data-testid={testId}>
      {!selected ? (
        <div className="file-preview-state" data-testid={stateTestId} data-variant="empty">
          {labels.selectPrompt}
        </div>
      ) : null}
      {selected && !selectedPreviewable ? (
        <div className="file-preview-state" data-testid={stateTestId} data-variant="unsupported">
          {labels.notPreviewable}
        </div>
      ) : null}
      {selected && selectedPreviewable && file ? (
        <>
          <header className="file-preview-content__header">
            <span className="file-preview-content__header-title" title={file.path} data-testid={`${testId}-title`}>
              {getPreviewFileLabel(file.path)}
            </span>
            <div className="file-preview-content__header-actions">
              <button
                type="button"
                onClick={handleCopy}
                disabled={status !== 'success' || !file.content}
                aria-label={labels.copy}
                title={labels.copy}
                data-testid={`${testId}-copy-btn`}
              >
                <FileCopyIcon width={16} height={16} aria-hidden="true" />
                {copyState === 'copied' ? labels.copied : copyState === 'failed' ? labels.copyFailed : null}
              </button>
              <button
                type="button"
                onClick={handleDownload}
                disabled={status !== 'success' || (!file.content && !file.downloadUrl)}
                aria-label={labels.download}
                title={labels.download}
                data-testid={`${testId}-download-btn`}
              >
                <ArrowDownToLine size={16} strokeWidth={1.5} aria-hidden="true" />
              </button>
            </div>
          </header>
          <div className="file-preview-content__body" data-testid={`${testId}-body`}>
            {status === 'loading' ? (
              <div className="file-preview-state" data-testid={stateTestId} data-variant="loading">
                {labels.loading}
              </div>
            ) : null}
            {status === 'error' ? (
              <div
                className="file-preview-state file-preview-state--error"
                data-testid={stateTestId}
                data-variant="error"
              >
                {errorText || labels.readError}
              </div>
            ) : null}
            {status === 'success' && file.content != null && isMarkdown ? (
              <article className="file-preview-markdown" data-testid={`${testId}-markdown`}>
                {markdownParts.frontMatter ? (
                  <pre className="file-preview-markdown__frontmatter" data-testid={`${testId}-markdown-frontmatter`}>
                    {markdownParts.frontMatter}
                  </pre>
                ) : null}
                <MarkdownRenderer
                  content={markdownParts.body || ' '}
                  className="prose prose-sm max-w-none file-preview-markdown__body"
                />
              </article>
            ) : null}
            {status === 'success' && file.content != null && isPython ? (
              <div className="file-preview-code-frame" data-testid={`${testId}-code`}>
                <CodePreview content={file.content} name={getPreviewFileLabel(file.path)} />
              </div>
            ) : null}
            {status === 'success' && file.content != null && isJson ? (
              <pre className="file-preview-code" data-testid={`${testId}-code`}>
                {formattedContent || ' '}
              </pre>
            ) : null}
            {status === 'success' && file.content != null && !isMarkdown && !isPython && !isJson ? (
              <pre className="file-preview-code" data-testid={`${testId}-code`}>
                {file.content || ' '}
              </pre>
            ) : null}
            {status === 'success' && file.content == null && isImage && file.downloadUrl ? (
              <div className="file-preview-image" data-testid={`${testId}-image`}>
                <img src={file.downloadUrl} alt={getPreviewFileLabel(file.path)} />
              </div>
            ) : null}
            {status === 'success' && file.content == null && !(isImage && file.downloadUrl) ? (
              <div
                className="file-preview-state"
                data-testid={stateTestId}
                data-variant={file.downloadUrl ? 'binary' : 'no-preview'}
              >
                {file.downloadUrl ? labels.binaryDownload : labels.noPreview}
              </div>
            ) : null}
          </div>
        </>
      ) : null}
    </div>
  );
}
