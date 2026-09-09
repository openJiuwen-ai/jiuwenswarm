import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Check, Copy, Power, RefreshCw, RotateCw, X } from 'lucide-react';
import { isEnterprise } from '../../edition';
import type { WebError } from '../../types/websocket';
import { A2AOutboundPanel } from './A2AOutboundPanel';
import { A2AIngressSecurityFields } from './A2AIngressSecurityFields';
import {
  canOperateA2AIngress,
  draftFromA2AIngressSnapshot,
  isA2AIngressTransitioning,
  normalizeA2AIngressHistory,
  normalizeA2AIngressSnapshot,
  normalizeA2AOutboundDispatchHistory,
  shouldAcceptA2AIngressResponse,
  toA2AIngressPatch,
  validateA2AIngressDraft,
  type A2AIngressDraft,
  type A2AIngressRequestRecord,
  type A2AIngressSnapshot,
  type A2AOutboundDispatchRecord,
} from './a2aIngressPanelState';

interface A2AIngressPanelProps {
  isConnected: boolean;
  request: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;
}

const STATE_BADGE_CLASS: Record<A2AIngressSnapshot['state'], string> = {
  disabled: 'bg-secondary text-text-muted',
  starting: 'bg-accent/15 text-accent',
  running: 'bg-ok/15 text-ok',
  stopping: 'bg-warn/15 text-warn',
  error: 'bg-danger/15 text-danger',
};

const REQUEST_BADGE_CLASS: Record<A2AIngressRequestRecord['status'], string> = {
  processing: 'bg-accent/15 text-accent',
  completed: 'bg-ok/15 text-ok',
  failed: 'bg-danger/15 text-danger',
  canceled: 'bg-secondary text-text-muted',
};

function errorMessage(error: unknown): string {
  return (error as WebError)?.message || String(error);
}

export function A2AIngressPanel({ isConnected, request }: A2AIngressPanelProps) {
  const { t, i18n } = useTranslation();
  const enterpriseMode = isEnterprise();
  const [activeTab, setActiveTab] = useState<'config' | 'outbound' | 'history'>(enterpriseMode ? 'outbound' : 'config');
  const [snapshot, setSnapshot] = useState<A2AIngressSnapshot | null>(null);
  const [draft, setDraft] = useState<A2AIngressDraft | null>(null);
  const [loading, setLoading] = useState(false);
  const [operation, setOperation] = useState<'save' | 'enable' | 'disable' | 'reload' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [isDirty, setIsDirty] = useState(false);
  const [history, setHistory] = useState<A2AIngressRequestRecord[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyDirection, setHistoryDirection] = useState<'ingress' | 'outbound'>(enterpriseMode ? 'outbound' : 'ingress');
  const [outboundHistory, setOutboundHistory] = useState<A2AOutboundDispatchRecord[]>([]);
  const [outboundHistoryLoading, setOutboundHistoryLoading] = useState(false);
  const [outboundHistoryError, setOutboundHistoryError] = useState<string | null>(null);
  const [copiedHistoryCell, setCopiedHistoryCell] = useState<string | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [outboundHeaderActionsContainer, setOutboundHeaderActionsContainer] = useState<HTMLDivElement | null>(null);
  const responseGenerationRef = useRef(0);
  const savedCredentialRef = useRef('');
  const historyResponseGenerationRef = useRef(0);
  const outboundHistoryResponseGenerationRef = useRef(0);
  const copyFeedbackTimerRef = useRef<number | null>(null);

  const acceptSnapshot = useCallback(
    (payload: unknown, credential?: string) => {
      const next = normalizeA2AIngressSnapshot(payload);
      if (!next) throw new Error(t('a2aIngress.errors.invalidResponse'));
      if (credential !== undefined) savedCredentialRef.current = credential;
      else if (!next.credential_configured) savedCredentialRef.current = '';
      setSnapshot(next);
      setDraft(draftFromA2AIngressSnapshot(next, savedCredentialRef.current));
      setIsDirty(false);
      return next;
    },
    [t],
  );

  const refresh = useCallback(
    async (showLoading = true) => {
      if (enterpriseMode || !isConnected) return;
      const responseGeneration = ++responseGenerationRef.current;
      if (showLoading) setLoading(true);
      try {
        const payload = await request<Record<string, unknown>>(showLoading ? 'a2a.ingress.edit' : 'a2a.ingress.get');
        if (!shouldAcceptA2AIngressResponse(responseGeneration, responseGenerationRef.current)) return;
        if (showLoading && typeof payload.credential !== 'string') throw new Error(t('a2aIngress.errors.invalidResponse'));
        acceptSnapshot(payload, showLoading ? (payload.credential as string) : undefined);
        setError(null);
      } catch (refreshError) {
        if (!shouldAcceptA2AIngressResponse(responseGeneration, responseGenerationRef.current)) return;
        setError(errorMessage(refreshError));
      } finally {
        if (showLoading && shouldAcceptA2AIngressResponse(responseGeneration, responseGenerationRef.current)) setLoading(false);
      }
    },
    [acceptSnapshot, enterpriseMode, isConnected, request],
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);
  useEffect(() => {
    if (enterpriseMode || loading || operation || isDirty || !isA2AIngressTransitioning(snapshot?.state)) return;
    const timer = window.setInterval(() => void refresh(false), 1200);
    return () => window.clearInterval(timer);
  }, [enterpriseMode, refresh, snapshot?.state, loading, operation, isDirty]);

  const refreshHistory = useCallback(
    async (showLoading = true) => {
      if (enterpriseMode || !isConnected) return;
      const responseGeneration = ++historyResponseGenerationRef.current;
      if (showLoading) setHistoryLoading(true);
      try {
        const payload = await request('a2a.ingress.history', { limit: 200 });
        if (!shouldAcceptA2AIngressResponse(responseGeneration, historyResponseGenerationRef.current)) return;
        const next = normalizeA2AIngressHistory(payload);
        if (!next) throw new Error(t('a2aIngress.errors.invalidHistoryResponse'));
        setHistory(next.items);
        setHistoryError(null);
      } catch (historyRequestError) {
        if (!shouldAcceptA2AIngressResponse(responseGeneration, historyResponseGenerationRef.current)) return;
        setHistoryError(errorMessage(historyRequestError));
      } finally {
        if (showLoading && shouldAcceptA2AIngressResponse(responseGeneration, historyResponseGenerationRef.current)) setHistoryLoading(false);
      }
    },
    [enterpriseMode, isConnected, request, t],
  );

  const refreshOutboundHistory = useCallback(
    async (showLoading = true) => {
      if (!isConnected) return;
      const responseGeneration = ++outboundHistoryResponseGenerationRef.current;
      if (showLoading) setOutboundHistoryLoading(true);
      try {
        const payload = await request('a2a.outbound.dispatch.list', { limit: 200 });
        if (!shouldAcceptA2AIngressResponse(responseGeneration, outboundHistoryResponseGenerationRef.current)) return;
        const next = normalizeA2AOutboundDispatchHistory(payload);
        if (!next) throw new Error(t('a2aIngress.errors.invalidHistoryResponse'));
        setOutboundHistory(next.items);
        setOutboundHistoryError(null);
      } catch (historyRequestError) {
        if (!shouldAcceptA2AIngressResponse(responseGeneration, outboundHistoryResponseGenerationRef.current)) return;
        setOutboundHistoryError(errorMessage(historyRequestError));
      } finally {
        if (showLoading && shouldAcceptA2AIngressResponse(responseGeneration, outboundHistoryResponseGenerationRef.current)) setOutboundHistoryLoading(false);
      }
    },
    [isConnected, request, t],
  );

  useEffect(() => {
    if (activeTab !== 'history' || !isConnected) return;
    const refreshActiveHistory = enterpriseMode || historyDirection === 'outbound' ? refreshOutboundHistory : refreshHistory;
    void refreshActiveHistory();
    const timer = window.setInterval(() => void refreshActiveHistory(false), 2000);
    return () => window.clearInterval(timer);
  }, [activeTab, enterpriseMode, historyDirection, isConnected, refreshHistory, refreshOutboundHistory]);

  useEffect(
    () => () => {
      if (copyFeedbackTimerRef.current !== null) window.clearTimeout(copyFeedbackTimerRef.current);
    },
    [],
  );

  const copyHistoryValue = useCallback(
    async (cellKey: string, value: string) => {
      try {
        await navigator.clipboard.writeText(value);
        setCopyError(null);
        setCopiedHistoryCell(cellKey);
        if (copyFeedbackTimerRef.current !== null) window.clearTimeout(copyFeedbackTimerRef.current);
        copyFeedbackTimerRef.current = window.setTimeout(() => setCopiedHistoryCell(null), 1600);
      } catch {
        setCopyError(t('a2aIngress.history.copyFailed'));
      }
    },
    [t],
  );

  const updateDraft = <K extends keyof A2AIngressDraft>(field: K, value: A2AIngressDraft[K]) => {
    setDraft(current => (current ? { ...current, [field]: value } : current));
    setIsDirty(true);
    setNotice(null);
  };

  const cancelEdit = () => {
    if (!snapshot) return;
    setDraft(draftFromA2AIngressSnapshot(snapshot, savedCredentialRef.current));
    setIsDirty(false);
    setError(null);
    setNotice(null);
  };

  const save = async () => {
    if (!draft || operation || !isConnected) return;
    const invalidField = validateA2AIngressDraft(draft);
    if (invalidField) {
      setError(t('a2aIngress.errors.invalidField', { field: t(`a2aIngress.fields.${invalidField}`) }));
      return;
    }
    setOperation('save');
    responseGenerationRef.current += 1;
    setLoading(false);
    setError(null);
    try {
      const savedCredential = draft.clear_credential ? '' : draft.credential || savedCredentialRef.current;
      acceptSnapshot(await request('a2a.ingress.update', { config: toA2AIngressPatch(draft), apply: true }), savedCredential);
      setNotice(t('a2aIngress.saved'));
    } catch (saveError) {
      const failedSnapshot = normalizeA2AIngressSnapshot((saveError as WebError).payload);
      if (failedSnapshot) acceptSnapshot(failedSnapshot);
      setError(errorMessage(saveError));
      if (!failedSnapshot) void refresh();
    } finally {
      setOperation(null);
    }
  };

  const runOperation = async (nextOperation: 'enable' | 'disable' | 'reload') => {
    if (operation || !isConnected) return;
    setOperation(nextOperation);
    responseGenerationRef.current += 1;
    setLoading(false);
    setError(null);
    setNotice(null);
    try {
      acceptSnapshot(await request(`a2a.ingress.${nextOperation}`));
    } catch (operationError) {
      const failedSnapshot = normalizeA2AIngressSnapshot((operationError as WebError).payload);
      if (failedSnapshot) acceptSnapshot(failedSnapshot);
      setError(errorMessage(operationError));
      if (!failedSnapshot) void refresh();
    } finally {
      setOperation(null);
    }
  };

  const busy = operation !== null || isA2AIngressTransitioning(snapshot?.state);
  const canEnable = canOperateA2AIngress(snapshot, isConnected, busy, isDirty, 'enable');
  const canDisable = canOperateA2AIngress(snapshot, isConnected, busy, isDirty, 'disable');
  const canReload = canOperateA2AIngress(snapshot, isConnected, busy, isDirty, 'reload');
  const serviceEnabled = snapshot?.enabled === true;
  const lifecycleOperation = serviceEnabled ? 'disable' : 'enable';
  const canToggleService = serviceEnabled ? canDisable : canEnable;
  const activeHistoryLoading = enterpriseMode || historyDirection === 'outbound' ? outboundHistoryLoading : historyLoading;
  const refreshActiveHistory = enterpriseMode || historyDirection === 'outbound' ? refreshOutboundHistory : refreshHistory;

  return (
    <div className="flex-1 min-h-0">
      <div className="card main-panel-card w-full h-full flex flex-col overflow-hidden">
        <div className="mb-1 flex shrink-0 flex-wrap items-start justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold">{t('a2aIngress.title')}</h2>
            <p className="text-sm text-text-muted mt-1">{t(enterpriseMode ? 'a2aIngress.enterpriseSubtitle' : 'a2aIngress.subtitle')}</p>
          </div>
          {activeTab === 'config' ? (
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                className={`btn ${serviceEnabled ? 'secondary text-danger' : 'primary'}`}
                onClick={() => void runOperation(lifecycleOperation)}
                disabled={!canToggleService}
              >
                <Power className="h-4 w-4" aria-hidden />
                {operation === 'enable'
                  ? t('a2aIngress.enabling')
                  : operation === 'disable'
                    ? t('a2aIngress.disabling')
                    : t(serviceEnabled ? 'a2aIngress.disable' : 'a2aIngress.enable')}
              </button>
              <button
                type="button"
                className="btn secondary"
                onClick={() => void runOperation('reload')}
                disabled={!canReload}
                title={t('a2aIngress.restartHint')}
              >
                <RotateCw className={`h-4 w-4 ${operation === 'reload' ? 'animate-spin' : ''}`} aria-hidden />
                {operation === 'reload' ? t('a2aIngress.restarting') : t('a2aIngress.restart')}
              </button>
              <div className="ml-1 border-l border-border pl-2">
                <button
                  type="button"
                  className="btn secondary px-3"
                  onClick={() => void refresh()}
                  disabled={!isConnected || loading || busy}
                  aria-label={t('a2aIngress.refreshStatus')}
                  title={t('a2aIngress.refreshHint')}
                >
                  <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} aria-hidden />
                </button>
              </div>
            </div>
          ) : activeTab === 'outbound' ? (
            <div ref={setOutboundHeaderActionsContainer} className="flex flex-wrap items-center gap-2" />
          ) : activeTab === 'history' ? (
            <button type="button" className="btn secondary" onClick={() => void refreshActiveHistory()} disabled={!isConnected || activeHistoryLoading}>
              {activeHistoryLoading ? t('common.refreshing') : t('common.refresh')}
            </button>
          ) : null}
        </div>
        <div className="app-subtabs shrink-0" role="tablist" aria-label={t('a2aIngress.tabs.ariaLabel')}>
          {(enterpriseMode ? (['outbound', 'history'] as const) : (['config', 'outbound', 'history'] as const)).map(tab => (
            <button
              key={tab}
              type="button"
              role="tab"
              id={`a2a-ingress-tab-${tab}`}
              aria-controls={`a2a-ingress-panel-${tab}`}
              aria-selected={activeTab === tab}
              tabIndex={activeTab === tab ? 0 : -1}
              className={`app-subtabs__tab${activeTab === tab ? ' app-subtabs__tab--active' : ''}`}
              onClick={() => setActiveTab(tab)}
            >
              {t(`a2aIngress.tabs.${tab}`)}
            </button>
          ))}
        </div>
        {activeTab === 'config' ? (
          <div
            id="a2a-ingress-panel-config"
            role="tabpanel"
            aria-labelledby="a2a-ingress-tab-config"
            className="flex min-h-0 flex-1 flex-col gap-5 overflow-auto pr-1 pt-1"
          >
            {!isConnected && <Alert tone="warn">{t('a2aIngress.disconnected')}</Alert>}
            {error && <Alert tone="danger">{error}</Alert>}
            {notice && <Alert tone="ok">{notice}</Alert>}
            {isDirty && <Alert tone="warn">{t('a2aIngress.saveBeforeLifecycleAction')}</Alert>}
            <section className="rounded-xl border border-border bg-panel-strong/60 p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 className="text-sm font-semibold text-text">{t('a2aIngress.status.title')}</h3>
                  <p className="mt-1 text-xs text-text-muted">{t('a2aIngress.status.configRevision', { revision: snapshot?.config_revision ?? '-' })}</p>
                </div>
                {snapshot && (
                  <span className={`rounded-full px-3 py-1 text-sm font-medium ${STATE_BADGE_CLASS[snapshot.state]}`}>
                    {t(`a2aIngress.states.${snapshot.state}`)}
                  </span>
                )}
              </div>
              <div className="mt-4 grid gap-4 md:grid-cols-2">
                <AddressCard
                  label={t('a2aIngress.status.effectiveRpcUrl')}
                  value={snapshot?.effective_rpc_url}
                  emptyText={t('a2aIngress.status.notListening')}
                />
                <AddressCard
                  label={t('a2aIngress.status.effectiveCardUrl')}
                  value={snapshot?.effective_card_url}
                  emptyText={t('a2aIngress.status.notListening')}
                />
              </div>
              {snapshot?.last_error && (
                <Alert tone="danger" className="mt-4">
                  {snapshot.last_error}
                </Alert>
              )}
              {snapshot && (
                <p className="mt-4 text-xs text-text-muted">
                  {t('a2aIngress.security.effective', {
                    method: snapshot.effective_auth_type ? t(`a2aIngress.security.types.${snapshot.effective_auth_type}`) : t('a2aIngress.status.notListening'),
                  })}
                </p>
              )}
              {snapshot?.security_pending_apply && (
                <Alert tone="warn" className="mt-3">
                  {t('a2aIngress.security.pendingApply')}
                </Alert>
              )}
              {snapshot?.exposure_warning && (
                <Alert tone="warn" className="mt-4">
                  {snapshot.exposure_warning}
                </Alert>
              )}
            </section>
            {draft && (
              <section className="rounded-xl border border-border bg-panel-strong/60 p-4">
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div>
                    <h3 className="text-sm font-semibold text-text">{t('a2aIngress.config.title')}</h3>
                    <p className="mt-1 text-xs text-text-muted">{t('a2aIngress.config.description')}</p>
                  </div>
                  <div className="flex gap-2">
                    <button type="button" className="btn secondary" onClick={cancelEdit} disabled={!isDirty || busy}>
                      {t('common.cancel')}
                    </button>
                    <button type="button" className="btn primary" onClick={() => void save()} disabled={!isConnected || busy}>
                      {operation === 'save' ? t('common.saving') : t('a2aIngress.save')}
                    </button>
                  </div>
                </div>
                <div className="mt-4 grid gap-4 md:grid-cols-2">
                  <TextField label={t('a2aIngress.fields.host')} value={draft.host} onChange={value => updateDraft('host', value)} disabled={busy} />
                  <TextField
                    label={t('a2aIngress.fields.port')}
                    value={draft.port}
                    onChange={value => updateDraft('port', value)}
                    disabled={busy}
                    type="number"
                    min={1}
                    max={65535}
                  />
                  <TextField
                    label={t('a2aIngress.fields.rpc_path')}
                    value={draft.rpc_path}
                    onChange={value => updateDraft('rpc_path', value)}
                    disabled={busy}
                  />
                  <TextField
                    label={t('a2aIngress.fields.card_path')}
                    value={draft.card_path}
                    onChange={value => updateDraft('card_path', value)}
                    disabled={busy}
                  />
                  <TextField
                    label={t('a2aIngress.fields.extended_card_path')}
                    value={draft.extended_card_path}
                    onChange={value => updateDraft('extended_card_path', value)}
                    disabled={busy}
                  />
                  <TextField
                    label={t('a2aIngress.fields.protocol_version')}
                    value={draft.protocol_version}
                    onChange={value => updateDraft('protocol_version', value)}
                    disabled={busy}
                  />
                  <TextField
                    label={t('a2aIngress.fields.app_name')}
                    value={draft.app_name}
                    onChange={value => updateDraft('app_name', value)}
                    disabled={busy}
                  />
                  <TextField
                    label={t('a2aIngress.fields.app_version')}
                    value={draft.app_version}
                    onChange={value => updateDraft('app_version', value)}
                    disabled={busy}
                  />
                  <label className="md:col-span-2">
                    <span className="text-xs font-medium text-text-muted">{t('a2aIngress.fields.app_description')}</span>
                    <textarea
                      className="mt-2 min-h-20 w-full resize-y rounded-md border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-accent"
                      value={draft.app_description}
                      onChange={event => updateDraft('app_description', event.target.value)}
                      disabled={busy}
                    />
                  </label>
                </div>
                <A2AIngressSecurityFields draft={draft} disabled={busy} onChange={updateDraft} />
              </section>
            )}
          </div>
        ) : activeTab === 'outbound' ? (
          <div id="a2a-ingress-panel-outbound" role="tabpanel" aria-labelledby="a2a-ingress-tab-outbound" className="flex min-h-0 flex-1 flex-col pt-1">
            <A2AOutboundPanel isConnected={isConnected} request={request} headerActionsContainer={outboundHeaderActionsContainer} />
          </div>
        ) : (
          <div id="a2a-ingress-panel-history" role="tabpanel" aria-labelledby="a2a-ingress-tab-history" className="flex min-h-0 flex-1 flex-col gap-3 pt-1">
            {!isConnected && <Alert tone="warn">{t('a2aIngress.disconnected')}</Alert>}
            {copyError && (
              <Alert tone="danger" onClose={() => setCopyError(null)} closeLabel={t('common.close')}>
                {copyError}
              </Alert>
            )}
            {(historyDirection === 'ingress' ? historyError : outboundHistoryError) && (
              <Alert tone="danger">{historyDirection === 'ingress' ? historyError : outboundHistoryError}</Alert>
            )}
            <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-panel-strong/60">
              <div className="shrink-0 border-b border-border px-4 py-3">
                <div className="flex flex-wrap items-center justify-between gap-4">
                  <div className="min-w-0">
                    <h3 className="text-sm font-semibold text-text">{t(`a2aIngress.history.${historyDirection}.title`)}</h3>
                    <p className="mt-1 text-xs text-text-muted">{t(`a2aIngress.history.${historyDirection}.description`)}</p>
                  </div>
                  {!enterpriseMode && (
                    <div
                      className="ml-auto inline-flex shrink-0 rounded-lg border border-border bg-secondary/50 p-1"
                      role="tablist"
                      aria-label={t('a2aIngress.history.tabs.ariaLabel')}
                    >
                      {(['ingress', 'outbound'] as const).map(direction => (
                        <button
                          key={direction}
                          type="button"
                          role="tab"
                          aria-selected={historyDirection === direction}
                          className={`rounded-md px-4 py-1.5 text-sm font-medium transition-colors ${historyDirection === direction ? 'bg-bg text-accent shadow-sm' : 'text-text-muted hover:bg-bg/60 hover:text-text'}`}
                          onClick={() => setHistoryDirection(direction)}
                        >
                          {t(`a2aIngress.history.tabs.${direction}`)}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
              {historyDirection === 'ingress' ? (
                historyLoading && history.length === 0 ? (
                  <div className="grid min-h-0 flex-1 place-items-center px-4 py-12 text-center text-sm text-text-muted">{t('common.loading')}</div>
                ) : history.length === 0 ? (
                  <div className="grid min-h-0 flex-1 place-items-center px-4 py-12 text-center">
                    <div>
                      <div className="text-sm font-medium text-text">{t('a2aIngress.history.ingress.empty')}</div>
                      <div className="mt-1 text-xs text-text-muted">{t('a2aIngress.history.ingress.emptyDescription')}</div>
                    </div>
                  </div>
                ) : (
                  <div className="min-h-0 flex-1 overflow-auto">
                    <table className="w-full min-w-[920px] text-left text-sm">
                      <thead className="sticky top-0 z-10 border-b border-border bg-secondary/60 text-xs text-text-muted">
                        <tr>
                          <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.status')}</th>
                          <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.operation')}</th>
                          <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.requestId')}</th>
                          <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.contextId')}</th>
                          <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.startedAt')}</th>
                          <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.duration')}</th>
                          <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.error')}</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border">
                        {history.map(item => (
                          <tr key={item.request_id} className="text-text">
                            <td className="px-4 py-3">
                              <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${REQUEST_BADGE_CLASS[item.status]}`}>
                                {t(`a2aIngress.history.statuses.${item.status}`)}
                              </span>
                            </td>
                            <td className="px-4 py-3">{t(`a2aIngress.history.operations.${item.operation}`, { defaultValue: item.operation })}</td>
                            <td className="max-w-48 px-4 py-3 font-mono text-xs">
                              <CopyableHistoryValue
                                value={item.request_id}
                                copied={copiedHistoryCell === `ingress:${item.request_id}:request`}
                                onCopy={() => void copyHistoryValue(`ingress:${item.request_id}:request`, item.request_id)}
                                copyLabel={t('a2aIngress.history.copy')}
                                copiedLabel={t('a2aIngress.history.copied')}
                              />
                            </td>
                            <td className="max-w-48 px-4 py-3 font-mono text-xs">
                              <CopyableHistoryValue
                                value={item.context_id}
                                copied={copiedHistoryCell === `ingress:${item.request_id}:context`}
                                onCopy={() => item.context_id && void copyHistoryValue(`ingress:${item.request_id}:context`, item.context_id)}
                                copyLabel={t('a2aIngress.history.copy')}
                                copiedLabel={t('a2aIngress.history.copied')}
                              />
                            </td>
                            <td className="whitespace-nowrap px-4 py-3">{formatTimestamp(item.started_at, i18n.language)}</td>
                            <td className="whitespace-nowrap px-4 py-3">{formatDuration(item.duration_ms, t('a2aIngress.history.processing'))}</td>
                            <td className="max-w-64 px-4 py-3 text-xs text-danger">
                              <CopyableHistoryValue
                                value={item.error}
                                copied={copiedHistoryCell === `ingress:${item.request_id}:error`}
                                onCopy={() => item.error && void copyHistoryValue(`ingress:${item.request_id}:error`, item.error)}
                                copyLabel={t('a2aIngress.history.copy')}
                                copiedLabel={t('a2aIngress.history.copied')}
                                multiline
                              />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )
              ) : outboundHistoryLoading && outboundHistory.length === 0 ? (
                <div className="grid min-h-0 flex-1 place-items-center px-4 py-12 text-center text-sm text-text-muted">{t('common.loading')}</div>
              ) : outboundHistory.length === 0 ? (
                <div className="grid min-h-0 flex-1 place-items-center px-4 py-12 text-center">
                  <div>
                    <div className="text-sm font-medium text-text">{t('a2aIngress.history.outbound.empty')}</div>
                    <div className="mt-1 text-xs text-text-muted">{t('a2aIngress.history.outbound.emptyDescription')}</div>
                  </div>
                </div>
              ) : (
                <div className="min-h-0 flex-1 overflow-auto">
                  <table className="w-full min-w-[1080px] text-left text-sm">
                    <thead className="sticky top-0 z-10 border-b border-border bg-secondary/60 text-xs text-text-muted">
                      <tr>
                        <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.status')}</th>
                        <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.mode')}</th>
                        <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.agent')}</th>
                        <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.dispatchId')}</th>
                        <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.remoteTaskId')}</th>
                        <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.startedAt')}</th>
                        <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.duration')}</th>
                        <th className="px-4 py-3 font-medium">{t('a2aIngress.history.columns.error')}</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {outboundHistory.map(item => (
                        <tr key={item.dispatch_id} className="text-text">
                          <td className="px-4 py-3">
                            <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${outboundStatusBadgeClass(item.status)}`}>
                              {t(`a2aIngress.history.outbound.statuses.${item.status}`)}
                            </span>
                          </td>
                          <td className="px-4 py-3">{t(`a2aIngress.history.outbound.modes.${item.mode}`)}</td>
                          <td className="max-w-48 px-4 py-3 font-mono text-xs">
                            <CopyableHistoryValue
                              value={item.agent_name}
                              copied={copiedHistoryCell === `outbound:${item.dispatch_id}:agent`}
                              onCopy={() => void copyHistoryValue(`outbound:${item.dispatch_id}:agent`, item.agent_id)}
                              copyLabel={t('a2aIngress.history.copy')}
                              copiedLabel={t('a2aIngress.history.copied')}
                            />
                          </td>
                          <td className="max-w-48 px-4 py-3 font-mono text-xs">
                            <CopyableHistoryValue
                              value={item.dispatch_id}
                              copied={copiedHistoryCell === `outbound:${item.dispatch_id}:dispatch`}
                              onCopy={() => void copyHistoryValue(`outbound:${item.dispatch_id}:dispatch`, item.dispatch_id)}
                              copyLabel={t('a2aIngress.history.copy')}
                              copiedLabel={t('a2aIngress.history.copied')}
                            />
                          </td>
                          <td className="max-w-48 px-4 py-3 font-mono text-xs">
                            <CopyableHistoryValue
                              value={item.remote_task_id}
                              copied={copiedHistoryCell === `outbound:${item.dispatch_id}:remote`}
                              onCopy={() => item.remote_task_id && void copyHistoryValue(`outbound:${item.dispatch_id}:remote`, item.remote_task_id)}
                              copyLabel={t('a2aIngress.history.copy')}
                              copiedLabel={t('a2aIngress.history.copied')}
                            />
                          </td>
                          <td className="whitespace-nowrap px-4 py-3">{formatTimestamp(item.created_at, i18n.language)}</td>
                          <td className="whitespace-nowrap px-4 py-3">{formatOutboundDuration(item, t('a2aIngress.history.processing'))}</td>
                          <td className="max-w-64 px-4 py-3 text-xs text-danger">
                            <CopyableHistoryValue
                              value={item.error_summary || item.error_code}
                              copied={copiedHistoryCell === `outbound:${item.dispatch_id}:error`}
                              onCopy={() => {
                                const value = item.error_summary || item.error_code;
                                if (value) void copyHistoryValue(`outbound:${item.dispatch_id}:error`, value);
                              }}
                              copyLabel={t('a2aIngress.history.copy')}
                              copiedLabel={t('a2aIngress.history.copied')}
                              multiline
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}

function formatTimestamp(timestamp: number | string, language: string): string {
  const date = typeof timestamp === 'number' ? new Date(timestamp * 1000) : new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleString(language.startsWith('zh') ? 'zh-CN' : 'en-US', { hour12: false });
}

function formatDuration(durationMs: number | null, processingText: string): string {
  if (durationMs === null) return processingText;
  if (durationMs < 1000) return `${durationMs} ms`;
  return `${(durationMs / 1000).toFixed(2)} s`;
}

function formatOutboundDuration(item: A2AOutboundDispatchRecord, processingText: string): string {
  if (['created', 'submitting', 'accepted', 'working'].includes(item.status)) return processingText;
  const observedEnd = item.finished_at || item.updated_at;
  if (!observedEnd) return '-';
  const durationMs = Date.parse(observedEnd) - Date.parse(item.created_at);
  return Number.isFinite(durationMs) && durationMs >= 0 ? formatDuration(durationMs, processingText) : '-';
}

function outboundStatusBadgeClass(status: A2AOutboundDispatchRecord['status']): string {
  if (status === 'completed') return 'bg-ok/15 text-ok';
  if (['failed', 'rejected', 'auth_required', 'dispatch_failed'].includes(status)) return 'bg-danger/15 text-danger';
  if (status === 'canceled') return 'bg-secondary text-text-muted';
  if (['input_required', 'unknown', 'timed_out'].includes(status)) return 'bg-warn/15 text-warn';
  return 'bg-accent/15 text-accent';
}

function CopyableHistoryValue({
  value,
  copied,
  onCopy,
  copyLabel,
  copiedLabel,
  multiline = false,
}: {
  value: string | null;
  copied: boolean;
  onCopy: () => void;
  copyLabel: string;
  copiedLabel: string;
  multiline?: boolean;
}) {
  if (!value) return <span className="text-text-muted">-</span>;
  return (
    <button
      type="button"
      className="group flex w-full max-w-full items-start gap-1.5 text-left outline-none transition-colors hover:text-accent focus-visible:text-accent"
      onClick={onCopy}
      title={copied ? copiedLabel : value}
      aria-label={`${copied ? copiedLabel : copyLabel}: ${value}`}
    >
      <span className={multiline ? 'line-clamp-2 min-w-0' : 'min-w-0 truncate'}>{value}</span>
      {copied ? (
        <Check size={13} className="mt-0.5 shrink-0 text-ok" aria-hidden="true" />
      ) : (
        <Copy size={13} className="mt-0.5 shrink-0 text-text-muted group-hover:text-accent" aria-hidden="true" />
      )}
    </button>
  );
}

function Alert({
  tone,
  className = '',
  children,
  onClose,
  closeLabel,
}: {
  tone: 'warn' | 'danger' | 'ok';
  className?: string;
  children: ReactNode;
  onClose?: () => void;
  closeLabel?: string;
}) {
  const toneClass = tone === 'danger' ? 'border-danger/30 bg-danger/10' : tone === 'warn' ? 'border-warn/30 bg-warn/10' : 'border-ok/30 bg-ok/10';
  return (
    <div className={`flex items-center gap-3 rounded-xl border px-4 py-3 text-sm text-text ${toneClass} ${className}`}>
      <div className="min-w-0 flex-1">{children}</div>
      {onClose && (
        <button type="button" className="shrink-0 rounded-md p-1 text-text-muted hover:bg-bg/60 hover:text-text" onClick={onClose} aria-label={closeLabel}>
          <X size={16} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}

function AddressCard({ label, value, emptyText }: { label: string; value: string | null | undefined; emptyText: string }) {
  return (
    <div className="rounded-lg border border-border bg-bg px-3 py-3">
      <div className="text-xs text-text-muted">{label}</div>
      <div className="mt-2 break-all font-mono text-sm text-text">{value || emptyText}</div>
    </div>
  );
}
function TextField({
  label,
  value,
  onChange,
  disabled,
  type = 'text',
  min,
  max,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
  type?: 'text' | 'number';
  min?: number;
  max?: number;
}) {
  return (
    <label>
      <span className="text-xs font-medium text-text-muted">{label}</span>
      <input
        className="mt-2 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-accent"
        type={type}
        value={value}
        onChange={event => onChange(event.target.value)}
        disabled={disabled}
        min={min}
        max={max}
      />
    </label>
  );
}
