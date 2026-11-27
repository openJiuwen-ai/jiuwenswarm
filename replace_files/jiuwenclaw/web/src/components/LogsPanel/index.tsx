import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

interface LogsPanelProps {
  isConnected: boolean;
}

const WS_RECONNECT_EVENT = 'jiuwenclaw:ws-reconnect-request';

function formatLogEntries(entries: unknown[]): string {
  return entries
    .map((entry) => {
      if (typeof entry === 'string') {
        return entry;
      }
      try {
        return JSON.stringify(entry, null, 2);
      } catch {
        return String(entry);
      }
    })
    .join('\n\n');
}

function parseJsonlTail(raw: string, limit: number): unknown[] {
  const lines = raw
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(-limit);

  return lines.map((line) => {
    try {
      return JSON.parse(line);
    } catch {
      return line;
    }
  });
}

function isDebugLogEntry(entry: unknown): boolean {
  if (typeof entry === 'string') {
    return entry.includes('[ws][') || entry.includes('/__dev/ws-log');
  }
  if (!entry || typeof entry !== 'object') {
    return false;
  }
  const record = entry as Record<string, unknown>;
  if (typeof record.ts === 'string' && typeof record.payload === 'object') {
    return true;
  }
  return false;
}

export function LogsPanel({ isConnected: _isConnected }: LogsPanelProps) {
  const { t } = useTranslation();
  const [entries, setEntries] = useState<unknown[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [wsDisableCompress, setWsDisableCompress] = useState(false);
  const [wsConfigLoading, setWsConfigLoading] = useState(false);
  const preRef = useRef<HTMLPreElement | null>(null);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    try {
      const filePath = encodeURIComponent('.logs/ws-dev.log');
      const response = await fetch(`/file-api/file-content?path=${filePath}`);
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`${t('logsPanel.errors.readLogs')} (HTTP ${response.status})${detail ? `: ${detail.slice(0, 120)}` : ''}`);
      }

      const raw = await response.text();
      setEntries(parseJsonlTail(raw, 300));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('logsPanel.errors.readLogs'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  const fetchWsDebugConfig = useCallback(async () => {
    setWsConfigLoading(true);
    try {
      const response = await fetch('/file-api/ws-debug-config');
      if (!response.ok) {
        throw new Error(`${t('logsPanel.errors.readDebugConfig')} (HTTP ${response.status})`);
      }
      const payload = (await response.json()) as { wsDisableCompress?: boolean };
      setWsDisableCompress(Boolean(payload.wsDisableCompress));
    } catch (err) {
      setError(err instanceof Error ? err.message : t('logsPanel.errors.readDebugConfig'));
    } finally {
      setWsConfigLoading(false);
    }
  }, [t]);

  const toggleWsDisableCompress = useCallback(async () => {
    const nextValue = !wsDisableCompress;
    setWsConfigLoading(true);
    try {
      const response = await fetch('/file-api/ws-debug-config', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
        },
        body: JSON.stringify({
          wsDisableCompress: nextValue,
        }),
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`${t('logsPanel.errors.updateDebugConfig')} (HTTP ${response.status})${detail ? `: ${detail.slice(0, 120)}` : ''}`);
      }
      const payload = (await response.json()) as { wsDisableCompress?: boolean };
      setWsDisableCompress(Boolean(payload.wsDisableCompress));
      setError(null);
      window.dispatchEvent(new CustomEvent(WS_RECONNECT_EVENT));
    } catch (err) {
      setError(err instanceof Error ? err.message : t('logsPanel.errors.updateDebugConfig'));
    } finally {
      setWsConfigLoading(false);
    }
  }, [t, wsDisableCompress]);

  useEffect(() => {
    void fetchLogs();
    void fetchWsDebugConfig();
  }, [fetchLogs, fetchWsDebugConfig]);

  useEffect(() => {
    if (!autoRefresh) {
      return;
    }
    const timer = window.setInterval(() => {
      void fetchLogs();
    }, 3000);
    return () => {
      window.clearInterval(timer);
    };
  }, [autoRefresh, fetchLogs]);

  const visibleEntries = useMemo(
    () => (wsDisableCompress ? entries : entries.filter((entry) => !isDebugLogEntry(entry))),
    [wsDisableCompress, entries]
  );

  const content = useMemo(() => formatLogEntries(visibleEntries), [visibleEntries]);

  useEffect(() => {
    const node = preRef.current;
    if (!node) {
      return;
    }
    node.scrollTop = node.scrollHeight;
  }, [content, error]);

  return (
    <div className="flex-1 min-h-0">
      <div className="card w-full h-full flex flex-col">
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">{t('logsPanel.title')}</h2>
            <p className="text-sm text-text-muted mt-1">
              {t('logsPanel.dataSource')}: <span className="mono text-xs">logs/ws-dev.log</span>
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => void toggleWsDisableCompress()}
              className={`btn ${wsDisableCompress ? 'primary' : ''} !px-3 !py-1.5`}
              disabled={wsConfigLoading}
            >
              {wsConfigLoading
                ? t('logsPanel.debugSetting')
                : wsDisableCompress
                  ? t('logsPanel.debugOn')
                  : t('logsPanel.debugOff')}
            </button>
            <button
              onClick={() => setAutoRefresh((prev) => !prev)}
              className={`btn ${autoRefresh ? 'primary' : ''} !px-3 !py-1.5`}
            >
              {autoRefresh ? t('logsPanel.autoRefreshOn') : t('logsPanel.autoRefreshOff')}
            </button>
            <button onClick={() => void fetchLogs()} className="btn !px-3 !py-1.5">
              {t('common.refresh')}
            </button>
          </div>
        </div>

        {error ? (
          <div className="text-sm text-danger flex-1 min-h-0">{error}</div>
        ) : (
          <div className="border border-border rounded-lg bg-secondary/30 flex-1 min-h-0 flex flex-col">
            <div className="px-3 py-2 text-xs text-text-muted border-b border-border flex items-center justify-between">
              <span>{t('logsPanel.recentLogs')}</span>
              <span>
                {loading
                  ? t('common.loading')
                  : wsDisableCompress
                    ? t('logsPanel.countVisible', { count: visibleEntries.length })
                    : t('logsPanel.countVisibleTotal', { visible: visibleEntries.length, total: entries.length })}
              </span>
            </div>
            <pre ref={preRef} className="m-0 p-3 text-xs mono overflow-auto flex-1 min-h-0 whitespace-pre-wrap break-all">
              {content || t('logsPanel.empty')}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}
