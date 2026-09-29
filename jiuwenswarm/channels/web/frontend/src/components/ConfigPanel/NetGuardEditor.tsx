import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { ShieldCheck } from 'lucide-react';
import { SecurityConfigSwitch } from './SecurityConfigSwitch';

export type NetGuardEditorProps = {
  isConnected: boolean;
};

type NetAction = 'allow' | 'ask' | 'deny';

type NetGuardSection = {
  enabled: boolean;
  defaults: NetAction;
  urls: Record<string, NetAction>;
  enforceHostExit: boolean;
};

type HostExitExemption = {
  id: string;
  description: string;
};

type HostExitInfo = {
  mode: string;
  error: string | null;
  exemptions: HostExitExemption[];
};

type NetGuardView = {
  section: NetGuardSection;
  builtinUrls: Record<string, NetAction>;
  hostExit: HostExitInfo;
  warnings: string[];
};

const HOST_EXIT_MODES = new Set(['unset', 'disabled', 'not_enforced', 'active', 'error']);

function normalizeAction(value: unknown): NetAction | null {
  if (typeof value !== 'string') return null;
  const a = value.trim().toLowerCase();
  return a === 'allow' || a === 'ask' || a === 'deny' ? a : null;
}

function parseActionMap(value: unknown): Record<string, NetAction> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const out: Record<string, NetAction> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    const pattern = String(k).trim();
    const action = normalizeAction(v);
    if (pattern && action) out[pattern] = action;
  }
  return out;
}

function parsePayload(data: Record<string, unknown>): NetGuardView {
  const raw = (data.net_guard ?? {}) as Record<string, unknown>;
  const hostExit = (data.host_exit ?? {}) as Record<string, unknown>;
  const exemptions = Array.isArray(hostExit.exemptions)
    ? (hostExit.exemptions as Record<string, unknown>[]).map(e => ({
        id: String(e.id ?? ''),
        description: String(e.description ?? ''),
      }))
    : [];
  return {
    section: {
      enabled: raw.enabled === true,
      defaults: normalizeAction(raw.defaults) ?? 'allow',
      urls: parseActionMap(raw.urls),
      enforceHostExit: raw.enforce_host_exit !== false,
    },
    builtinUrls: parseActionMap(data.builtin_urls),
    hostExit: {
      mode: typeof hostExit.mode === 'string' ? hostExit.mode : 'unset',
      error: typeof hostExit.error === 'string' && hostExit.error ? hostExit.error : null,
      exemptions,
    },
    warnings: Array.isArray(data.warnings) ? (data.warnings as unknown[]).map(String) : [],
  };
}

function modeBadgeClass(mode: string): string {
  if (mode === 'active') return 'text-ok bg-ok-subtle border-ok/30';
  if (mode === 'error') return 'text-danger bg-danger/10 border-danger/30';
  if (mode === 'not_enforced') return 'text-warn bg-warn/10 border-warn/30';
  return 'text-text-muted bg-secondary/60 border-border';
}

export function NetGuardEditor({ isConnected }: NetGuardEditorProps) {
  const { t } = useTranslation();
  const [view, setView] = useState<NetGuardView | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
  const [newPattern, setNewPattern] = useState('');
  const [newAction, setNewAction] = useState<NetAction>('deny');

  const userPatterns = useMemo(() => Object.keys(view?.section.urls ?? {}).sort((a, b) => a.localeCompare(b)), [view]);
  const builtinPatterns = useMemo(() => Object.keys(view?.builtinUrls ?? {}).sort((a, b) => a.localeCompare(b)), [view]);

  const load = useCallback(async () => {
    if (!isConnected) return;
    setLoading(true);
    setError(null);
    setAddError(null);
    try {
      const data = await webRequest<Record<string, unknown>>('permissions.net_guard.get', {});
      setView(parsePayload(data));
    } catch (e) {
      setError(e instanceof Error ? e.message : t('config.netGuard.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [isConnected, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async (key: string, patch: Record<string, unknown>, onError?: (msg: string) => void) => {
    if (!isConnected) return false;
    setBusyKey(key);
    setError(null);
    setAddError(null);
    try {
      const data = await webRequest<Record<string, unknown>>('permissions.net_guard.set', {
        net_guard: patch,
      });
      setView(parsePayload(data));
      return true;
    } catch (e) {
      const msg = e instanceof Error ? e.message : t('config.netGuard.saveFailed');
      (onError ?? setError)(msg);
      return false;
    } finally {
      setBusyKey(null);
    }
  };

  const handleActionChange = (pattern: string, action: NetAction) => {
    if (!view) return;
    void save(pattern, { urls: { ...view.section.urls, [pattern]: action } });
  };

  const handleDelete = (pattern: string) => {
    if (!view) return;
    if (!window.confirm(t('config.netGuard.deleteConfirm', { pattern }))) return;
    const urls = { ...view.section.urls };
    delete urls[pattern];
    void save(pattern, { urls });
  };

  const handleAdd = async () => {
    const pattern = newPattern.trim();
    if (!view || !pattern) return;
    if (pattern in view.section.urls) {
      setAddError(t('config.netGuard.duplicatePattern', { pattern }));
      return;
    }
    if (newAction !== 'deny' && view.builtinUrls[pattern] === 'deny') {
      setAddError(t('config.netGuard.builtinNotWidenable', { pattern }));
      return;
    }
    const ok = await save('__add__', { urls: { ...view.section.urls, [pattern]: newAction } }, setAddError);
    if (ok) {
      setNewPattern('');
      setNewAction('deny');
    }
  };

  const selectClass = 'rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text min-w-0 outline-none focus:border-accent';
  const disabledAll = !isConnected || !view || busyKey !== null || loading;

  const mode = view?.hostExit.mode ?? 'unset';
  const modeLabel = HOST_EXIT_MODES.has(mode) ? t(`config.netGuard.mode.${mode}`) : mode;

  return (
    <section className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm" aria-labelledby="network-guard-title">
      <div className="flex items-center justify-between gap-3 px-4 py-3 bg-secondary/30 border-b border-border">
        <div className="flex items-center gap-3 min-w-0">
          <span className="inline-flex items-center justify-center rounded-md border w-7 h-7 shrink-0 text-danger bg-danger-subtle border-danger/20">
            <ShieldCheck className="w-4 h-4" />
          </span>
          <div>
            <h3 id="network-guard-title" className="text-sm font-medium text-text-strong">
              {t('config.netGuard.title')}
            </h3>
            <p className="text-xs text-text-muted">{t('config.netGuard.subtitle')}</p>
          </div>
        </div>
        <button type="button" onClick={() => void load()} disabled={!isConnected || loading || busyKey !== null} className="btn text-xs disabled:opacity-50">
          {loading ? t('config.netGuard.refreshing') : t('config.netGuard.refresh')}
        </button>
      </div>

      {!isConnected ? <p className="px-4 py-2 text-xs text-warn">{t('config.netGuard.needConnection')}</p> : null}

      {error ? (
        <p className="px-4 py-2 text-xs text-danger break-words" role="alert">
          {error}
        </p>
      ) : null}

      {loading && !view ? (
        <p role="status" className="px-4 py-2 text-xs text-text-muted">
          {t('config.netGuard.loading')}
        </p>
      ) : null}

      {view ? (
        <>
          <fieldset disabled={disabledAll} className="min-w-0 disabled:opacity-60">
            <SecurityConfigSwitch label={t('config.netGuard.enabled')} checked={view.section.enabled} onChange={enabled => void save('enabled', { enabled })} />
            <div className="flex flex-wrap items-center border-b border-border px-4 py-2.5 text-xs text-text hover:bg-secondary/25">
              <label htmlFor="network-guard-defaults" className="w-[32%] shrink-0 text-text-muted">
                {t('config.netGuard.defaults')}
              </label>
              <div className="pl-4">
                <select
                  id="network-guard-defaults"
                  className={selectClass}
                  value={view.section.defaults}
                  onChange={e => void save('defaults', { defaults: e.target.value as NetAction })}
                >
                  <option value="allow">{t('config.netGuard.defaultsAllow')}</option>
                  <option value="ask">{t('config.netGuard.actionAsk')}</option>
                  <option value="deny">{t('config.netGuard.defaultsDeny')}</option>
                </select>
              </div>
            </div>
            <SecurityConfigSwitch
              label={t('config.netGuard.enforceHostExit')}
              checked={view.section.enforceHostExit}
              onChange={enforce_host_exit => void save('enforce_host_exit', { enforce_host_exit })}
            />
          </fieldset>
          <div className="px-4 py-3 space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-text-muted">{t('config.netGuard.hostExitStatus')}</span>
              <span className={`px-2 py-0.5 rounded-full border ${modeBadgeClass(mode)}`}>{modeLabel}</span>
              {view.hostExit.error ? <span className="text-danger break-all">{view.hostExit.error}</span> : null}
            </div>

            {view.warnings.length > 0 ? (
              <ul className="text-xs text-warn space-y-0.5" role="alert">
                {view.warnings.map(w => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            ) : null}
          </div>
          <div className="border-t border-border px-4 py-3 space-y-3">
            <p className="text-xs text-text-muted">{t('config.netGuard.matchHint')}</p>

            {userPatterns.length === 0 ? (
              <p className="text-xs text-text-muted">{t('config.netGuard.empty')}</p>
            ) : (
              <div className="space-y-3">
                {userPatterns.map(pattern => (
                  <div key={pattern} className="rounded-lg border border-border bg-secondary/20 p-3 space-y-2">
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
                      <label className="flex items-center gap-2 text-text-muted">
                        {t('config.netGuard.colAction')}
                        <select
                          className={selectClass}
                          value={view.section.urls[pattern]}
                          disabled={disabledAll}
                          onChange={e => handleActionChange(pattern, e.target.value as NetAction)}
                        >
                          <option value="deny">{t('config.netGuard.actionDeny')}</option>
                          <option value="allow">{t('config.netGuard.actionAllow')}</option>
                          <option value="ask">{t('config.netGuard.actionAsk')}</option>
                        </select>
                      </label>
                      <button
                        type="button"
                        onClick={() => handleDelete(pattern)}
                        disabled={disabledAll}
                        className="btn text-xs text-danger disabled:opacity-50"
                      >
                        {t('config.netGuard.delete')}
                      </button>
                    </div>
                    <code className="block break-all whitespace-pre-wrap text-xs text-text">{pattern}</code>
                  </div>
                ))}
              </div>
            )}

            <div className="rounded-lg border border-border bg-secondary/20 p-3 space-y-3">
              <p className="text-sm font-medium text-text-strong">{t('config.netGuard.addTitle')}</p>
              <div className="flex flex-wrap items-end gap-2">
                <div className="flex-1 min-w-[10rem]">
                  <label className="block text-xs text-text-muted mb-1.5">{t('config.netGuard.colPattern')}</label>
                  <input
                    type="text"
                    aria-label={t('config.netGuard.colPattern')}
                    value={newPattern}
                    onChange={e => {
                      setNewPattern(e.target.value);
                      if (addError) setAddError(null);
                    }}
                    placeholder={t('config.netGuard.patternPlaceholder')}
                    disabled={disabledAll}
                    className={`w-full min-w-0 rounded-md border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent mono ${
                      addError ? 'border-danger' : 'border-border'
                    }`}
                  />
                  {addError ? (
                    <p className="mt-1 text-xs text-danger break-words" role="alert">
                      {addError}
                    </p>
                  ) : null}
                </div>
                <div>
                  <label className="block text-xs text-text-muted mb-1.5">{t('config.netGuard.colAction')}</label>
                  <select
                    className={selectClass}
                    aria-label={t('config.netGuard.colAction')}
                    value={newAction}
                    onChange={e => setNewAction(e.target.value as NetAction)}
                    disabled={disabledAll}
                  >
                    <option value="deny">{t('config.netGuard.actionDeny')}</option>
                    <option value="allow">{t('config.netGuard.actionAllow')}</option>
                    <option value="ask">{t('config.netGuard.actionAsk')}</option>
                  </select>
                </div>
                <button type="button" onClick={() => void handleAdd()} disabled={disabledAll || !newPattern.trim()} className="btn text-xs disabled:opacity-50">
                  {busyKey === '__add__' ? t('common.saving') : t('config.netGuard.add')}
                </button>
              </div>
            </div>
          </div>
          <div className="border-t border-border px-4 py-3 space-y-3">
            <details className="text-xs">
              <summary className="cursor-pointer py-2 text-text-muted">{t('config.netGuard.builtinTitle', { count: builtinPatterns.length })}</summary>
              <p className="mt-1 text-xs text-text-muted">{t('config.netGuard.builtinHint')}</p>
              <ul className="mt-1 space-y-0.5">
                {builtinPatterns.map(pattern => (
                  <li key={pattern} className="mono text-text break-all">
                    {pattern} → {view.builtinUrls[pattern]}
                  </li>
                ))}
              </ul>
            </details>

            <details className="text-xs">
              <summary className="cursor-pointer py-2 text-text-muted">
                {t('config.netGuard.exemptionsTitle', { count: view.hostExit.exemptions.length })}
              </summary>
              <p className="mt-1 text-xs text-text-muted">{t('config.netGuard.exemptionsHint')}</p>
              <ul className="mt-1 space-y-0.5">
                {view.hostExit.exemptions.map(e => (
                  <li key={e.id} className="text-text">
                    <span className="mono">{e.id}</span>：{e.description}
                  </li>
                ))}
              </ul>
            </details>
          </div>
        </>
      ) : null}
    </section>
  );
}
