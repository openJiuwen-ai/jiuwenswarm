import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ShieldCheck, X } from 'lucide-react';
import { webRequest } from '../../services/webClient';
import { ShellRulesEditor } from './ShellRulesEditor';

type ShellGuard = { builtin_rules_enabled: boolean };

export function ShellSecurityEditor({ isConnected }: { isConnected: boolean }) {
  const { t } = useTranslation();
  const [guard, setGuard] = useState<ShellGuard | null>(null);
  const [saved, setSaved] = useState<ShellGuard | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [reload, setReload] = useState(0);
  const lock = useRef(false);
  const generation = useRef(0);
  const dirty = guard?.builtin_rules_enabled !== saved?.builtin_rules_enabled;
  const disabled = !isConnected || loading || busy;

  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(''), 5000);
    return () => window.clearTimeout(timer);
  }, [message]);

  useEffect(() => {
    const current = ++generation.current;
    if (!isConnected) return;
    setLoading(true);
    setError('');
    void webRequest<{ shell_guard: ShellGuard }>('permissions.shell_guard.get', {})
      .then(result => {
        if (generation.current !== current) return;
        setGuard(result.shell_guard);
        setSaved(result.shell_guard);
      })
      .catch(e => {
        if (generation.current !== current) return;
        setGuard(null);
        setSaved(null);
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (generation.current === current) setLoading(false);
      });
    return () => {
      generation.current++;
    };
  }, [isConnected, reload]);

  async function save() {
    if (disabled || lock.current || !guard || !dirty) return;
    const current = generation.current;
    lock.current = true;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const result = await webRequest<{ shell_guard: ShellGuard }>('permissions.shell_guard.update', { patch: guard });
      if (generation.current !== current) return;
      setGuard(result.shell_guard);
      setSaved(result.shell_guard);
      setMessage(t('shellSecurity.saved'));
    } catch (e) {
      if (generation.current === current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      lock.current = false;
      setBusy(false);
    }
  }

  return (
    <section className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm" aria-labelledby="shell-guard-title">
      <div className="flex items-center justify-between gap-3 px-4 py-3 bg-secondary/30 border-b border-border">
        <div className="flex items-center gap-3 min-w-0">
          <span className="inline-flex items-center justify-center rounded-md border w-7 h-7 shrink-0 text-danger bg-danger-subtle border-danger/20">
            <ShieldCheck className="w-4 h-4" />
          </span>
          <div>
            <h3 id="shell-guard-title" className="text-sm font-medium text-text-strong">
              {t('shellSecurity.title')}
            </h3>
            <p className="text-xs text-text-muted">{t('shellSecurity.hint')}</p>
          </div>
        </div>
        <button
          type="button"
          className="btn text-xs disabled:opacity-50"
          disabled={disabled || dirty}
          onClick={() => {
            setMessage('');
            setReload(n => n + 1);
          }}
        >
          {t('fileSecurity.refresh')}
        </button>
      </div>
      {loading && (
        <p role="status" className="px-4 py-2 text-xs text-text-muted">
          {t('fileSecurity.loading')}
        </p>
      )}
      {guard && (
        <div className="flex items-center border-b border-border px-4 py-2.5">
          <span className="w-[32%] shrink-0 text-xs text-text-muted">{t('shellSecurity.builtinRules')}</span>
          <div className="flex h-[calc(1.25rem+16px)] items-center pl-4">
            <button
              type="button"
              role="switch"
              aria-label={t('shellSecurity.builtinRules')}
              aria-checked={guard.builtin_rules_enabled}
              disabled={disabled}
              onClick={() => {
                setGuard({ builtin_rules_enabled: !guard.builtin_rules_enabled });
                setMessage('');
              }}
              className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-60 ${guard.builtin_rules_enabled ? 'bg-[var(--color-toggle-enabled)]' : 'bg-[var(--color-toggle-disabled)]'}`}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${guard.builtin_rules_enabled ? 'translate-x-4' : 'translate-x-0'}`}
              />
            </button>
          </div>
        </div>
      )}
      <div className="px-4 py-3 space-y-3">
        <p className="text-xs text-text-muted">{t('shellSecurity.builtinHint')}</p>
        {error && (
          <p role="alert" className="text-xs text-danger">
            {error}
          </p>
        )}
        <button type="button" className="btn text-xs disabled:opacity-50" disabled={disabled || !guard || !dirty} onClick={() => void save()}>
          {t('fileSecurity.save')}
        </button>
      </div>
      <ShellRulesEditor isConnected={isConnected} />
      {message &&
        createPortal(
          <div className="app-toast-wrapper app-toast-wrapper--top-center" style={{ zIndex: 2101, pointerEvents: 'none' }}>
            <div
              className="app-session-toast animate-rise flex items-start gap-3 text-sm max-w-[calc(100vw-2rem)]"
              role="status"
              style={{ pointerEvents: 'auto' }}
            >
              <span className="whitespace-pre-wrap break-words">{message}</span>
              <button type="button" aria-label={t('common.close')} className="shrink-0 rounded p-0.5 hover:opacity-70" onClick={() => setMessage('')}>
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>,
          document.body,
        )}
    </section>
  );
}
