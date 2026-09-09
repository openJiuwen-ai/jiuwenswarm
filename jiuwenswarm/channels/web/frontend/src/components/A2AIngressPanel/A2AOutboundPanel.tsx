import { useCallback, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ChevronDown, Plus, Power, RefreshCw, Search, Server, Trash2, X } from 'lucide-react';
import { isEnterprise } from '../../edition';
import type { WebError } from '../../types/websocket';
import { Switch } from '../Switch';
import { A2AOutboundCredentialInput } from './A2AOutboundCredentialInput';
import {
  normalizeA2AOutboundAgent,
  normalizeA2AOutboundDiscovery,
  normalizeA2AOutboundList,
  normalizeA2AOutboundSettings,
  createA2AOutboundRequestScope,
  type A2AOutboundAgent,
  type A2AOutboundAvailability,
  type A2AOutboundDiscovery,
} from './a2aOutboundPanelState';

interface Props {
  isConnected: boolean;
  request: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;
  headerActionsContainer?: HTMLElement | null;
}

const BADGE: Record<A2AOutboundAvailability, string> = {
  available: 'bg-ok/15 text-ok',
  unreachable: 'bg-danger/15 text-danger',
  incompatible: 'bg-danger/15 text-danger',
  review_required: 'bg-warn/15 text-warn',
};

const errorMessage = (error: unknown): string => (error as WebError)?.message || String(error);

export function A2AOutboundPanel({ isConnected, request, headerActionsContainer }: Props) {
  const { t, i18n } = useTranslation();
  const enterpriseMode = isEnterprise();
  const [url, setUrl] = useState('');
  const [discovery, setDiscovery] = useState<A2AOutboundDiscovery | null>(null);
  const [discoveryDrawerOpen, setDiscoveryDrawerOpen] = useState(false);
  const [agents, setAgents] = useState<A2AOutboundAgent[]>([]);
  const [allowLoopbackHttp, setAllowLoopbackHttp] = useState(false);
  const [savedAllowLoopbackHttp, setSavedAllowLoopbackHttp] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [displayName, setDisplayName] = useState('');
  const [credential, setCredential] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<{
    agentId: string;
    displayName: string;
    connectTimeout: string;
    syncWait: string;
    credential: string;
    clearCredential: boolean;
  } | null>(null);
  const generationRef = useRef(createA2AOutboundRequestScope());
  const editGenerationRef = useRef(createA2AOutboundRequestScope());

  useEffect(() => {
    editGenerationRef.current.next();
    if (!isConnected) setEditing(null);
    return () => {
      editGenerationRef.current.next();
    };
  }, [isConnected]);

  const refresh = useCallback(async () => {
    if (!isConnected) return;
    const generation = generationRef.current.next();
    try {
      const [payload, rawSettings] = enterpriseMode
        ? [await request('a2a.outbound.list'), null]
        : await Promise.all([request('a2a.outbound.list'), request('a2a.outbound.settings.get')]);
      if (!generationRef.current.accepts(generation)) return;
      const next = normalizeA2AOutboundList(payload);
      const settings = enterpriseMode ? null : normalizeA2AOutboundSettings(rawSettings);
      if (!next || (!enterpriseMode && !settings)) throw new Error(t('a2aIngress.outbound.errors.invalidResponse'));
      setAgents(next);
      if (settings) {
        setAllowLoopbackHttp(settings.allow_loopback_http);
        setSavedAllowLoopbackHttp(settings.allow_loopback_http);
      }
      setError(null);
    } catch (nextError) {
      if (generationRef.current.accepts(generation)) setError(errorMessage(nextError));
    }
  }, [enterpriseMode, isConnected, request, t]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const closeDiscoveryDrawer = useCallback(() => {
    if (busy) return;
    setDiscoveryDrawerOpen(false);
    setDiscovery(null);
    setUrl('');
    setDisplayName('');
    setCredential('');
    setAdvancedOpen(false);
    setError(null);
    setNotice(null);
  }, [busy]);

  useEffect(() => {
    if (!discoveryDrawerOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeDiscoveryDrawer();
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [closeDiscoveryDrawer, discoveryDrawerOpen]);

  const discover = async () => {
    if (!url.trim() || busy || !isConnected) return;
    setBusy('discover');
    setError(null);
    setNotice(null);
    setDiscovery(null);
    try {
      const next = normalizeA2AOutboundDiscovery(await request('a2a.outbound.discover', { url: url.trim() }));
      if (!next) throw new Error(t('a2aIngress.outbound.errors.invalidResponse'));
      setDiscovery(next);
      setDisplayName(next.agent.name);
      setCredential('');
      setNotice(t('a2aIngress.outbound.discovery.previewReady'));
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setBusy(null);
    }
  };

  const updateAllowLoopbackHttp = async (nextEnabled: boolean) => {
    if (busy || !isConnected) return;
    const generation = generationRef.current.next();
    const previous = savedAllowLoopbackHttp;
    setAllowLoopbackHttp(nextEnabled);
    setBusy('settings');
    setError(null);
    setNotice(null);
    try {
      const settings = normalizeA2AOutboundSettings(
        await request('a2a.outbound.settings.update', {
          allow_loopback_http: nextEnabled,
        }),
      );
      if (!settings) throw new Error(t('a2aIngress.outbound.errors.invalidResponse'));
      if (!generationRef.current.accepts(generation)) return;
      setAllowLoopbackHttp(settings.allow_loopback_http);
      setSavedAllowLoopbackHttp(settings.allow_loopback_http);
      setNotice(t('a2aIngress.outbound.localDebug.saved'));
    } catch (nextError) {
      if (!generationRef.current.accepts(generation)) return;
      setAllowLoopbackHttp(previous);
      setError(errorMessage(nextError));
    } finally {
      setBusy(null);
    }
  };

  const register = async () => {
    if (!discovery || busy || !isConnected) return;
    setBusy('register');
    setError(null);
    try {
      const payload = await request('a2a.outbound.register', {
        discovery_id: discovery.discovery_id,
        display_name: displayName.trim() || discovery.agent.name,
        enabled: true,
        ...(credential ? { credential } : {}),
      });
      const created = normalizeA2AOutboundAgent(payload);
      if (!created) throw new Error(t('a2aIngress.outbound.errors.invalidResponse'));
      generationRef.current.next();
      setAgents(current => [created, ...current]);
      setDiscovery(null);
      setCredential('');
      setDiscoveryDrawerOpen(false);
      setUrl('');
      setAdvancedOpen(false);
      setNotice(t('a2aIngress.outbound.registered'));
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setBusy(null);
    }
  };

  const operate = async (agent: A2AOutboundAgent, operation: 'toggle' | 'refresh' | 'confirm' | 'reject' | 'delete') => {
    if (busy || !isConnected) return;
    if (operation === 'delete' && !window.confirm(t('a2aIngress.outbound.deleteConfirm'))) return;
    setBusy(`${operation}:${agent.agent_id}`);
    setError(null);
    setNotice(null);
    generationRef.current.next();
    try {
      let payload: unknown;
      if (operation === 'toggle') {
        payload = enterpriseMode
          ? await request('a2a.outbound.enabled.update', { agent_id: agent.agent_id, user_enabled: !agent.user_enabled })
          : await request('a2a.outbound.update', { agent_id: agent.agent_id, enabled: !agent.enabled });
      }
      else if (operation === 'refresh') payload = await request('a2a.outbound.refresh', { agent_id: agent.agent_id });
      else if (operation === 'confirm' || operation === 'reject')
        payload = await request('a2a.outbound.confirm_revision', { agent_id: agent.agent_id, accept: operation === 'confirm' });
      else payload = await request('a2a.outbound.delete', { agent_id: agent.agent_id });
      if (operation === 'delete') setAgents(current => current.filter(item => item.agent_id !== agent.agent_id));
      else {
        const updated = normalizeA2AOutboundAgent(payload);
        if (!updated) throw new Error(t('a2aIngress.outbound.errors.invalidResponse'));
        setAgents(current => current.map(item => (item.agent_id === updated.agent_id ? updated : item)));
      }
    } catch (nextError) {
      setError(errorMessage(nextError));
      void refresh();
    } finally {
      setBusy(null);
    }
  };

  const saveSettings = async (agent: A2AOutboundAgent) => {
    if (!editing || editing.agentId !== agent.agent_id || busy || !isConnected) return;
    const connectTimeout = Number(editing.connectTimeout);
    const syncWait = Number(editing.syncWait);
    if (!editing.displayName.trim() || !Number.isFinite(connectTimeout) || connectTimeout <= 0 || !Number.isFinite(syncWait) || syncWait <= 0) {
      setError(t('a2aIngress.outbound.errors.invalidSettings'));
      return;
    }
    setBusy(`save:${agent.agent_id}`);
    setError(null);
    generationRef.current.next();
    try {
      const payload = await request('a2a.outbound.update', {
        agent_id: agent.agent_id,
        display_name: editing.displayName.trim(),
        connect_timeout_seconds: connectTimeout,
        sync_wait_seconds: syncWait,
        ...(editing.credential ? { credential: editing.credential } : {}),
        ...(editing.clearCredential ? { clear_credential: true } : {}),
      });
      const updated = normalizeA2AOutboundAgent(payload);
      if (!updated) throw new Error(t('a2aIngress.outbound.errors.invalidResponse'));
      setAgents(current => current.map(item => (item.agent_id === updated.agent_id ? updated : item)));
      setEditing(null);
      setNotice(t('a2aIngress.outbound.saved'));
    } catch (nextError) {
      setError(errorMessage(nextError));
      void refresh();
    } finally {
      setBusy(null);
    }
  };

  const toggleSettings = async (agent: A2AOutboundAgent) => {
    if (busy || !isConnected) return;
    if (editing?.agentId === agent.agent_id) {
      setEditing(null);
      return;
    }
    setBusy(`edit:${agent.agent_id}`);
    setEditing(null);
    setError(null);
    const generation = editGenerationRef.current.next();
    try {
      const payload = await request<Record<string, unknown>>('a2a.outbound.edit', { agent_id: agent.agent_id });
      if (!editGenerationRef.current.accepts(generation)) return;
      const detail = normalizeA2AOutboundAgent(payload);
      if (!detail || typeof payload.credential !== 'string') throw new Error(t('a2aIngress.outbound.errors.invalidResponse'));
      setEditing({
        agentId: detail.agent_id,
        displayName: detail.display_name,
        connectTimeout: String(detail.connect_timeout_seconds),
        syncWait: String(detail.sync_wait_seconds),
        credential: payload.credential,
        clearCredential: false,
      });
    } catch (nextError) {
      if (editGenerationRef.current.accepts(generation)) setError(errorMessage(nextError));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 pr-1">
      {!enterpriseMode && headerActionsContainer &&
        createPortal(
          <button
            type="button"
            className="btn primary"
            onClick={() => {
              setError(null);
              setNotice(null);
              setDiscoveryDrawerOpen(true);
            }}
            disabled={!isConnected || !!busy}
          >
            <Plus size={16} />
            {t('a2aIngress.outbound.discovery.action')}
          </button>,
          headerActionsContainer,
        )}
      {!isConnected && <Notice tone="warn">{t('a2aIngress.disconnected')}</Notice>}
      {!discoveryDrawerOpen && error && (
        <Notice tone="danger" onClose={() => setError(null)} closeLabel={t('common.close')}>
          {error}
        </Notice>
      )}
      {!discoveryDrawerOpen && notice && (
        <Notice tone="ok" onClose={() => setNotice(null)} closeLabel={t('common.close')}>
          {notice}
        </Notice>
      )}

      {!enterpriseMode && discoveryDrawerOpen && (
        <div className="fixed inset-0 z-[1400] flex justify-end bg-overlay-cron-drawer" onClick={closeDiscoveryDrawer}>
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="a2a-discovery-drawer-title"
            className="relative flex h-full w-[640px] max-w-full flex-col bg-card shadow-xl animate-slide-in-right"
            onClick={event => event.stopPropagation()}
          >
            <div className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-5 py-4">
              <div className="min-w-0">
                <h3 id="a2a-discovery-drawer-title" className="text-xl font-bold text-text-strong">
                  {t('a2aIngress.outbound.discovery.title')}
                </h3>
                <p className="mt-1 text-sm text-text-muted">{t('a2aIngress.outbound.discovery.description')}</p>
              </div>
              <button
                type="button"
                className="grid h-9 w-9 shrink-0 place-items-center rounded-lg text-text-muted hover:bg-secondary/50 hover:text-text"
                onClick={closeDiscoveryDrawer}
                disabled={!!busy}
                aria-label={t('common.close')}
              >
                <X size={20} />
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto bg-secondary/10 p-5">
              <div className="grid gap-4">
                <section className="rounded-xl border border-border bg-card p-4">
                  <label className="text-xs font-medium text-text-muted" htmlFor="a2a-agent-card-url">
                    {t('a2aIngress.outbound.discovery.urlLabel')}
                  </label>
                  <div className="mt-2 flex flex-col gap-2 sm:flex-row">
                    <input
                      id="a2a-agent-card-url"
                      className="min-w-0 flex-1 rounded-md border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-accent"
                      value={url}
                      onChange={event => setUrl(event.target.value)}
                      placeholder="https://agent.example.com/.well-known/agent-card.json"
                      disabled={!!busy}
                    />
                    <button type="button" className="btn primary shrink-0" onClick={() => void discover()} disabled={!isConnected || !!busy || !url.trim()}>
                      <Search size={16} />
                      {busy === 'discover' ? t('a2aIngress.outbound.discovery.discovering') : t('a2aIngress.outbound.discovery.action')}
                    </button>
                  </div>
                  <div className="mt-4 border-t border-border pt-3">
                    <button
                      type="button"
                      className="flex w-full items-center justify-between gap-3 text-left text-sm font-medium text-text-muted hover:text-text"
                      aria-expanded={advancedOpen}
                      onClick={() => setAdvancedOpen(open => !open)}
                    >
                      <span>{t('a2aIngress.outbound.discovery.advanced')}</span>
                      <ChevronDown size={16} className={`shrink-0 transition-transform ${advancedOpen ? 'rotate-180' : ''}`} />
                    </button>
                    {advancedOpen && (
                      <div className="mt-3 rounded-lg bg-secondary/30 p-3">
                        <div className="flex flex-wrap items-center justify-between gap-3">
                          <div className="min-w-0 flex-1">
                            <div className="text-sm font-medium text-text">{t('a2aIngress.outbound.localDebug.allow')}</div>
                            <p className="mt-1 text-xs text-text-muted">{t('a2aIngress.outbound.localDebug.description')}</p>
                          </div>
                          <Switch
                            checked={allowLoopbackHttp}
                            onChange={nextEnabled => void updateAllowLoopbackHttp(nextEnabled)}
                            disabled={!isConnected || !!busy}
                            title={t('a2aIngress.outbound.localDebug.allow')}
                          />
                        </div>
                        {allowLoopbackHttp && (
                          <p className="mt-3 border-t border-warn/20 pt-3 text-xs text-warn">{t('a2aIngress.outbound.localDebug.warning')}</p>
                        )}
                      </div>
                    )}
                  </div>
                </section>

                {(error || notice) && (
                  <div className="grid gap-3">
                    {error && (
                      <Notice tone="danger" onClose={() => setError(null)} closeLabel={t('common.close')}>
                        {error}
                      </Notice>
                    )}
                    {notice && (
                      <Notice tone="ok" onClose={() => setNotice(null)} closeLabel={t('common.close')}>
                        {notice}
                      </Notice>
                    )}
                  </div>
                )}

                {discovery && (
                  <section className="grid gap-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <h4 className="text-sm font-semibold text-text">{t('a2aIngress.outbound.discovery.candidateTitle')}</h4>
                        <p className="mt-1 text-xs text-text-muted">{t('a2aIngress.outbound.discovery.candidateDescription')}</p>
                      </div>
                      <span className="rounded-full border border-border bg-card px-2.5 py-1 text-xs text-text-muted">
                        {t('a2aIngress.outbound.discovery.previewOnly')}
                      </span>
                    </div>
                    <div className="grid gap-4">
                      <div className="rounded-xl border border-border bg-card p-4">
                        <div className="flex items-start gap-3">
                          <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-accent/10 text-accent">
                            <Server size={20} />
                          </div>
                          <div className="min-w-0">
                            <h5 className="font-semibold text-text">{discovery.agent.name}</h5>
                            <p className="mt-1 text-sm leading-5 text-text-muted">{discovery.agent.description || '-'}</p>
                          </div>
                        </div>
                        <div className="mt-4 grid gap-3 rounded-lg bg-secondary/30 p-3 sm:grid-cols-2">
                          <Value label={t('a2aIngress.outbound.fields.version')} value={discovery.agent.version || '-'} />
                          <Value
                            label={t('a2aIngress.outbound.fields.protocol')}
                            value={`${discovery.agent.compatible_interfaces[0].protocol_binding} ${discovery.agent.compatible_interfaces[0].protocol_version}`}
                          />
                          <Value label={t('a2aIngress.outbound.fields.endpoint')} value={discovery.agent.compatible_interfaces[0].url} mono />
                          <Value label={t('a2aIngress.outbound.fields.expiresAt')} value={formatTime(discovery.expires_at, i18n.language)} />
                        </div>
                      </div>
                      <div className="rounded-xl border border-border bg-card p-4">
                        <h5 className="text-sm font-semibold text-text">{t('a2aIngress.outbound.discovery.registrationTitle')}</h5>
                        <p className="mt-1 text-xs text-text-muted">{t('a2aIngress.outbound.discovery.registrationDescription')}</p>
                        <div className="mt-4 grid gap-3 sm:grid-cols-2">
                          <Input label={t('a2aIngress.outbound.fields.displayName')} value={displayName} onChange={setDisplayName} />
                          <A2AOutboundCredentialInput card={discovery.agent_card} value={credential} onChange={setCredential} disabled={!!busy} />
                        </div>
                        <div className="mt-4 flex justify-end gap-2 border-t border-border pt-4">
                          <button type="button" className="btn secondary" onClick={() => setDiscovery(null)} disabled={!!busy}>
                            {t('common.cancel')}
                          </button>
                          <button type="button" className="btn primary" onClick={() => void register()} disabled={!!busy}>
                            {busy === 'register' ? t('a2aIngress.outbound.registering') : t('a2aIngress.outbound.register')}
                          </button>
                        </div>
                      </div>
                    </div>
                  </section>
                )}
              </div>
            </div>
          </section>
        </div>
      )}

      <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-panel-strong/60">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold text-text">{t('a2aIngress.outbound.list.title')}</h3>
              <span className="rounded-full bg-secondary px-2 py-0.5 text-xs text-text-muted">{agents.length}</span>
            </div>
            <p className="mt-1 text-xs text-text-muted">
              {t(enterpriseMode ? 'a2aIngress.outbound.list.enterpriseDescription' : 'a2aIngress.outbound.list.description')}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button type="button" className="btn secondary" onClick={() => void refresh()} disabled={!isConnected || !!busy}>
              <RefreshCw size={16} />
              {t('common.refresh')}
            </button>
          </div>
        </div>
        {agents.length === 0 ? (
          <div className="grid min-h-0 flex-1 place-items-center px-4 py-12 text-center text-sm text-text-muted">
            {t(enterpriseMode ? 'a2aIngress.outbound.list.enterpriseEmpty' : 'a2aIngress.outbound.list.empty')}
          </div>
        ) : (
          <div className="grid min-h-0 flex-1 content-start gap-3 overflow-auto p-4">
            {agents.map(agent => {
              const isEditing = editing?.agentId === agent.agent_id;
              const settingsPanelId = `a2a-agent-settings-${agent.agent_id}`;
              return (
                <article key={agent.agent_id} className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
                  <div className="p-4">
                    <div className="flex min-w-0 items-start gap-3">
                      <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg bg-accent/10 text-accent">
                        <Server size={20} />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <h4 className="text-base font-semibold text-text">{agent.display_name}</h4>
                          <span className={`rounded-full px-2 py-0.5 text-xs ${BADGE[agent.availability]}`}>
                            {t(`a2aIngress.outbound.availability.${agent.availability}`)}
                          </span>
                          <span className="rounded-full bg-secondary px-2 py-0.5 text-xs text-text-muted">
                            {enterpriseMode && !agent.manager_enabled
                              ? t('a2aIngress.outbound.managerDisabled')
                              : agent.effective_enabled
                                ? t('a2aIngress.outbound.enabled')
                                : t('a2aIngress.outbound.disabled')}
                          </span>
                        </div>
                        <div className="mt-1 break-all font-mono text-xs text-text-muted">{agent.selected_interface.url}</div>
                      </div>
                    </div>
                    <div className={`mt-4 grid gap-3 rounded-lg border border-border bg-secondary/20 p-3 sm:grid-cols-2 ${enterpriseMode ? 'xl:grid-cols-3' : 'xl:grid-cols-4'}`}>
                      <Value
                        label={t('a2aIngress.outbound.fields.protocol')}
                        value={`${agent.selected_interface.protocol_binding} ${agent.selected_interface.protocol_version}`}
                      />
                      <Value label={t('a2aIngress.outbound.fields.revision')} value={String(agent.card_revision)} />
                      {!enterpriseMode && (
                        <Value
                          label={t('a2aIngress.outbound.fields.credentialState')}
                          value={agent.has_credential ? t('a2aIngress.outbound.configured') : t('a2aIngress.outbound.notConfigured')}
                        />
                      )}
                      <Value
                        label={t('a2aIngress.outbound.fields.lastChecked')}
                        value={agent.last_checked_at ? formatTime(agent.last_checked_at, i18n.language) : '-'}
                      />
                    </div>
                    {agent.last_error_summary && <p className="mt-3 rounded-md bg-danger/10 px-3 py-2 text-xs text-danger">{agent.last_error_summary}</p>}
                    {!enterpriseMode && <div className="mt-4 border-t border-border pt-3">
                      <button
                        type="button"
                        className="flex w-full items-center justify-between gap-3 text-left text-sm font-medium text-text-muted hover:text-text"
                        onClick={() => toggleSettings(agent)}
                        disabled={!!busy}
                        aria-expanded={isEditing}
                        aria-controls={settingsPanelId}
                      >
                        <span>{t('a2aIngress.outbound.settings')}</span>
                        <ChevronDown size={16} className={`shrink-0 transition-transform ${isEditing ? 'rotate-180' : ''}`} />
                      </button>
                      {isEditing && editing && (
                        <div id={settingsPanelId} className="mt-3 rounded-lg bg-secondary/30 p-3">
                          <div className="grid gap-3 md:grid-cols-2">
                            <Input
                              label={t('a2aIngress.outbound.fields.displayName')}
                              value={editing.displayName}
                              onChange={value => setEditing(current => (current ? { ...current, displayName: value } : current))}
                            />
                            <Input
                              label={t('a2aIngress.outbound.fields.connectTimeout')}
                              value={editing.connectTimeout}
                              onChange={value => setEditing(current => (current ? { ...current, connectTimeout: value } : current))}
                            />
                            <Input
                              label={t('a2aIngress.outbound.fields.syncWait')}
                              value={editing.syncWait}
                              onChange={value => setEditing(current => (current ? { ...current, syncWait: value } : current))}
                            />
                            <A2AOutboundCredentialInput
                              card={agent.agent_card}
                              value={editing.credential}
                              onChange={value => setEditing(current => (current ? { ...current, credential: value, clearCredential: false } : current))}
                              disabled={!!busy || editing.clearCredential}
                            />
                          </div>
                          <label className="mt-3 flex items-center gap-2 text-sm text-text">
                            <input
                              type="checkbox"
                              checked={editing.clearCredential}
                              onChange={event =>
                                setEditing(current =>
                                  current
                                    ? { ...current, clearCredential: event.target.checked, credential: event.target.checked ? '' : current.credential }
                                    : current,
                                )
                              }
                            />
                            {t('a2aIngress.outbound.clearCredential')}
                          </label>
                          <div className="mt-4 flex justify-end gap-2 border-t border-border pt-4">
                            <button type="button" className="btn secondary" onClick={() => setEditing(null)}>
                              {t('common.cancel')}
                            </button>
                            <button type="button" className="btn primary" onClick={() => void saveSettings(agent)} disabled={!!busy}>
                              {t('common.save')}
                            </button>
                          </div>
                        </div>
                      )}
                    </div>}
                    {!enterpriseMode && agent.availability === 'review_required' && (
                      <div className="mt-4 rounded-lg border border-warn/30 bg-warn/10 p-3 text-sm">
                        <p>{t('a2aIngress.outbound.reviewRequired')}</p>
                        <PendingDiff agent={agent} label={t('a2aIngress.outbound.endpointChange')} />
                        <div className="mt-3 flex gap-2">
                          <button type="button" className="btn primary" onClick={() => void operate(agent, 'confirm')} disabled={!!busy}>
                            {t('a2aIngress.outbound.confirm')}
                          </button>
                          <button type="button" className="btn secondary" onClick={() => void operate(agent, 'reject')} disabled={!!busy}>
                            {t('a2aIngress.outbound.reject')}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                  {enterpriseMode ? (
                    <div className="flex items-center justify-end gap-3 border-t border-border bg-secondary/10 px-4 py-3">
                      <span className="text-sm text-text-muted">
                        {agent.manager_enabled
                          ? t(agent.user_enabled ? 'a2aIngress.outbound.disable' : 'a2aIngress.outbound.enable')
                          : t('a2aIngress.outbound.managerDisabled')}
                      </span>
                      <Switch
                        checked={agent.user_enabled}
                        onChange={() => void operate(agent, 'toggle')}
                        disabled={!isConnected || !!busy || !agent.manager_enabled}
                        title={agent.manager_enabled ? t('a2aIngress.outbound.userToggle') : t('a2aIngress.outbound.managerDisabled')}
                      />
                    </div>
                  ) : (
                    <div className="flex flex-wrap justify-end gap-2 border-t border-border bg-secondary/10 px-4 py-3">
                      <button
                        type="button"
                        className="btn secondary"
                        onClick={() => void operate(agent, 'toggle')}
                        disabled={!!busy || agent.availability === 'review_required'}
                      >
                        <Power size={16} />
                        {agent.enabled ? t('a2aIngress.outbound.disable') : t('a2aIngress.outbound.enable')}
                      </button>
                      <button type="button" className="btn secondary" onClick={() => void operate(agent, 'refresh')} disabled={!!busy}>
                        <RefreshCw size={16} />
                        {t('a2aIngress.outbound.refresh')}
                      </button>
                      <button type="button" className="btn secondary text-danger" onClick={() => void operate(agent, 'delete')} disabled={!!busy}>
                        <Trash2 size={16} />
                        {t('common.delete')}
                      </button>
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

function formatTime(value: string, language: string): string {
  return new Date(value).toLocaleString(language.startsWith('zh') ? 'zh-CN' : 'en-US', { hour12: false });
}
function Notice({
  tone,
  children,
  onClose,
  closeLabel,
}: {
  tone: 'warn' | 'danger' | 'ok';
  children: React.ReactNode;
  onClose?: () => void;
  closeLabel?: string;
}) {
  const style = tone === 'danger' ? 'border-danger/30 bg-danger/10' : tone === 'warn' ? 'border-warn/30 bg-warn/10' : 'border-ok/30 bg-ok/10';
  return (
    <div
      className={`flex items-center justify-between gap-3 rounded-xl border px-4 py-3 text-sm text-text ${style}`}
      role={tone === 'danger' ? 'alert' : 'status'}
    >
      <div className="min-w-0 flex-1 leading-5">{children}</div>
      {onClose && (
        <button
          type="button"
          className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-text-muted hover:bg-black/5 hover:text-text"
          onClick={onClose}
          aria-label={closeLabel}
        >
          <X size={16} />
        </button>
      )}
    </div>
  );
}
function Value({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-text-muted">{label}</div>
      <div className={`mt-1 break-all text-sm text-text ${mono ? 'font-mono' : ''}`}>{value}</div>
    </div>
  );
}
function Input({ label, value, onChange, type = 'text' }: { label: string; value: string; onChange: (value: string) => void; type?: 'text' | 'password' }) {
  return (
    <label>
      <span className="text-xs font-medium text-text-muted">{label}</span>
      <input
        type={type}
        className="mt-2 w-full rounded-md border border-border bg-bg px-3 py-2 text-sm text-text outline-none focus:border-accent"
        value={value}
        onChange={event => onChange(event.target.value)}
      />
    </label>
  );
}
function PendingDiff({ agent, label }: { agent: A2AOutboundAgent; label: string }) {
  const pending = agent.pending_revision?.selected_interface as Record<string, unknown> | undefined;
  const nextUrl = typeof pending?.url === 'string' ? pending.url : '';
  if (!nextUrl || nextUrl === agent.selected_interface.url) return null;
  return (
    <div className="mt-2 grid gap-1 rounded-md bg-bg/70 p-2 text-xs">
      <span className="font-medium text-text">{label}</span>
      <span className="break-all text-text-muted">{agent.selected_interface.url}</span>
      <span className="break-all text-warn">→ {nextUrl}</span>
    </div>
  );
}
