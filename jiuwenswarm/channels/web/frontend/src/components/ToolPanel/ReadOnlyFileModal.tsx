/**
 * ReadOnlyFileModal Component
 *
 * Modal for displaying file content in read-only mode.
 * Used in auto_harness mode to preview extension files.
 */

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';

interface ReadOnlyFileModalProps {
  open: boolean;
  filePath: string;
  fileName: string;
  onClose: () => void;
}

function isPreviewableFile(fileName: string): boolean {
  const lowerName = fileName.toLowerCase();
  return (
    lowerName.endsWith('.md') ||
    lowerName.endsWith('.mdx') ||
    lowerName.endsWith('.json') ||
    lowerName.endsWith('.yaml') ||
    lowerName.endsWith('.yml') ||
    lowerName.endsWith('.py') ||
    lowerName.endsWith('.txt')
  );
}

function isMarkdownFile(fileName: string): boolean {
  const lowerName = fileName.toLowerCase();
  return lowerName.endsWith('.md') || lowerName.endsWith('.mdx');
}

function isJsonFile(fileName: string): boolean {
  return fileName.toLowerCase().endsWith('.json');
}

export function ReadOnlyFileModal({ open, filePath, fileName, onClose }: ReadOnlyFileModalProps) {
  const { t } = useTranslation();
  const [content, setContent] = useState<string>('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fileEncoding, setFileEncoding] = useState<string>('auto');

  useEffect(() => {
    if (!open) return;

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [open, onClose]);

  useEffect(() => {
    if (!open || !filePath) {
      setContent('');
      setError(null);
      setLoading(false);
      return;
    }

    const loadFile = async () => {
      setLoading(true);
      setError(null);

      try {
        const encodedPath = encodeURIComponent(filePath);
        const url = `/file-api/file-content?path=${encodedPath}&encoding=${fileEncoding}`;
        const response = await fetch(url, { cache: 'no-store' });

        if (!response.ok) {
          const errorData = await response.text();
          throw new Error(`HTTP ${response.status}: ${errorData.substring(0, 100)}`);
        }

        const text = await response.text();
        setContent(text);
      } catch (err) {
        console.error('Failed to load file:', err);
        setError(err instanceof Error ? err.message : t('fileViewer.unknownError'));
      } finally {
        setLoading(false);
      }
    };

    loadFile();
  }, [open, filePath, fileName, t, fileEncoding]);

  if (!open) {
    return null;
  }

  const previewable = isPreviewableFile(fileName);
  const isMarkdown = isMarkdownFile(fileName);
  const isJson = isJsonFile(fileName);

  const renderContent = () => {
    if (loading) {
      return (
        <div className="h-full flex items-center justify-center">
          <div className="w-7 h-7 rounded-full border-4 border-border border-t-accent animate-spin" />
        </div>
      );
    }

    if (error) {
      return (
        <div className="h-full flex items-center justify-center text-danger text-sm">
          {error}
        </div>
      );
    }

    if (!previewable) {
      return (
        <div className="h-full flex items-center justify-center text-text-muted text-sm">
          {t('toolPanel.fileNotPreviewable')}
        </div>
      );
    }

    if (isMarkdown) {
      return (
        <article className="chat-text max-w-none">
          <ReactMarkdown>{content || ' '}</ReactMarkdown>
        </article>
      );
    }

    if (isJson) {
      try {
        const parsed = JSON.parse(content);
        const formatted = JSON.stringify(parsed, null, 2);
        return (
          <pre className="text-sm text-text mono whitespace-pre-wrap break-all overflow-auto">
            {formatted || ' '}
          </pre>
        );
      } catch {
        return (
          <pre className="text-sm text-text mono whitespace-pre-wrap break-all overflow-auto">
            {content || ' '}
          </pre>
        );
      }
    }

    // Other previewable files (py, yaml, txt)
    return (
      <pre className="text-sm text-text mono whitespace-pre-wrap break-all overflow-auto">
        {content || ' '}
      </pre>
    );
  };

  return (
    <div className="fixed inset-0 z-[2100] flex items-center justify-center p-4">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-label={t('common.close')}
      />
      <div className="relative w-full max-w-5xl max-h-[85vh] overflow-hidden rounded-xl border border-border bg-card shadow-2xl animate-rise">
        <div className="flex items-center justify-between gap-3 px-5 py-3 border-b border-border bg-panel">
          <div className="min-w-0 flex-1">
            <h3 className="text-base font-semibold text-text truncate">{fileName}</h3>
            <p className="text-xs text-text-muted mono truncate mt-1" title={filePath}>
              {filePath}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-text-muted">Encoding:</label>
            <select
              value={fileEncoding}
              onChange={(e) => setFileEncoding(e.target.value)}
              className="rounded border border-border bg-bg px-2 py-1 text-xs text-text"
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
          <button
            type="button"
            onClick={onClose}
            className="px-2.5 py-1.5 rounded-md border border-border bg-secondary/50 text-text-muted hover:text-text hover:bg-secondary "
          >
            {t('common.close')}
          </button>
        </div>
        <div className="p-5 overflow-auto max-h-[calc(85vh-64px)]">
          {renderContent()}
        </div>
      </div>
    </div>
  );
}
