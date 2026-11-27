import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  OffloadFileListResponse,
  OffloadFileContentResponse,
} from '../../types';
import { webRequest } from '../../services/webClient';

interface OffloadFilesWidgetProps {
  sessionId: string;
}

interface SelectedFile {
  filename: string;
  content: string;
}

export function OffloadFilesWidget({ sessionId }: OffloadFilesWidgetProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(true);
  const [files, setFiles] = useState<string[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<SelectedFile | null>(null);
  const [isContentLoading, setIsContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);

  const isSessionReady = useMemo(
    () => Boolean(sessionId && sessionId !== 'new'),
    [sessionId]
  );

  const fetchFiles = useCallback(async () => {
    if (!isSessionReady) {
      setFiles([]);
      return;
    }
    setIsLoading(true);
    setLoadError(null);
    try {
      const data = await webRequest<OffloadFileListResponse>('files.list', {
        session_id: sessionId,
      });
      setFiles(data.files || []);
    } catch (error) {
      console.error('Failed to fetch offload files:', error);
      setLoadError(t('offloadFiles.errors.loadFiles'));
      setFiles([]);
    } finally {
      setIsLoading(false);
    }
  }, [isSessionReady, sessionId, t]);

  useEffect(() => {
    if (isExpanded) {
      fetchFiles();
    } else {
      setLoadError(null);
    }
  }, [isExpanded, fetchFiles]);

  useEffect(() => {
    if (!isSessionReady) {
      setFiles([]);
      setSelectedFile(null);
      setContentError(null);
    }
  }, [isSessionReady]);

  const handleOpenFile = useCallback(
    async (filename: string) => {
      if (!isSessionReady) {
        return;
      }
      setSelectedFile({ filename, content: '' });
      setIsContentLoading(true);
      setContentError(null);
      try {
        const data = await webRequest<OffloadFileContentResponse>('files.get', {
          session_id: sessionId,
          filename,
        });
        setSelectedFile({ filename, content: data.content || '' });
      } catch (error) {
        console.error('Failed to load offload file:', error);
        setContentError(t('offloadFiles.errors.loadContent'));
      } finally {
        setIsContentLoading(false);
      }
    },
    [isSessionReady, sessionId, t]
  );

  const handleCloseModal = useCallback(() => {
    setSelectedFile(null);
    setContentError(null);
    setIsContentLoading(false);
  }, []);

  return (
    <div
      className="mt-4 pt-4 border-t border-border px-2.5"
      style={{ borderColor: 'var(--border)' }}
    >
      <div
        className="flex items-center justify-between cursor-pointer"
        onClick={() => setIsExpanded((prev) => !prev)}
      >
        <div className="flex items-center gap-2">
          <div
            className="w-7 h-7 rounded-lg flex items-center justify-center"
            style={{
              background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            }}
          >
            <svg
              className="w-4 h-4 text-white"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
              strokeWidth={1.6}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M4 7h16M4 12h16M4 17h16"
              />
            </svg>
          </div>
          <div>
            <div className="text-xs font-semibold text-text-strong">
              {t('offloadFiles.title')}
            </div>
            <div className="text-[11px] text-text-muted">
              {isSessionReady ? t('offloadFiles.count', { count: files.length }) : t('offloadFiles.notConnected')}
            </div>
          </div>
        </div>
        <button
          className="w-6 h-6 rounded-md flex items-center justify-center"
          style={{ backgroundColor: 'var(--bg-elevated)' }}
          aria-label="toggle offload files"
        >
          <svg
            className={`w-3.5 h-3.5 transition-transform ${
              isExpanded ? 'rotate-180' : ''
            }`}
            viewBox="0 0 20 20"
            fill="currentColor"
          >
            <path
              fillRule="evenodd"
              d="M5.23 7.21a.75.75 0 011.06.02L10 11.1l3.71-3.87a.75.75 0 111.08 1.04l-4.24 4.42a.75.75 0 01-1.08 0L5.21 8.27a.75.75 0 01.02-1.06z"
              clipRule="evenodd"
            />
          </svg>
        </button>
      </div>

      {isExpanded && (
        <div
          className="mt-3 rounded-lg border overflow-hidden"
          style={{
            borderColor: 'var(--border)',
            backgroundColor: 'var(--panel-strong)',
          }}
        >
          <div className="max-h-40 overflow-y-auto">
            {isLoading && (
              <div className="px-3 py-2 text-xs text-text-muted">
                {t('common.loading')}
              </div>
            )}
            {!isLoading && loadError && (
              <div className="px-3 py-2 text-xs text-danger">{loadError}</div>
            )}
            {!isLoading && !loadError && files.length === 0 && (
              <div className="px-3 py-2 text-xs text-text-muted">
                {t('offloadFiles.empty')}
              </div>
            )}
            {!isLoading &&
              !loadError &&
              files.map((filename) => (
                <button
                  key={filename}
                  onClick={() => handleOpenFile(filename)}
                  className="w-full text-left px-3 py-2 text-xs transition-colors"
                  style={{
                    borderBottom: '1px solid var(--border)',
                  }}
                  onMouseOver={(event) => {
                    event.currentTarget.style.backgroundColor =
                      'var(--panel-hover)';
                  }}
                  onMouseOut={(event) => {
                    event.currentTarget.style.backgroundColor = 'transparent';
                  }}
                >
                  <div className="flex items-center gap-2">
                    <svg
                      className="w-3.5 h-3.5 text-text-muted"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                      strokeWidth={1.5}
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        d="M4 5a2 2 0 012-2h8l6 6v10a2 2 0 01-2 2H6a2 2 0 01-2-2V5z"
                      />
                    </svg>
                    <span className="truncate">{filename}</span>
                  </div>
                </button>
              ))}
          </div>
        </div>
      )}

      <OffloadFileModal
        isOpen={Boolean(selectedFile)}
        filename={selectedFile?.filename || ''}
        content={selectedFile?.content || ''}
        isLoading={isContentLoading}
        errorMessage={contentError}
        onClose={handleCloseModal}
      />
    </div>
  );
}

interface OffloadFileModalProps {
  isOpen: boolean;
  filename: string;
  content: string;
  isLoading: boolean;
  errorMessage: string | null;
  onClose: () => void;
}

function OffloadFileModal({
  isOpen,
  filename,
  content,
  isLoading,
  errorMessage,
  onClose,
}: OffloadFileModalProps) {
  const { t } = useTranslation();

  if (!isOpen) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <div
        className="relative w-full max-w-2xl max-h-[80vh] overflow-hidden rounded-xl animate-rise"
        style={{
          backgroundColor: 'var(--card)',
          boxShadow: 'var(--shadow-xl)',
        }}
      >
        <div
          className="px-6 py-4 flex items-center gap-4"
          style={{
            backgroundColor: 'var(--panel-strong)',
            borderBottom: '1px solid var(--border)',
          }}
        >
          <div
            className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0"
            style={{
              background: 'linear-gradient(135deg, var(--accent), var(--accent-2))',
            }}
          >
            <svg
              className="w-5 h-5 text-white"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 5a2 2 0 012-2h8l6 6v10a2 2 0 01-2 2H6a2 2 0 01-2-2V5z"
              />
            </svg>
          </div>
          <div className="min-w-0">
            <h2
              className="text-lg font-semibold truncate"
              style={{ color: 'var(--text-strong)' }}
            >
              {filename || t('offloadFiles.previewFallback')}
            </h2>
            <p className="text-sm" style={{ color: 'var(--muted)' }}>
              {t('offloadFiles.previewTitle')}
            </p>
          </div>
        </div>

        <div
          className="px-6 py-5 overflow-y-auto"
          style={{
            maxHeight: '50vh',
            backgroundColor: 'var(--card)',
          }}
        >
          {isLoading && (
            <div className="text-sm text-text-muted">{t('common.loading')}</div>
          )}
          {!isLoading && errorMessage && (
            <div className="text-sm text-danger">{errorMessage}</div>
          )}
          {!isLoading && !errorMessage && (
            <pre
              className="text-sm whitespace-pre-wrap break-words"
              style={{ color: 'var(--text)' }}
            >
              {content || t('offloadFiles.emptyContent')}
            </pre>
          )}
        </div>

        <div
          className="px-6 py-4 flex justify-end"
          style={{
            backgroundColor: 'var(--panel-strong)',
            borderTop: '1px solid var(--border)',
          }}
        >
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium rounded-lg transition-colors"
            style={{
              color: 'var(--muted)',
              backgroundColor: 'transparent',
            }}
            onMouseOver={(event) => {
              event.currentTarget.style.backgroundColor = 'var(--bg-hover)';
              event.currentTarget.style.color = 'var(--text)';
            }}
            onMouseOut={(event) => {
              event.currentTarget.style.backgroundColor = 'transparent';
              event.currentTarget.style.color = 'var(--muted)';
            }}
          >
            {t('common.close')}
          </button>
        </div>
      </div>
    </div>
  );
}
