import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { Box, ShieldCheck, X } from 'lucide-react';
import { webRequest } from '../../services/webClient';
import { SecurityConfigSwitch as ConfigSwitch } from './SecurityConfigSwitch';

type Level = 'allow' | 'ask' | 'deny';
type Axes = Partial<Record<'read' | 'write' | 'exec', Level>>;
type PathRule = Axes & { path: string; match?: 'prefix' | 'glob' };
type FileGuard = { enabled?: boolean; defaults?: Axes; workspace?: Axes; paths?: PathRule[] };

export function FileSecurityEditor({ isConnected }: { isConnected: boolean }) {
  const { t } = useTranslation();
  const [guard, setGuard] = useState<FileGuard | null>(null);
  const [saved, setSaved] = useState<FileGuard | null>(null);
  const [sandbox, setSandbox] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [reload, setReload] = useState(0);
  const lock = useRef(false);
  const dirty = JSON.stringify(guard) !== JSON.stringify(saved);
  const disabled = !isConnected || busy || loading;
  const inputClass = 'rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text min-w-0 outline-none focus:border-accent';
  const cardClass = 'rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm';

  useEffect(() => {
    if (!error && !message) return;
    const timer = window.setTimeout(
      () => {
        setError('');
        setMessage('');
      },
      error || message.includes('\n') ? 10000 : 5000,
    );
    return () => window.clearTimeout(timer);
  }, [error, message]);

  useEffect(() => {
    if (!isConnected) return;
    let cancelled = false;
    setLoading(true);
    setError('');
    void Promise.allSettled([
      webRequest<{ file_guard: FileGuard }>('permissions.file_guard.get', {}),
      webRequest<{ enabled: boolean }>('sandbox.enabled.get', {}),
    ]).then(([fileResult, sandboxResult]) => {
      if (cancelled) return;
      const errors: string[] = [];
      if (fileResult.status === 'fulfilled') {
        setGuard(fileResult.value.file_guard);
        setSaved(fileResult.value.file_guard);
      } else {
        setGuard(null);
        setSaved(null);
        errors.push(String(fileResult.reason?.message || fileResult.reason));
      }
      if (sandboxResult.status === 'fulfilled') setSandbox(sandboxResult.value.enabled);
      else {
        setSandbox(null);
        errors.push(String(sandboxResult.reason?.message || sandboxResult.reason));
      }
      setError(errors.join('\n'));
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [isConnected, reload]);

  async function run(action: () => Promise<string>) {
    if (disabled || lock.current) return;
    lock.current = true;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      setMessage(await action());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  function levelSelect(value: Level | undefined, label: string, change: (level: Level | undefined) => void, inheritable = false) {
    return (
      <select aria-label={label} className={inputClass} value={value || ''} onChange={e => change((e.target.value || undefined) as Level | undefined)}>
        {(inheritable || !value) && (
          <option value="" disabled={!inheritable}>
            {t('fileSecurity.inherit')}
          </option>
        )}
        {(['allow', 'ask', 'deny'] as const).map(level => (
          <option key={level} value={level}>
            {t(`fileSecurity.${level}`)}
          </option>
        ))}
      </select>
    );
  }

  function changePath(index: number, patch: Partial<PathRule>) {
    setGuard(current => current && { ...current, paths: (current.paths || []).map((rule, i) => (i === index ? { ...rule, ...patch } : rule)) });
    setMessage('');
  }

  async function save() {
    if (!guard || !saved) return '';
    const patch: Partial<FileGuard> = {};
    for (const key of ['enabled', 'defaults', 'workspace', 'paths'] as const) {
      if (JSON.stringify(guard[key]) !== JSON.stringify(saved[key])) Object.assign(patch, { [key]: guard[key] });
    }
    if (guard.paths?.some(rule => !rule.path.trim())) throw new Error(t('fileSecurity.pathRequired'));
    const result = await webRequest<{ file_guard: FileGuard }>('permissions.file_guard.update', { patch });
    setGuard(result.file_guard);
    setSaved(result.file_guard);
    return t('fileSecurity.saved');
  }

  return (
    <div className="space-y-3">
      <section className={cardClass} aria-labelledby="sandbox-config-title">
        <div className="flex items-center gap-3 px-4 py-3 bg-secondary/30 border-b border-border">
          <span className="inline-flex items-center justify-center rounded-md border w-7 h-7 shrink-0 text-accent bg-accent/10 border-accent/20">
            <Box className="w-4 h-4" />
          </span>
          <div>
            <h3 id="sandbox-config-title" className="text-sm font-medium text-text-strong">
              {t('fileSecurity.sandbox')}
            </h3>
            <p className="text-xs text-text-muted">{t('sandboxSecurity.hint')}</p>
          </div>
        </div>
        <fieldset disabled={disabled || sandbox === null} className="min-w-0 disabled:opacity-60">
          <ConfigSwitch
            label={t('fileSecurity.sandboxEnabled')}
            checked={sandbox === true}
            onChange={enabled => {
              void run(async () => {
                const result = await webRequest<{ enabled: boolean }>('sandbox.enabled.set', { enabled });
                setSandbox(result.enabled);
                return t('fileSecurity.switchSaved');
              });
            }}
          />
          <div className="px-4 py-3 space-y-3">
            <p className="text-xs text-text-muted">{t('sandboxSecurity.syncHint')}</p>
            {dirty && <p className="text-xs text-warn">{t('fileSecurity.saveFirst')}</p>}
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="btn text-xs disabled:opacity-50"
                disabled={dirty || !guard || guard.enabled === false}
                onClick={() =>
                  void run(async () => {
                    const result = await webRequest<{ files: PathRule[]; skipped?: { path: string; reason: string }[] }>('sandbox.files.sync', {});
                    if (result.skipped?.length) {
                      return `${t('fileSecurity.syncedPartial', { count: result.files.length, skipped: result.skipped.length })}\n${result.skipped.map(item => `${item.path}: ${item.reason}`).join('\n')}`;
                    }
                    return t('fileSecurity.synced');
                  })
                }
              >
                {t('fileSecurity.sync')}
              </button>
              <button
                type="button"
                className="btn text-xs disabled:opacity-50"
                onClick={() =>
                  void run(async () => {
                    const result = await webRequest<{ skipped: { pattern: string; reason: string }[] }>('sandbox.network.sync', {});
                    const skipped = result.skipped.map(item => `${item.pattern}: ${item.reason}`).join('\n');
                    return t('sandboxSecurity.networkSynced') + (skipped ? `\n${skipped}` : '');
                  })
                }
              >
                {t('sandboxSecurity.syncNetwork')}
              </button>
              <button
                type="button"
                className="btn primary text-xs disabled:opacity-50"
                disabled={!sandbox || dirty}
                onClick={() =>
                  void run(async () => {
                    const result = await webRequest<{ status: string; restarted: number; network_status?: string }>('sandbox.restart', {}, { timeoutMs: 180000 });
                    if (result.network_status === 'externally_managed') return t('fileSecurity.restartedExternal', { count: result.restarted });
                    return result.status === 'no_active_sandboxes' ? t('fileSecurity.noActive') : t('fileSecurity.restarted', { count: result.restarted });
                  })
                }
              >
                {t('fileSecurity.restart')}
              </button>
            </div>
            <p className="text-xs text-text-muted">{t('fileSecurity.restartHint')}</p>
          </div>
        </fieldset>
      </section>
      <section className={cardClass} aria-labelledby="file-guard-title">
        <div className="flex items-center justify-between gap-3 px-4 py-3 bg-secondary/30 border-b border-border">
          <div className="flex items-center gap-3 min-w-0">
            <span className="inline-flex items-center justify-center rounded-md border w-7 h-7 shrink-0 text-danger bg-danger-subtle border-danger/20">
              <ShieldCheck className="w-4 h-4" />
            </span>
            <div>
              <h3 id="file-guard-title" className="text-sm font-medium text-text-strong">
                {t('fileSecurity.title')}
              </h3>
              <p className="text-xs text-text-muted">{t('fileSecurity.hint')}</p>
            </div>
          </div>
          <button type="button" className="btn text-xs disabled:opacity-50" disabled={disabled || dirty} onClick={() => setReload(n => n + 1)}>
            {t('fileSecurity.refresh')}
          </button>
        </div>
        {loading && (
          <p role="status" className="text-xs text-text-muted">
            {t('fileSecurity.loading')}
          </p>
        )}
        {guard && (
          <fieldset disabled={disabled} className="min-w-0 disabled:opacity-60">
            <ConfigSwitch label={t('fileSecurity.enabled')} checked={guard.enabled !== false} onChange={enabled => setGuard({ ...guard, enabled })} />
            {(['defaults', 'workspace'] as const).map(section => (
              <div key={section} className="flex flex-wrap items-center border-b border-border px-4 py-2.5 text-xs text-text hover:bg-secondary/25">
                <span className="w-[32%] shrink-0 text-text-muted">{t(`fileSecurity.${section}`)}</span>
                <div className="flex flex-wrap gap-3 pl-4">
                  {(['read', 'write', 'exec'] as const).map(axis => (
                    <label key={axis} className="flex items-center gap-2">
                      {t(`fileSecurity.${axis}`)}
                      {levelSelect(guard[section]?.[axis], `${t(`fileSecurity.${section}`)} ${t(`fileSecurity.${axis}`)}`, value => {
                        setGuard({ ...guard, [section]: { ...guard[section], [axis]: value } });
                        setMessage('');
                      })}
                    </label>
                  ))}
                </div>
              </div>
            ))}
            <div className="p-4 space-y-2">
              {(guard.paths || []).map((rule, index) => (
                <div key={index} className="rounded-lg border border-border bg-secondary/20 p-3 space-y-3">
                  <div className="flex gap-2">
                    <input
                      aria-label={t('fileSecurity.path')}
                      placeholder={t('fileSecurity.pathPlaceholder')}
                      className={`${inputClass} flex-1`}
                      value={rule.path}
                      onChange={e => changePath(index, { path: e.target.value })}
                    />
                    <button
                      type="button"
                      className="text-[11px] px-2 py-0.5 rounded border border-border hover:bg-danger-subtle text-danger disabled:opacity-40"
                      onClick={() => setGuard({ ...guard, paths: guard.paths?.filter((_, i) => i !== index) })}
                    >
                      {t('fileSecurity.remove')}
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-3 text-xs text-text">
                    <label className="flex items-center gap-2">
                      {t('fileSecurity.match')}
                      <select
                        className={inputClass}
                        value={rule.match || 'prefix'}
                        onChange={e => changePath(index, { match: e.target.value as PathRule['match'] })}
                      >
                        <option value="prefix">{t('fileSecurity.prefix')}</option>
                        <option value="glob">{t('fileSecurity.glob')}</option>
                      </select>
                    </label>
                    {(['read', 'write', 'exec'] as const).map(axis => (
                      <label key={axis} className="flex items-center gap-2">
                        {t(`fileSecurity.${axis}`)}
                        {levelSelect(rule[axis], `${rule.path} ${t(`fileSecurity.${axis}`)}`, value => changePath(index, { [axis]: value }), true)}
                      </label>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-3">
              <button
                type="button"
                className="btn text-xs"
                onClick={() => setGuard({ ...guard, paths: [...(guard.paths || []), { path: '', read: 'allow', write: 'deny' }] })}
              >
                {t('fileSecurity.add')}
              </button>
              <button type="button" className="btn primary text-xs disabled:opacity-50" disabled={!dirty} onClick={() => void run(save)}>
                {t('fileSecurity.save')}
              </button>
              <button
                type="button"
                className="btn text-xs disabled:opacity-50"
                disabled={!dirty}
                onClick={() => {
                  setGuard(saved);
                  setMessage('');
                }}
              >
                {t('fileSecurity.discard')}
              </button>
            </div>
          </fieldset>
        )}
      </section>
      {(error || message || busy) &&
        createPortal(
          <div className="app-toast-wrapper app-toast-wrapper--top-center" style={{ zIndex: 2101, pointerEvents: 'none' }}>
            <div
              role={error ? 'alert' : 'status'}
              className={`${error ? 'app-connection-toast' : 'app-session-toast'} animate-rise flex items-start gap-3 text-sm max-w-[calc(100vw-2rem)]`}
              style={{ pointerEvents: 'auto' }}
            >
              <span className="whitespace-pre-wrap break-words max-h-60 overflow-y-auto">{error || message || t('fileSecurity.working')}</span>
              {!busy && (
                <button
                  type="button"
                  aria-label={t('common.close')}
                  className="shrink-0 rounded p-0.5 hover:opacity-70"
                  onClick={() => {
                    setError('');
                    setMessage('');
                  }}
                >
                  <X className="h-4 w-4" />
                </button>
              )}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
