import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { webClient, webRequest } from '../../services/webClient';
import { ImAuthorizeDialog } from './components/ImAuthorizeDialog';
import { JiuwenMateConfirmDialog } from './components/JiuwenMateDialog';
import { JiuwenMateRail, type JiuwenMateRailView } from './components/JiuwenMateRail';
import {
  cancelColleagueCreation,
  closeCustomColleagueDialog,
  closeTemplateDetail,
  completeColleagueCreation,
  createInitialJiuwenMateState,
  deleteColleague,
  finishColleagueAuthorization,
  openColleagueAuthorization,
  openColleagueCreation,
  openColleagueMessages,
  openColleagueProfile,
  openColleagueWorkspace,
  openCustomColleagueCreation,
  openOffice,
  replaceColleagues,
  selectColleagueTemplate,
  setColleagueDisabled,
  startColleagueRefinement,
  updateColleagueDraft,
  type JiuwenMateState,
} from './createFlowState';
import { createJiuwenMateApi, errorMessage, type JiuwenMateModelCatalog } from './jiuwenmateApi';
import { emptyCapabilities } from './mockData';
import { ColleagueCreatePage } from './pages/ColleagueCreatePage';
import { ColleagueEmptyPage } from './pages/ColleagueEmptyPage';
import { ColleagueProfilePage, type JiuwenMateProfileSection } from './pages/ColleagueProfilePage';
import { ColleagueWorkspacePage, type JiuwenMateWorkspaceIntent } from './pages/ColleagueWorkspacePage';
import { OfficeDashboardPage } from './pages/OfficeDashboardPage';
import { OfficeIntroPage } from './pages/OfficeIntroPage';
import type { JiuwenMateColleague, JiuwenMateExistingPage } from './types';
import './jiuwenmate.css';

export interface JiuwenMateAppProps {
  onExit: () => void;
  renderExistingPage: (nav: JiuwenMateExistingPage) => ReactNode;
  confirmDiscardSettings?: () => boolean;
}

type PendingConfirm = { kind: 'delete' | 'disable'; colleague: JiuwenMateColleague };
type LoadStatus = 'loading' | 'ready' | 'failed';

const EMPTY_MODEL_CATALOG: JiuwenMateModelCatalog = { models: [], defaultModelId: '', defaultModelLabel: '' };

export function JiuwenMateApp({ onExit, renderExistingPage, confirmDiscardSettings }: JiuwenMateAppProps) {
  const { t } = useTranslation();
  const api = useMemo(
    () =>
      createJiuwenMateApi(
        (method, params) => webRequest(method, params),
        (event, handler) => webClient.on(event, (frame) => handler(frame.payload)),
      ),
    [],
  );
  const [state, setState] = useState(() => createInitialJiuwenMateState([]));
  const [loadStatus, setLoadStatus] = useState<LoadStatus>('loading');
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [modelCatalog, setModelCatalog] = useState<JiuwenMateModelCatalog>(EMPTY_MODEL_CATALOG);
  const [activeRailView, setActiveRailView] = useState<JiuwenMateRailView>('office');
  const [existingPage, setExistingPage] = useState<JiuwenMateExistingPage | null>(null);
  const [publicLabel, setPublicLabel] = useState<string | null>(null);
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm | null>(null);
  const [workspaceIntent, setWorkspaceIntent] = useState<{ nonce: number; intent: JiuwenMateWorkspaceIntent | null }>({
    nonce: 0,
    intent: null,
  });
  const [profileIntent, setProfileIntent] = useState<{ nonce: number; section: JiuwenMateProfileSection }>({
    nonce: 0,
    section: 'overview',
  });
  const [connectionEpoch, setConnectionEpoch] = useState(0);

  const loadColleagues = useCallback(async () => {
    setLoadStatus('loading');
    setLoadError('');
    try {
      const colleagues = await api.loadColleagues();
      setState((current) => replaceColleagues(current, colleagues));
      setLoadStatus('ready');
    } catch (error) {
      setLoadError(errorMessage(error));
      setLoadStatus('failed');
    }
  }, [api]);

  useEffect(() => {
    void loadColleagues();
  }, [loadColleagues]);

  useEffect(() => {
    let last = webClient.getState();
    return webClient.onStateChange((next) => {
      if (next === 'ready' && last !== 'ready') setConnectionEpoch((epoch) => epoch + 1);
      last = next;
    });
  }, []);

  useEffect(() => {
    let active = true;
    api.listModels().then(
      (catalog) => {
        if (active) setModelCatalog(catalog);
      },
      () => undefined,
    );
    return () => {
      active = false;
    };
  }, [api]);

  const canLeaveSettings = (nextPage: JiuwenMateExistingPage | null) => {
    if (existingPage !== 'settings' || nextPage === 'settings') return true;
    return confirmDiscardSettings?.() ?? true;
  };

  const showFeature = (railView: JiuwenMateRailView, update: (current: JiuwenMateState) => JiuwenMateState) => {
    if (!canLeaveSettings(null)) return;
    setExistingPage(null);
    setPublicLabel(null);
    setActiveRailView(railView);
    setState(update);
  };

  const handleRailViewChange = (view: JiuwenMateRailView) => {
    showFeature(view, view === 'office' ? openOffice : openColleagueMessages);
  };

  const handleOpenExistingPage = (nav: JiuwenMateExistingPage, labelKey: string) => {
    if (!canLeaveSettings(nav)) return;
    setExistingPage(nav);
    setPublicLabel(labelKey);
  };

  const openPlaza = () => handleOpenExistingPage('agents', 'jiuwenmate.nav.plaza');
  const startCreation = () => setState((current) => openColleagueCreation(current));
  const startCustomCreation = () => setState((current) => openCustomColleagueCreation(current));

  const openWorkspace = (colleagueId: string, intent: JiuwenMateWorkspaceIntent | null = null) => {
    setWorkspaceIntent((current) => ({ nonce: current.nonce + 1, intent }));
    showFeature('messages', (current) => openColleagueWorkspace(current, colleagueId));
  };

  const openProfile = (colleagueId: string, section: JiuwenMateProfileSection = 'overview') => {
    setProfileIntent((current) => ({ nonce: current.nonce + 1, section }));
    showFeature('office', (current) => openColleagueProfile(current, colleagueId));
  };

  const requestToggleDisabled = (colleagueId: string) => {
    const colleague = state.colleagues.find((item) => item.id === colleagueId);
    if (!colleague) return;
    if (colleague.disabled) {
      setState((current) => setColleagueDisabled(current, colleagueId, false));
      return;
    }
    setPendingConfirm({ kind: 'disable', colleague });
  };

  const requestDelete = (colleagueId: string) => {
    const colleague = state.colleagues.find((item) => item.id === colleagueId);
    if (colleague) setPendingConfirm({ kind: 'delete', colleague });
  };

  const confirmPending = async () => {
    if (!pendingConfirm) return;
    const { kind, colleague } = pendingConfirm;
    setPendingConfirm(null);
    if (kind === 'disable') {
      setState((current) => setColleagueDisabled(current, colleague.id, true));
      return;
    }
    setActionError('');
    try {
      await api.deleteColleague(colleague.id);
      setState((current) => deleteColleague(current, colleague.id));
    } catch (error) {
      setActionError(t('jiuwenmate.load.deleteFailed', { reason: errorMessage(error) }));
    }
  };

  const completeCreation = async () => {
    const draft = state.draft;
    if (!draft || submitting) return;
    setSubmitting(true);
    setActionError('');
    try {
      const created = await api.createColleague(draft, modelCatalog.models);
      const colleague: JiuwenMateColleague = { ...created, avatarUrl: created.avatarUrl ?? draft.avatarUrl };
      setProfileIntent((current) => ({ nonce: current.nonce + 1, section: 'overview' }));
      setActiveRailView('office');
      setState((current) => completeColleagueCreation(current, colleague));
    } catch (error) {
      setActionError(t('jiuwenmate.load.createFailed', { reason: errorMessage(error) }));
    } finally {
      setSubmitting(false);
    }
  };

  const listHandlers = {
    onCreateFromPlaza: startCreation,
    onCreateCustom: startCustomCreation,
    onOpenSchedules: () => undefined,
    onManage: (id: string) => openProfile(id),
    onToggleDisabled: requestToggleDisabled,
    onDelete: requestDelete,
  };

  const selectedColleague = state.colleagues.find((item) => item.id === state.selectedColleagueId) ?? null;
  const authorizeColleague = state.colleagues.find((item) => item.id === state.authorizeColleagueId) ?? null;

  const renderFeature = () => {
    if (loadStatus !== 'ready' && state.view !== 'colleague-create') {
      return (
        <div className="jiuwenmate-load-state" data-testid="jiuwenmate-load-state" data-variant={loadStatus}>
          {loadStatus === 'loading' ? (
            <p>{t('jiuwenmate.load.loading')}</p>
          ) : (
            <>
              <p role="alert">{t('jiuwenmate.load.failed', { reason: loadError })}</p>
              <button type="button" className="jiuwenmate-outline-button" onClick={() => void loadColleagues()}>
                {t('jiuwenmate.load.retry')}
              </button>
            </>
          )}
        </div>
      );
    }
    switch (state.view) {
      case 'office-intro':
        return <OfficeIntroPage onCreate={startCreation} />;
      case 'colleague-empty':
        return <ColleagueEmptyPage onCreate={startCreation} {...listHandlers} />;
      case 'colleague-create':
        return (
          <ColleagueCreatePage
            step={state.creationStep}
            source={state.creationSource}
            detailTemplateId={state.detailTemplateId}
            customDialogOpen={state.customDialogOpen}
            draft={state.draft}
            modelCatalog={modelCatalog}
            submitting={submitting}
            onOpenTemplateDetail={(id) => setState((current) => selectColleagueTemplate(current, id))}
            onCloseTemplateDetail={() => setState((current) => closeTemplateDetail(current))}
            onChooseTemplate={(template) =>
              setState((current) =>
                startColleagueRefinement(current, 'template', {
                  name: template.name,
                  description: template.description,
                  tags: [...template.tags],
                  category: template.category,
                  avatarUrl: template.avatarUrl,
                  capabilities: {
                    ...template.capabilities,
                    skills: [...template.capabilities.skills],
                    connectors: [...template.capabilities.connectors],
                    plugins: [...template.capabilities.plugins],
                    knowledge: [...template.capabilities.knowledge],
                    starterPrompts: [...template.capabilities.starterPrompts],
                  },
                }),
              )
            }
            onOpenCustom={startCustomCreation}
            onCloseCustom={() => setState((current) => closeCustomColleagueDialog(current))}
            onConfirmCustom={({ name, description }) =>
              setState((current) =>
                startColleagueRefinement(current, 'custom', {
                  name,
                  description,
                  tags: [],
                  category: 'custom',
                  capabilities: emptyCapabilities(),
                }),
              )
            }
            onOpenPlaza={openPlaza}
            onDraftChange={(patch) => setState((current) => updateColleagueDraft(current, patch))}
            onCancel={() => setState((current) => cancelColleagueCreation(current))}
            onComplete={() => void completeCreation()}
          />
        );
      case 'office-dashboard':
        return (
          <OfficeDashboardPage
            colleagues={state.colleagues}
            onCreate={startCreation}
            onChat={(id) => openWorkspace(id)}
            onManage={(id) => openProfile(id)}
            onToggleDisabled={requestToggleDisabled}
            onDelete={requestDelete}
            onOpenTrace={(colleagueId, channelId) => openWorkspace(colleagueId, { recordTab: 'channels', channelId, trace: true })}
          />
        );
      case 'colleague-profile':
        return selectedColleague ? (
          <ColleagueProfilePage
            key={`${selectedColleague.id}-${profileIntent.nonce}`}
            colleague={selectedColleague}
            modelCatalog={modelCatalog}
            initialSection={profileIntent.section}
            onOpenOffice={() => showFeature('office', openOffice)}
            onChat={() => openWorkspace(selectedColleague.id)}
            onConfigureIm={() => setState((current) => openColleagueAuthorization(current, selectedColleague.id))}
            onToggleDisabled={() => requestToggleDisabled(selectedColleague.id)}
            onDelete={() => requestDelete(selectedColleague.id)}
          />
        ) : null;
      case 'colleague-workspace':
        return selectedColleague ? (
          <ColleagueWorkspacePage
            key={workspaceIntent.nonce}
            colleagues={state.colleagues}
            selectedColleagueId={selectedColleague.id}
            intent={workspaceIntent.intent}
            chat={api}
            connectionEpoch={connectionEpoch}
            onSelectColleague={(id) => setState((current) => ({ ...current, selectedColleagueId: id }))}
            onCreateSchedule={(id) => openProfile(id, 'schedules')}
            {...listHandlers}
          />
        ) : null;
      default:
        return null;
    }
  };

  const flush = !existingPage && (state.view === 'colleague-workspace' || state.view === 'colleague-empty');

  return (
    <div className="jiuwenmate-shell" data-testid="jiuwenmate-shell" data-variant={state.view}>
      <JiuwenMateRail
        activeView={activeRailView}
        activePublicLabel={publicLabel}
        onViewChange={handleRailViewChange}
        onOpenExistingPage={handleOpenExistingPage}
        onExit={onExit}
      />
      <main
        className={`jiuwenmate-shell__content${existingPage ? ' jiuwenmate-shell__content--reused' : ''}${flush ? ' jiuwenmate-shell__content--flush' : ''}`}
        data-testid="jiuwenmate-content"
        aria-label={t('jiuwenmate.title')}
      >
        {existingPage ? (
          <div className="jiuwenmate-reused-page" data-testid="jiuwenmate-reused-page" data-variant={existingPage}>
            {renderExistingPage(existingPage)}
          </div>
        ) : (
          renderFeature()
        )}
      </main>
      {actionError && !existingPage ? (
        <div className="jiuwenmate-action-error" role="alert" data-testid="jiuwenmate-action-error">
          <span>{actionError}</span>
          <button type="button" className="jiuwenmate-outline-button" onClick={() => setActionError('')}>
            {t('jiuwenmate.common.confirm')}
          </button>
        </div>
      ) : null}
      {authorizeColleague && !existingPage ? (
        <ImAuthorizeDialog
          onSkip={() => setState((current) => finishColleagueAuthorization(current, false))}
          onConfirm={() => setState((current) => finishColleagueAuthorization(current, true))}
        />
      ) : null}
      {pendingConfirm ? (
        <JiuwenMateConfirmDialog
          title={pendingConfirm.kind === 'delete' ? t('jiuwenmate.office.delete') : t('jiuwenmate.office.disable')}
          warning={pendingConfirm.kind === 'delete' ? t('jiuwenmate.office.deleteWarning') : t('jiuwenmate.office.disableWarning')}
          message={
            <>
              {pendingConfirm.kind === 'delete' ? t('jiuwenmate.office.deleteConfirm') : t('jiuwenmate.office.disableConfirm')}
              <strong>{pendingConfirm.colleague.name}</strong>
            </>
          }
          onCancel={() => setPendingConfirm(null)}
          onConfirm={() => void confirmPending()}
        />
      ) : null}
    </div>
  );
}
