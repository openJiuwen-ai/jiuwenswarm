import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { Activity, RefreshCw, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Dialog } from '../ui/Dialog/Dialog';
import { LlmAuditClient, type AuditCall, type AuditChunk, type AuditCollection, type AuditPage, type AuditParts } from './llmAuditClient';
import './LlmCallAudit.css';

export function LlmCallAudit({ sessionId }: { sessionId: string }) {
  const { t } = useTranslation();
  const titleId = useId();
  const [open, setOpen] = useState(false);
  const [calls, setCalls] = useState<AuditCall[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<AuditCall | null>(null);
  const [collection, setCollection] = useState<AuditCollection>('messages');
  const [parts, setParts] = useState<AuditParts | null>(null);
  const [chunk, setChunk] = useState<AuditChunk | null>(null);
  const [partIndex, setPartIndex] = useState<number | null>(null);
  const [offsets, setOffsets] = useState<number[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const client = useRef<LlmAuditClient | null>(null);
  const generation = useRef(0);
  const detailGeneration = useRef(0);
  const viewingOlderCalls = useRef(false);

  const loadCalls = useCallback(async (api: LlmAuditClient, epoch: number, after: string | null = null) => {
    viewingOlderCalls.current = Boolean(after);
    setLoading(true);
    try {
      const page = await api.query<AuditPage>(sessionId, { action: 'list', cursor: after, limit: 30 });
      if (generation.current !== epoch) return;
      setCalls((previous) => after ? [...previous, ...page.calls] : page.calls);
      setSelected((previous) => previous ? page.calls.find((call) => call.call_id === previous.call_id) ?? previous : null);
      setCursor(page.next_cursor);
      setError('');
    } catch {
      if (generation.current === epoch) setError(t('chat.llmAudit.loadFailed'));
    } finally {
      if (generation.current === epoch) setLoading(false);
    }
  }, [sessionId, t]);

  useEffect(() => {
    if (!open) return;
    const epoch = ++generation.current;
    const api = new LlmAuditClient();
    client.current = api;
    setCalls([]);
    setSelected(null);
    setParts(null);
    setChunk(null);
    setError('');
    setLoading(true);
    viewingOlderCalls.current = false;
    let timer: ReturnType<typeof setInterval> | undefined;
    void api.connect().then(async () => {
      if (generation.current !== epoch) return;
      await loadCalls(api, epoch);
      if (generation.current === epoch) timer = setInterval(() => {
        if (!viewingOlderCalls.current) void loadCalls(api, epoch);
      }, 3000);
    }).catch(() => {
      if (generation.current === epoch) {
        setError(t('chat.llmAudit.loadFailed'));
        setLoading(false);
      }
    });
    return () => {
      generation.current += 1;
      detailGeneration.current += 1;
      if (timer) clearInterval(timer);
      client.current = null;
      api.close();
    };
  }, [open, sessionId, loadCalls, t]);

  const loadParts = async (call: AuditCall, nextCollection: AuditCollection, start = 0) => {
    const api = client.current;
    if (!api) return;
    const epoch = generation.current;
    const detail = ++detailGeneration.current;
    setSelected(call);
    setCollection(nextCollection);
    setParts(null);
    setChunk(null);
    setPartIndex(null);
    setOffsets([]);
    try {
      const page = await api.query<AuditParts>(sessionId, {
        action: 'parts', call_id: call.call_id, collection: nextCollection, start, limit: 30,
      });
      if (generation.current === epoch && detailGeneration.current === detail) {
        setParts(page);
        setError('');
      }
    } catch {
      if (generation.current === epoch && detailGeneration.current === detail) setError(t('chat.llmAudit.loadFailed'));
    }
  };

  const readPart = async (index: number, offset = 0, previousOffsets: number[] = []) => {
    const api = client.current;
    if (!api || !selected) return;
    const epoch = generation.current;
    const detail = ++detailGeneration.current;
    try {
      const value = await api.query<AuditChunk>(sessionId, {
        action: 'read', call_id: selected.call_id, collection, index, offset, max_bytes: 65536,
      });
      if (generation.current !== epoch || detailGeneration.current !== detail) return;
      setChunk(value);
      setPartIndex(index);
      setOffsets(previousOffsets);
      setError('');
    } catch {
      if (generation.current === epoch && detailGeneration.current === detail) setError(t('chat.llmAudit.loadFailed'));
    }
  };

  return <>
    <button type="button" className="chat-header-icon-btn" data-testid="chat-panel-llm-audit-open"
      title={t('chat.llmAudit.title')} aria-label={t('chat.llmAudit.title')} onClick={() => setOpen(true)}>
      <Activity size={16} aria-hidden />
    </button>
    <Dialog open={open} titleId={titleId} className="llm-audit-dialog" onCancel={() => setOpen(false)}
      onBackdropClick={() => setOpen(false)}>
      <section data-testid="chat-panel-llm-audit-dialog">
        <header className="llm-audit-header">
          <h2 id={titleId} data-testid="chat-panel-llm-audit-title">{t('chat.llmAudit.title')}</h2>
          <button type="button" data-testid="chat-panel-llm-audit-refresh" aria-label={t('chat.llmAudit.refresh')}
            disabled={loading} onClick={() => client.current && void loadCalls(client.current, generation.current)}>
            <RefreshCw size={16} aria-hidden />
          </button>
          <button type="button" data-testid="chat-panel-llm-audit-close" aria-label={t('chat.llmAudit.close')}
            onClick={() => setOpen(false)}><X size={18} aria-hidden /></button>
        </header>
        {error && <p role="alert" data-testid="chat-panel-llm-audit-error">{error}</p>}
        <div className="llm-audit-body">
          <aside className="llm-audit-calls" data-testid="chat-panel-llm-audit-calls">
            {!calls.length && <p data-testid="chat-panel-llm-audit-empty">
              {t(loading ? 'chat.llmAudit.loading' : 'chat.llmAudit.empty')}
            </p>}
            {calls.map((call) => <button key={call.call_id} type="button" className="llm-audit-call"
              data-testid="chat-panel-llm-audit-call" data-variant={call.call_id}
              aria-pressed={selected?.call_id === call.call_id} onClick={() => void loadParts(call, 'messages')}>
              <span>{call.subagent_type || call.member_name || call.model_name}</span>
              <span>{new Date(call.started_ns / 1e6).toLocaleTimeString()} · {t(`chat.llmAudit.${call.status}`)}</span>
              <small>{call.call_id}</small>
            </button>)}
            {cursor && <button type="button" data-testid="chat-panel-llm-audit-more-calls" disabled={loading}
              onClick={() => client.current && void loadCalls(client.current, generation.current, cursor)}>
              {t('chat.llmAudit.more')}
            </button>}
          </aside>
          <article className="llm-audit-detail" data-testid="chat-panel-llm-audit-detail">
            {selected ? <>
              <p data-testid="chat-panel-llm-audit-summary">
                {selected.model_name} · {t(`chat.llmAudit.${selected.status}`)}
                {selected.latency_ms != null && ` · ${Math.round(selected.latency_ms)} ms`}
                {selected.usage && ` · ${t('chat.llmAudit.tokens')}: ${selected.usage.total_tokens ?? '—'}`}
              </p>
              {(selected.error || selected.capture_error) && <p role="status" data-testid="chat-panel-llm-audit-capture-error">
                {selected.error || selected.capture_error}
              </p>}
              <nav className="llm-audit-tabs" aria-label={t('chat.llmAudit.original')}>
                {(['messages', 'tools', 'response'] as const).map((tab) => <button key={tab} type="button"
                  data-testid="chat-panel-llm-audit-collection" data-variant={tab} aria-pressed={collection === tab}
                  onClick={() => void loadParts(selected, tab)}>{t(`chat.llmAudit.${tab}`)}</button>)}
              </nav>
              <div className="llm-audit-parts" data-testid="chat-panel-llm-audit-parts">
                {parts?.parts.map((part) => <button key={part.index} type="button"
                  data-testid="chat-panel-llm-audit-part" data-variant={part.index}
                  aria-pressed={partIndex === part.index} onClick={() => void readPart(part.index)}>
                  {t(`chat.llmAudit.${collection}`)} {part.index + 1} · {part.bytes} B
                </button>)}
                {parts && parts.parts.length > 0 && <>
                  {parts.parts[0].index > 0 && <button type="button" data-testid="chat-panel-llm-audit-previous-parts"
                    onClick={() => void loadParts(selected, collection, Math.max(0, parts.parts[0].index - 30))}>
                    {t('chat.llmAudit.previous')}
                  </button>}
                  {parts.parts[parts.parts.length - 1].index + 1 < parts.total && <button type="button"
                    data-testid="chat-panel-llm-audit-next-parts" onClick={() => void loadParts(selected, collection,
                      parts.parts[parts.parts.length - 1].index + 1)}>{t('chat.llmAudit.next')}</button>}
                </>}
              </div>
              {chunk && <>
                <p data-testid="chat-panel-llm-audit-byte-range">{chunk.offset}–{chunk.next_offset} / {chunk.bytes} B</p>
                <pre className="llm-audit-original" data-testid="chat-panel-llm-audit-original">{chunk.content}</pre>
                <div className="llm-audit-tabs">
                  {offsets.length > 0 && <button type="button" data-testid="chat-panel-llm-audit-previous-chunk"
                    onClick={() => partIndex != null && void readPart(partIndex, offsets[offsets.length - 1], offsets.slice(0, -1))}>
                    {t('chat.llmAudit.previous')}
                  </button>}
                  {!chunk.eof && <button type="button" data-testid="chat-panel-llm-audit-next-chunk"
                    onClick={() => partIndex != null && void readPart(partIndex, chunk.next_offset, [...offsets, chunk.offset])}>
                    {t('chat.llmAudit.next')}
                  </button>}
                </div>
              </>}
            </> : <p data-testid="chat-panel-llm-audit-select">{t('chat.llmAudit.select')}</p>}
          </article>
        </div>
      </section>
    </Dialog>
  </>;
}
