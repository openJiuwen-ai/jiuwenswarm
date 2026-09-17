import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import {
  parseHistoryJsonFilePreviewMode,
  parseHistoryJsonFileToTimelinePreview,
} from '../../features/historyRestore';
import { isHistoryPreviewFile, parseHistoryFileContent } from '../../features/historyFilePreview';
import { ChatTimelineList } from '../ChatPanel/MessageList';
import '../ChatPanel/ChatPanel.css';

interface FileViewerProps {
  filePath: string;
  fileName: string;
  reloadNonce?: number;
}

interface TodoPreviewItem {
  id: string;
  status: string;
  content: string;
}

function parseTodoJsonFileToPreview(parsed: unknown): TodoPreviewItem[] {
  if (!Array.isArray(parsed)) {
    return [];
  }
  return parsed
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    .map((item) => ({
      id: typeof item.id === 'string' ? item.id : String(item.id ?? ''),
      status: typeof item.status === 'string' ? item.status.toLowerCase() : 'pending',
      content: typeof item.content === 'string' ? item.content : '',
    }));
}

function sessionIdFromAgentPath(filePath: string): string {
  const m = filePath.match(/sess_[a-zA-Z0-9_]+/);
  return m ? m[0] : 'file';
}

function VirtualizedTextViewer({ text }: { text: string }) {
  const lineHeight = 20;
  const overscan = 8;
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(0);

  const lines = useMemo(() => (text || ' ').split(/\r\n|\n|\r/), [text]);
  const totalHeight = Math.max(lines.length * lineHeight, lineHeight);
  const visibleCount = Math.max(1, Math.ceil(viewportHeight / lineHeight) + overscan * 2);
  const startIndex = Math.max(0, Math.floor(scrollTop / lineHeight) - overscan);
  const endIndex = Math.min(lines.length, startIndex + visibleCount);
  const visibleLines = lines.slice(startIndex, endIndex);

  const measure = useCallback((node: HTMLDivElement | null) => {
    if (!node) {
      return;
    }
    setViewportHeight(node.clientHeight);
  }, []);

  return (
    <div
      ref={measure}
      className="w-full h-full min-h-[280px] overflow-auto rounded-lg border border-border bg-card p-3 text-sm text-text mono"
      onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      data-testid="agent-panel-file-viewer-text-viewer-scroll"
    >
      <div style={{ height: totalHeight, position: 'relative' }}>
        <pre
          className="absolute left-0 m-0 min-w-full whitespace-pre"
          style={{
            top: startIndex * lineHeight,
            lineHeight: `${lineHeight}px`,
            width: 'max-content',
          }}
        >
          {visibleLines.join('\n')}
        </pre>
      </div>
    </div>
  );
}

export function FileViewer({ filePath, fileName, reloadNonce = 0 }: FileViewerProps) {
  const { t } = useTranslation();
  const [content, setContent] = useState<string>('');
  const [draftContent, setDraftContent] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [fileEncoding, setFileEncoding] = useState<string>('auto');
  const lowerFileName = fileName.toLowerCase();
  const isMarkdown = lowerFileName.endsWith('.md') || lowerFileName.endsWith('.mdx');
  const isHistoryJson = isHistoryPreviewFile(fileName);
  const isJson = lowerFileName.endsWith('.json') || lowerFileName.endsWith('.jsonl');
  const isTodoJson = lowerFileName === 'todo.json';
  const isPreviewable = isMarkdown || isJson;
  const fileNotFound = Boolean(error && error.includes('HTTP 404'));
  const [historyChatPreview, setHistoryChatPreview] = useState(true);

  const jsonParseResult = useMemo(() => {
    if (!isJson || !content.trim()) {
      return {
        parsed: null as unknown,
        invalid: false,
      };
    }
    try {
      const parsed: unknown = isHistoryJson ? parseHistoryFileContent(content) : JSON.parse(content);
      return { parsed, invalid: false };
    } catch {
      return { parsed: null as unknown, invalid: true };
    }
  }, [isJson, isHistoryJson, content]);

  const historyPreview = useMemo(() => {
    if (!isHistoryJson || !historyChatPreview || !Array.isArray(jsonParseResult.parsed)) {
      return null;
    }
    return parseHistoryJsonFileToTimelinePreview(
      jsonParseResult.parsed,
      sessionIdFromAgentPath(filePath)
    );
  }, [isHistoryJson, historyChatPreview, jsonParseResult.parsed, filePath]);

  const historyMessages = historyPreview?.messages ?? [];
  const historyExecutions = historyPreview?.executions ?? [];
  const historyReasoningSegments = historyPreview?.reasoningSegments ?? [];

  const historyPreviewMode = useMemo(() => {
    if (historyPreview?.mode) {
      return historyPreview.mode;
    }
    if (!isHistoryJson || !Array.isArray(jsonParseResult.parsed)) {
      return null;
    }
    return parseHistoryJsonFilePreviewMode(jsonParseResult.parsed);
  }, [historyPreview?.mode, isHistoryJson, jsonParseResult.parsed]);

  const todoItems = useMemo(() => {
    if (!isTodoJson || !Array.isArray(jsonParseResult.parsed)) {
      return [] as TodoPreviewItem[];
    }
    return parseTodoJsonFileToPreview(jsonParseResult.parsed);
  }, [isTodoJson, jsonParseResult.parsed]);

  const formattedJson = useMemo(() => {
    if (!isJson || isHistoryJson || jsonParseResult.invalid || jsonParseResult.parsed === null) {
      return content;
    }
    return JSON.stringify(jsonParseResult.parsed, null, 2);
  }, [isJson, isHistoryJson, jsonParseResult.invalid, jsonParseResult.parsed, content]);

  const historyInvalid = isHistoryJson && jsonParseResult.invalid;

  useEffect(() => {
    if (!filePath) return;
    if (!isPreviewable) {
      setLoading(false);
      setError(null);
      setSaveError(null);
      setIsEditing(false);
      setSaving(false);
      setContent('');
      setDraftContent('');
      return;
    }

    const loadFile = async () => {
      setLoading(true);
      setError(null);
      setSaveError(null);
      setIsEditing(false);
      setSaving(false);

      try {
        const encodedPath = encodeURIComponent(filePath);
        const url = `/file-api/file-content?path=${encodedPath}&encoding=${fileEncoding}`;
        const response = await fetch(url, { cache: 'no-store' });

        if (!response.ok) {
          const errorData = await response.text();
          throw new Error(`HTTP ${response.status}: ${errorData.substring(0, 100)}`);
        }

        const originalEncoding = response.headers.get('X-Original-Encoding');
        if (originalEncoding && fileEncoding === 'auto') {
          console.log(`File encoding detected: ${originalEncoding}`);
        }

        const text = await response.text();
        setContent(text);
        setDraftContent(text);
      } catch (err) {
        console.error('Failed to load file:', err);
        setError(err instanceof Error ? err.message : t('fileViewer.unknownError'));
      } finally {
        setLoading(false);
      }
    };

    loadFile();
  }, [filePath, fileName, isPreviewable, reloadNonce, fileEncoding]);

  useEffect(() => {
    if (isHistoryJson) {
      setHistoryChatPreview(true);
    }
  }, [filePath, isHistoryJson]);

  const handleStartEdit = () => {
    setDraftContent(content);
    setSaveError(null);
    setIsEditing(true);
  };

  const handleCancelEdit = () => {
    setDraftContent(content);
    setSaveError(null);
    setIsEditing(false);
  };

  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const response = await fetch('/file-api/file-content', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
        },
        body: JSON.stringify({
          path: filePath,
          content: draftContent,
        }),
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`HTTP ${response.status}: ${errorText.substring(0, 120)}`);
      }

      setContent(draftContent);
      setIsEditing(false);
    } catch (err) {
      console.error('Failed to save file:', err);
      setSaveError(err instanceof Error ? err.message : t('fileViewer.unknownError'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="h-full min-h-0 flex flex-col overflow-hidden" data-testid="agent-panel-file-viewer-root">
      <div className="flex-shrink-0 px-4 py-3 bg-secondary/30 border-b border-border" data-testid="agent-panel-file-viewer-toolbar">
        <div className="flex items-stretch justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            <span className="h-9 w-9 rounded-lg border border-border bg-card flex items-center justify-center text-text-muted flex-shrink-0" data-testid="agent-panel-file-viewer-icon">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="h-7 w-7">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
              </svg>
            </span>
            <div className="min-w-0 flex-1">
              <h3 className="text-sm font-medium text-text truncate" data-testid="agent-panel-file-viewer-file-name">{fileName}</h3>
              <p className="text-xs text-text-muted mono truncate mt-1" title={filePath} data-testid="agent-panel-file-viewer-file-path">
                {filePath}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-text-muted" data-testid="agent-panel-file-viewer-encoding-label">Encoding:</label>
            <select
              value={fileEncoding}
              onChange={(e) => setFileEncoding(e.target.value)}
              className="rounded border border-border bg-bg px-2 py-1 text-xs text-text"
              data-testid="agent-panel-file-viewer-encoding-select"
            >
              <option value="auto">Auto Detect</option>
              <option value="utf-8">UTF-8</option>
              <option value="utf-16">UTF-16</option>
              <option value="utf-16le">UTF-16LE</option>
              <option value="utf-16be">UTF-16BE</option>
              <option value="gbk">GBK</option>
              <option value="gb2312">GB2312</option>
              <option value="big5">Big5</option>
              <option value="shift_jis">Shift_JIS</option>
              <option value="euc_kr">EUC-KR</option>
              <option value="iso-8859-1">ISO-8859-1</option>
            </select>
          </div>
          {isMarkdown && !loading ? (
            <div className="flex flex-shrink-0 items-center gap-2 self-stretch">
              {isEditing ? (
                <>
                  <button
                    type="button"
                    className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                    onClick={handleCancelEdit}
                    disabled={saving}
                    data-testid="agent-panel-file-viewer-edit-cancel-button"
                  >
                    {t('common.cancel')}
                  </button>
                  <button
                    type="button"
                    className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                    onClick={handleSave}
                    disabled={saving}
                    data-testid="agent-panel-file-viewer-edit-save-button"
                  >
                    {saving ? t('common.saving') : t('common.save')}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="btn !px-3 !py-1.5"
                  onClick={handleStartEdit}
                  data-testid="agent-panel-file-viewer-edit-button"
                >
                  {t('fileViewer.edit')}
                </button>
              )}
            </div>
          ) : null}
          {isHistoryJson && isJson && !loading ? (
            <div className="flex flex-shrink-0 items-center gap-2 self-stretch">
              <span className="inline-flex items-center gap-1 text-xs leading-snug text-text-muted whitespace-nowrap" data-testid="agent-panel-file-viewer-chat-preview-label">
                {t('fileViewer.chatPreview')}
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={historyChatPreview}
                onClick={() => setHistoryChatPreview((v) => !v)}
                className={`inline-flex h-8 w-12 shrink-0 items-center rounded-full border border-border p-1  ${
                  historyChatPreview ? 'justify-end bg-accent' : 'justify-start bg-secondary'
                }`}
                title={t('fileViewer.chatPreview')}
                data-testid="agent-panel-file-viewer-chat-preview-switch"
              >
                <span className="pointer-events-none h-5 w-5 rounded-full bg-[var(--color-control-thumb)] shadow-sm ring-1 ring-control-ring" />
              </button>
            </div>
          ) : null}
        </div>
        {error ? (
          <div className="mt-2 rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger" data-testid="agent-panel-file-viewer-error">
            {error}
          </div>
        ) : null}
        {fileNotFound ? (
          <div className="mt-2 rounded-md border border-warn/30 bg-warn/10 px-2.5 py-1.5 text-xs text-warn" data-testid="agent-panel-file-viewer-file-missing">
            {t('fileViewer.fileMissingPrefix')} <span className="mono">{filePath}</span> {t('fileViewer.fileMissingSuffix')}
          </div>
        ) : null}
        {saveError ? (
          <div className="mt-2 rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger" data-testid="agent-panel-file-viewer-save-error">
            {saveError}
          </div>
        ) : null}
      </div>

      <div className="flex-1 min-h-0 overflow-auto p-5" data-testid="agent-panel-file-viewer-body">
        {loading ? (
          <div className="h-full flex items-center justify-center" data-testid="agent-panel-file-viewer-loading">
            <div className="w-7 h-7 rounded-full border-4 border-border border-t-accent animate-spin" />
          </div>
        ) : isMarkdown ? (
          isEditing ? (
            <textarea
              className="w-full h-full min-h-[280px] resize-none rounded-lg border border-border bg-card p-3 text-sm text-text outline-none focus:border-accent/50"
              value={draftContent}
              onChange={(event) => setDraftContent(event.target.value)}
              disabled={saving}
              data-testid="agent-panel-file-viewer-edit-textarea"
            />
          ) : (
            <article className="chat-text max-w-none" data-testid="agent-panel-file-viewer-markdown">
              <ReactMarkdown>{content || ' '}</ReactMarkdown>
            </article>
          )
        ) : isJson ? (
          isTodoJson && todoItems.length > 0 ? (
            <div className="w-full min-h-[280px] rounded-lg border border-border bg-card p-4 space-y-4" data-testid="agent-panel-file-viewer-todo-preview">
              {(() => {
                const inProgress = todoItems.filter((i) => i.status === 'in_progress');
                const pending = todoItems.filter((i) => i.status === 'pending');
                const completed = todoItems.filter((i) => i.status === 'completed');
                const cancelled = todoItems.filter((i) => i.status === 'cancelled');

                const renderGroup = (title: string, items: TodoPreviewItem[], colorClass: string, icon: string) => (
                  items.length > 0 ? (
                    <div key={title} data-testid="agent-panel-file-viewer-todo-group" data-variant={title}>
                      <div className="flex items-center gap-2 mb-2">
                        <span className={`text-xs font-medium ${colorClass}`} data-testid="agent-panel-file-viewer-todo-group-title">{title}</span>
                        <span className="text-xs px-1.5 py-0.5 rounded bg-secondary text-text-muted" data-testid="agent-panel-file-viewer-todo-group-count">{items.length}</span>
                      </div>
                      <div className="space-y-1.5">
                        {items.map((item) => (
                          <div key={item.id} className="flex items-center gap-2 text-sm py-1.5 px-2 rounded bg-secondary/30" data-testid="agent-panel-file-viewer-todo-item" data-variant={item.id}>
                            <span className={`shrink-0 ${colorClass}`}>{icon}</span>
                            <span className="flex-1 text-text truncate" data-testid="agent-panel-file-viewer-todo-item-content">{item.content}</span>
                            <span className="mono text-xs text-text-muted shrink-0 bg-secondary px-1.5 py-0.5 rounded" data-testid="agent-panel-file-viewer-todo-item-id">{item.id}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null
                );

                return (
                  <>
                    {renderGroup('进行中', inProgress, 'text-accent', '▶')}
                    {renderGroup('待处理', pending, 'text-text-muted', '○')}
                    {renderGroup('已完成', completed, 'text-green-600', '✓')}
                    {renderGroup('已取消', cancelled, 'text-danger', '×')}
                  </>
                );
              })()}
            </div>
          ) : isHistoryJson && historyChatPreview ? (
            historyInvalid ? (
              <VirtualizedTextViewer text={content} />
            ) : historyMessages.length === 0 ? (
              <div className="h-full min-h-[280px] flex items-center justify-center rounded-lg border border-border bg-card px-4 text-sm text-text-muted text-center" data-testid="agent-panel-file-viewer-history-empty">
                {t('fileViewer.historyPreviewEmpty')}
              </div>
            ) : (
              <div className="w-full min-h-[280px] rounded-lg border border-border bg-card p-3 chat-content" data-testid="agent-panel-file-viewer-history-timeline">
                <ChatTimelineList
                  messages={historyMessages}
                  executions={historyExecutions}
                  reasoningSegments={historyReasoningSegments}
                  staticTimeline
                  mode={historyPreviewMode ?? undefined}
                  disableA2UIInteraction={true}
                />
              </div>
            )
          ) : (
            <div data-testid="agent-panel-file-viewer-text-viewer">
              <VirtualizedTextViewer text={isHistoryJson ? content : formattedJson} />
            </div>
          )
        ) : (
          <div className="h-full flex items-center justify-center text-text-muted text-sm" data-testid="agent-panel-file-viewer-not-previewable">
            {t('fileViewer.notPreviewable')}
          </div>
        )}
      </div>
    </div>
  );
}
