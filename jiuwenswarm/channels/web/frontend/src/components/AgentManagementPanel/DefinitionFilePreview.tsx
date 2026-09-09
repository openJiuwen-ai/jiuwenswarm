import { useEffect, useMemo, useState } from 'react';
import { ArrowDownToLine, ChevronDown, ChevronRight, FileCode2, FileText } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { DefinitionFileEntry, RequestStatus } from '../../features/agentManagement';
import { isPreviewableFile } from '../../features/agentManagement';
import FileCopyIcon from '../../assets/agent-management/file-copy.svg?react';
import FolderAssetIcon from '../../assets/work-mode/folder.svg?react';
import FolderFoldAssetIcon from '../../assets/work-mode/folder-fold.svg?react';
import { CodePreview } from '../ArtifactsPanel/CodePreview';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { executeDesktopSave, type DesktopSaveApiResult } from '../../utils/desktopSave';

type DefinitionFilePreviewProps = {
  files: DefinitionFileEntry[];
  filesStatus: RequestStatus;
  filesError: string | null;
  selectedFilePath: string | null;
  fileContent: { relativePath: string; content: string } | null;
  fileStatus: RequestStatus;
  fileError: string | null;
  onRetryFiles: () => void;
  onSelectFile: (relativePath: string) => void;
};

function getLabel(path: string): string {
  return path.replace(/\/$/, '').split('/').filter(Boolean).pop() || path;
}

const CODE_FILE_PATTERN = /\.(?:bash|c|cc|cfg|conf|cpp|css|env|go|h|hpp|html?|ini|ipynb|java|js|json|jsx|mjs|php|py|pyw|rb|rs|sh|sql|swift|toml|ts|tsx|vue|xml|yaml|yml)$/i;

function isCodeFile(fileName: string): boolean {
  return CODE_FILE_PATTERN.test(fileName);
}

function splitMarkdownFrontMatter(content: string): { frontMatter: string | null; body: string } {
  const match = /^(?:\uFEFF)?---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(content);
  if (!match) return { frontMatter: null, body: content };
  return { frontMatter: match[1], body: content.slice(match[0].length) };
}

function findExpandedDirectories(entries: DefinitionFileEntry[]): Set<string> {
  const expanded = new Set<string>();
  const visit = (items: DefinitionFileEntry[]) => {
    for (const item of items) {
      if (item.visible === false || item.kind !== 'directory') continue;
      expanded.add(item.relativePath);
      visit(item.children || []);
    }
  };
  visit(entries);
  return expanded;
}

function TreeEntry({
  entry,
  depth,
  expanded,
  onToggle,
  selectedFilePath,
  onSelectFile,
}: {
  entry: DefinitionFileEntry;
  depth: number;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  selectedFilePath: string | null;
  onSelectFile: (path: string) => void;
}) {
  if (entry.visible === false) return null;
  const isDirectory = entry.kind === 'directory';
  const isExpanded = expanded.has(entry.relativePath);
  const label = getLabel(entry.relativePath);
  const isSkillDefinition = !isDirectory && label.toLowerCase() === 'skill.md';
  return (
    <div>
      <button
        type="button"
        className={`agent-management-file-entry${selectedFilePath === entry.relativePath ? ' is-selected' : ''}${!isDirectory && !entry.previewable ? ' is-unsupported' : ''}${isSkillDefinition ? ' is-skill-definition' : ''}`}
        style={{ paddingLeft: `calc(${depth} * var(--agent-management-file-indent) + var(--agent-management-file-pad))` }}
        onClick={() => (isDirectory ? onToggle(entry.relativePath) : onSelectFile(entry.relativePath))}
        aria-label={label}
        title={entry.relativePath}
      >
        <span className="agent-management-file-entry__chevron" aria-hidden="true">
          {isDirectory ? isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} /> : null}
        </span>
        <span className="agent-management-file-entry__icon" aria-hidden="true">
          {isDirectory ? (
            isExpanded ? <FolderFoldAssetIcon width={15} height={15} /> : <FolderAssetIcon width={15} height={15} />
          ) : isCodeFile(label) ? <FileCode2 size={16} strokeWidth={1.5} /> : <FileText size={16} strokeWidth={1.5} />}
        </span>
        <span className="agent-management-file-entry__label">{label}</span>
      </button>
      {isDirectory && isExpanded ? (
        <div>
          {(entry.children || []).map(child => (
            <TreeEntry
              key={child.relativePath}
              entry={child}
              depth={depth + 1}
              expanded={expanded}
              onToggle={onToggle}
              selectedFilePath={selectedFilePath}
              onSelectFile={onSelectFile}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function DefinitionFilePreview({
  files,
  filesStatus,
  filesError,
  selectedFilePath,
  fileContent,
  fileStatus,
  fileError,
  onRetryFiles,
  onSelectFile,
}: DefinitionFilePreviewProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const selectedIsPreviewable = selectedFilePath ? isPreviewableFile(selectedFilePath) : false;
  const selectedIsMarkdown = selectedFilePath ? /\.mdx?$/.test(selectedFilePath.toLowerCase()) : false;
  const selectedIsPython = selectedFilePath?.toLowerCase().endsWith('.py') ?? false;
  const markdownParts = useMemo(() => splitMarkdownFrontMatter(fileContent?.content || ''), [fileContent]);
  const formattedContent = useMemo(() => {
    if (!fileContent || !fileContent.relativePath.toLowerCase().endsWith('.json')) return fileContent?.content || '';
    try {
      return JSON.stringify(JSON.parse(fileContent.content), null, 2);
    } catch {
      return fileContent.content;
    }
  }, [fileContent]);

  useEffect(() => {
    if (filesStatus === 'success') setExpanded(findExpandedDirectories(files));
  }, [files, filesStatus]);

  const toggleFolder = (path: string) => {
    setExpanded(current => {
      const next = new Set(current);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const handleCopy = async () => {
    if (!fileContent) return;
    try {
      await navigator.clipboard.writeText(fileContent.content);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
    window.setTimeout(() => setCopyState('idle'), 1600);
  };

  const handleDownload = async () => {
    if (!fileContent) return;
    const filename = getLabel(fileContent.relativePath);
    const blob = new Blob([fileContent.content], { type: 'text/plain;charset=utf-8' });
    const pywebviewApi = (window as Window & { pywebview?: { api?: { download_file?: (url: string, filename: string) => DesktopSaveApiResult } } }).pywebview?.api;
    if (pywebviewApi?.download_file) {
      try{
        const dataUrl = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as string);
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(blob);
        });
        const outcome = await executeDesktopSave(() => pywebviewApi.download_file!(dataUrl, filename));
        if (outcome === 'failed') {
          window.alert(t('artifacts.downloadFailed', { name: filename }));
        }
      }catch {
        window.alert(t('artifacts.downloadFailed', { name: filename }));
      }
      return;
    }
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);
  };

  return (
    <div className="agent-management-file-preview" data-testid="agent-file-preview">
      <aside className="agent-management-file-tree" aria-label={t('agentManagement.files.treeLabel')}>
        {filesStatus === 'loading' ? <div className="agent-management-file-state">{t('common.loading')}</div> : null}
        {filesStatus === 'error' ? (
          <div className="agent-management-file-state agent-management-file-state--error">
            <p>{filesError || t('agentManagement.files.loadError')}</p>
            <button type="button" className="agent-management-button agent-management-button--secondary" onClick={onRetryFiles}>
              {t('common.retry')}
            </button>
          </div>
        ) : null}
        {filesStatus === 'success' && files.length === 0 ? <div className="agent-management-file-state">{t('agentManagement.files.empty')}</div> : null}
        {filesStatus === 'success'
          ? files.map(entry => (
              <TreeEntry
                key={entry.relativePath}
                entry={entry}
                depth={0}
                expanded={expanded}
                onToggle={toggleFolder}
                selectedFilePath={selectedFilePath}
                onSelectFile={onSelectFile}
              />
            ))
          : null}
      </aside>
      <section className="agent-management-file-content" aria-live="polite">
        {!selectedFilePath ? <div className="agent-management-file-state">{t('agentManagement.files.select')}</div> : null}
        {selectedFilePath && !selectedIsPreviewable ? <div className="agent-management-file-state">{t('agentManagement.files.notPreviewable')}</div> : null}
        {selectedFilePath && selectedIsPreviewable ? (
          <>
            <header className="agent-management-file-content__header">
              <span title={selectedFilePath}>{getLabel(selectedFilePath)}</span>
              <div className="agent-management-file-content__actions">
                <button
                  type="button"
                  onClick={handleCopy}
                  disabled={!fileContent || fileStatus !== 'success'}
                  aria-label={t('agentManagement.files.copy')}
                  title={t('agentManagement.files.copy')}
                >
                  <FileCopyIcon width={16} height={16} aria-hidden="true" />
                  {copyState === 'copied' ? t('agentManagement.files.copied') : copyState === 'failed' ? t('agentManagement.files.copyFailed') : null}
                </button>
                <button
                  type="button"
                  onClick={handleDownload}
                  disabled={!fileContent || fileStatus !== 'success'}
                  aria-label={t('agentManagement.files.download')}
                  title={t('agentManagement.files.download')}
                >
                  <ArrowDownToLine size={16} strokeWidth={1.5} aria-hidden="true" />
                </button>
              </div>
            </header>
            <div className="agent-management-file-content__body">
              {fileStatus === 'loading' ? <div className="agent-management-file-state">{t('common.loading')}</div> : null}
              {fileStatus === 'error' ? (
                <div className="agent-management-file-state agent-management-file-state--error">{fileError || t('agentManagement.files.readError')}</div>
              ) : null}
              {fileStatus === 'success' &&
              fileContent &&
              selectedIsMarkdown ? (
                <article className="agent-management-markdown">
                  {markdownParts.frontMatter ? <pre className="agent-management-markdown__frontmatter">{markdownParts.frontMatter}</pre> : null}
                  <MarkdownRenderer
                    content={markdownParts.body || ' '}
                    className="prose prose-sm max-w-none agent-management-markdown__body"
                  />
                </article>
              ) : null}
              {fileStatus === 'success' && fileContent && selectedIsPython ? (
                <div className="agent-management-code-preview">
                  <CodePreview content={fileContent.content} name={getLabel(fileContent.relativePath)} />
                </div>
              ) : null}
              {fileStatus === 'success' && fileContent && selectedFilePath.toLowerCase().endsWith('.json') ? (
                <pre className="agent-management-code">{formattedContent || ' '}</pre>
              ) : null}
            </div>
          </>
        ) : null}
      </section>
    </div>
  );
}
