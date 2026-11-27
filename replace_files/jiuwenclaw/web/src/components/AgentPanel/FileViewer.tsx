import { useState, useEffect, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import { parseHistoryJsonFileToPreviewMessages } from '../../features/historyRestore';
import { MessageItem } from '../ChatPanel/MessageItem';
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

export function FileViewer({ filePath, fileName, reloadNonce = 0 }: FileViewerProps) {
  const { t } = useTranslation();
  const [content, setContent] = useState<string>('');
  const [draftContent, setDraftContent] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const lowerFileName = fileName.toLowerCase();
  const isMarkdown = lowerFileName.endsWith('.md') || lowerFileName.endsWith('.mdx');
  const isJson = lowerFileName.endsWith('.json');
  const isHistoryJson = lowerFileName === 'history.json';
  const isTodoJson = lowerFileName === 'todo.json';
  const isPreviewable = isMarkdown || isJson;
  const fileNotFound = Boolean(error && error.includes('HTTP 404'));
  const [historyChatPreview, setHistoryChatPreview] = useState(true);

  /** 任意 .json 单次 parse，避免预览/格式化重复 JSON.parse */
  const jsonDerived = useMemo(() => {
    if (!isJson || !content.trim()) {
      return {
        historyMessages: [] as ReturnType<typeof parseHistoryJsonFileToPreviewMessages>,
        historyInvalid: false,
        todoItems: [] as TodoPreviewItem[],
        todoInvalid: false,
        formatted: content,
      };
    }
    try {
      const parsed: unknown = JSON.parse(content);
      const formatted = JSON.stringify(parsed, null, 2);
      if (!isHistoryJson && !isTodoJson) {
        return { historyMessages: [], historyInvalid: false, todoItems: [], todoInvalid: false, formatted };
      }
      if (isTodoJson) {
        if (!Array.isArray(parsed)) {
          return { historyMessages: [], historyInvalid: false, todoItems: [], todoInvalid: true, formatted };
        }
        return {
          historyMessages: [],
          historyInvalid: false,
          todoItems: parseTodoJsonFileToPreview(parsed),
          todoInvalid: false,
          formatted,
        };
      }
      if (!Array.isArray(parsed)) {
        return { historyMessages: [], historyInvalid: true, todoItems: [], todoInvalid: false, formatted };
      }
      return {
        historyMessages: parseHistoryJsonFileToPreviewMessages(parsed, sessionIdFromAgentPath(filePath)),
        historyInvalid: false,
        todoItems: [],
        todoInvalid: false,
        formatted,
      };
    } catch {
      return { historyMessages: [], historyInvalid: true, todoItems: [], todoInvalid: true, formatted: content };
    }
  }, [isJson, isHistoryJson, isTodoJson, content, filePath]);

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
        const url = `/file-api/file-content?path=${encodedPath}`;
        const response = await fetch(url, { cache: 'no-store' });

        if (!response.ok) {
          const errorData = await response.text();
          throw new Error(`HTTP ${response.status}: ${errorData.substring(0, 100)}`);
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
  }, [filePath, fileName, isPreviewable, reloadNonce]);

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
    <div className="h-full min-h-0 flex flex-col overflow-hidden">
      <div className="flex-shrink-0 px-4 py-3 bg-secondary/30 border-b border-border">
        <div className="flex items-stretch justify-between gap-4">
          <div className="flex items-center gap-3 min-w-0 flex-1">
            <span className="h-9 w-9 rounded-lg border border-border bg-card flex items-center justify-center text-text-muted flex-shrink-0">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="h-7 w-7">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
              </svg>
            </span>
            <div className="min-w-0 flex-1">
              <h3 className="text-sm font-medium text-text truncate">{fileName}</h3>
              <p className="text-xs text-text-muted mono truncate mt-1" title={filePath}>
                {filePath}
              </p>
            </div>
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
                  >
                    {t('common.cancel')}
                  </button>
                  <button
                    type="button"
                    className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                    onClick={handleSave}
                    disabled={saving}
                  >
                    {saving ? t('common.saving') : t('common.save')}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="btn !px-3 !py-1.5"
                  onClick={handleStartEdit}
                >
                  {t('fileViewer.edit')}
                </button>
              )}
            </div>
          ) : null}
          {isHistoryJson && isJson && !loading ? (
            <div className="flex flex-shrink-0 items-center gap-2 self-stretch">
              <span className="text-xs leading-snug text-text-muted whitespace-nowrap">{t('fileViewer.chatPreview')}</span>
              <button
                type="button"
                role="switch"
                aria-checked={historyChatPreview}
                onClick={() => setHistoryChatPreview((v) => !v)}
                className={`inline-flex h-8 w-12 shrink-0 items-center rounded-full border border-border p-1 transition-colors ${
                  historyChatPreview ? 'justify-end bg-accent' : 'justify-start bg-secondary'
                }`}
                title={t('fileViewer.chatPreview')}
              >
                <span className="pointer-events-none h-5 w-5 rounded-full bg-card shadow-sm ring-1 ring-black/5 dark:ring-white/10" />
              </button>
            </div>
          ) : null}
        </div>
        {error ? (
          <div className="mt-2 rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
            {error}
          </div>
        ) : null}
        {fileNotFound ? (
          <div className="mt-2 rounded-md border border-warning/30 bg-warning/10 px-2.5 py-1.5 text-xs text-warning">
            {t('fileViewer.fileMissingPrefix')} <span className="mono">{filePath}</span> {t('fileViewer.fileMissingSuffix')}
          </div>
        ) : null}
        {saveError ? (
          <div className="mt-2 rounded-md border border-danger/30 bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
            {saveError}
          </div>
        ) : null}
      </div>

      <div className="flex-1 min-h-0 overflow-auto p-5">
        {loading ? (
          <div className="h-full flex items-center justify-center">
            <div className="w-7 h-7 rounded-full border-4 border-border border-t-accent animate-spin" />
          </div>
        ) : isMarkdown ? (
          isEditing ? (
            <textarea
              className="w-full h-full min-h-[280px] resize-none rounded-lg border border-border bg-card p-3 text-sm text-text outline-none focus:border-accent/50"
              value={draftContent}
              onChange={(event) => setDraftContent(event.target.value)}
              disabled={saving}
            />
          ) : (
            <article className="chat-text max-w-none">
              <ReactMarkdown>{content || ' '}</ReactMarkdown>
            </article>
          )
        ) : isJson ? (
          isTodoJson && jsonDerived.todoItems.length > 0 ? (
            <div className="w-full min-h-[280px] rounded-lg border border-border bg-card p-4 space-y-4">
              {(() => {
                const inProgress = jsonDerived.todoItems.filter((i) => i.status === 'in_progress');
                const pending = jsonDerived.todoItems.filter((i) => i.status === 'pending');
                const completed = jsonDerived.todoItems.filter((i) => i.status === 'completed');
                const cancelled = jsonDerived.todoItems.filter((i) => i.status === 'cancelled');

                const renderGroup = (title: string, items: TodoPreviewItem[], colorClass: string, icon: string) => (
                  items.length > 0 ? (
                    <div key={title}>
                      <div className="flex items-center gap-2 mb-2">
                        <span className={`text-xs font-medium ${colorClass}`}>{title}</span>
                        <span className="text-xs px-1.5 py-0.5 rounded bg-secondary text-text-muted">{items.length}</span>
                      </div>
                      <div className="space-y-1.5">
                        {items.map((item) => (
                          <div key={item.id} className="flex items-center gap-2 text-sm py-1.5 px-2 rounded bg-secondary/30">
                            <span className={`shrink-0 ${colorClass}`}>{icon}</span>
                            <span className="flex-1 text-text truncate">{item.content}</span>
                            <span className="mono text-xs text-text-muted shrink-0 bg-secondary px-1.5 py-0.5 rounded">{item.id}</span>
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
            jsonDerived.historyInvalid ? (
              <pre className="w-full h-full min-h-[280px] overflow-auto rounded-lg border border-border bg-card p-3 text-sm text-text mono whitespace-pre-wrap break-all">
                {jsonDerived.formatted || ' '}
              </pre>
            ) : jsonDerived.historyMessages.length === 0 ? (
              <div className="h-full min-h-[280px] flex items-center justify-center rounded-lg border border-border bg-card px-4 text-sm text-text-muted text-center">
                {t('fileViewer.historyPreviewEmpty')}
              </div>
            ) : (
              <div className="w-full min-h-[280px] rounded-lg border border-border bg-card p-3">
                {jsonDerived.historyMessages.map((message) => (
                  <MessageItem key={message.id} message={message} />
                ))}
              </div>
            )
          ) : (
            <pre className="w-full h-full min-h-[280px] overflow-auto rounded-lg border border-border bg-card p-3 text-sm text-text mono whitespace-pre-wrap break-all">
              {jsonDerived.formatted || ' '}
            </pre>
          )
        ) : (
          <div className="h-full flex items-center justify-center text-text-muted text-sm">
            {t('fileViewer.notPreviewable')}
          </div>
        )}
      </div>
    </div>
  );
}
