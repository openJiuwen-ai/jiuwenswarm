import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
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
  officeActivityLoad,
  openOffice,
  replaceColleagues,
  selectColleagueTemplate,
  setColleagueDisabled,
  startColleagueRefinement,
  updateColleagueDraft,
  type JiuwenMateState,
} from './createFlowState';
import {
  createJiuwenMateApi,
  errorMessage,
  selectableColleagueResources,
  selectableKnowledgePages,
  selectablePluginPackages,
  withColleagueEdit,
  type ColleagueResourceOption,
  type JiuwenMateModelCatalog,
  type JiuwenMateSchedule,
} from './jiuwenmateApi';
import {
  buildTemplateSkillSearchQuery,
  recommendInstalledTemplateSkills,
  recommendMarketplaceTemplateSkills,
  splitAvailableColleagueCapabilities,
  type MutableColleagueCapabilitySelections,
  type SkillInstallCandidate,
  type TemplateSkillRecommendations,
} from './colleagueCapabilities';
import type { ScheduleDraft } from './schedules/types';
import { customAvatarHue } from './colleaguePortraits';
import { emptyCapabilities } from './mockData';
import { ColleagueCreatePage } from './pages/ColleagueCreatePage';
import { ColleagueEmptyPage } from './pages/ColleagueEmptyPage';
import {
  ColleagueProfilePage,
  type ColleagueResourceCatalog,
  type JiuwenMateProfileSection,
} from './pages/ColleagueProfilePage';
import { ColleagueWorkspacePage, type JiuwenMateWorkspaceIntent } from './pages/ColleagueWorkspacePage';
import { OfficeDashboardPage } from './pages/OfficeDashboardPage';
import { OfficeIntroPage } from './pages/OfficeIntroPage';
import type {
  JiuwenMateActivity,
  JiuwenMateColleague,
  JiuwenMateExistingPage,
  JiuwenMateTemplate,
} from './types';
import './jiuwenmate.css';

export interface JiuwenMateAppProps {
  onExit: () => void;
  renderExistingPage: (nav: JiuwenMateExistingPage) => ReactNode;
  confirmDiscardSettings?: () => boolean;
}

type PendingConfirm = { kind: 'delete' | 'disable'; colleague: JiuwenMateColleague };
type LoadStatus = 'loading' | 'ready' | 'failed';

const EMPTY_MODEL_CATALOG: JiuwenMateModelCatalog = { models: [], defaultModelId: '', defaultModelLabel: '' };
const SCHEDULE_REFRESH_INTERVAL_MS = 5_000;
const EMPTY_MISSING_CAPABILITIES: MutableColleagueCapabilitySelections = {
  skills: [],
  connectors: [],
  plugins: [],
  knowledge: [],
};
const EMPTY_TEMPLATE_SKILL_RECOMMENDATIONS: TemplateSkillRecommendations = {
  status: 'idle',
  local: [],
  plaza: [],
  error: null,
};

interface SkillInstallReply {
  success?: boolean;
  pending?: boolean;
  install_id?: string;
  status?: string;
  detail?: string;
  skill?: { name?: string };
}

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
  const [resourceCatalog, setResourceCatalog] = useState<ColleagueResourceCatalog>({
    skills: [],
    connectors: [],
    plugins: [],
    knowledge: [],
    ready: false,
  });
  const [resourceCatalogEpoch, setResourceCatalogEpoch] = useState(0);
  const [missingTemplateCapabilities, setMissingTemplateCapabilities] = useState(EMPTY_MISSING_CAPABILITIES);
  const [skillRecommendations, setSkillRecommendations] = useState(EMPTY_TEMPLATE_SKILL_RECOMMENDATIONS);
  const skillRecommendationRequest = useRef(0);
  const [savingResources, setSavingResources] = useState(false);
  const [activity, setActivity] = useState<JiuwenMateActivity | null>(null);
  const [activityStatus, setActivityStatus] = useState<LoadStatus>('loading');
  const [activityError, setActivityError] = useState('');
  const [schedules, setSchedules] = useState<JiuwenMateSchedule[]>([]);
  const [savingSchedule, setSavingSchedule] = useState(false);

  const loadColleagues = useCallback(async () => {
    setLoadStatus('loading');
    setLoadError('');
    try {
      const [colleagues, bindingResult] = await Promise.all([
        api.loadColleagues(),
        webRequest<{ agent_ids?: string[] }>('channel.feishu.get_bindings').then(
          (value) => ({ ok: true as const, value }),
          () => ({ ok: false as const, value: null }),
        ),
      ]);
      setState((current) =>
        replaceColleagues(
          current,
          colleagues.map((colleague) =>
            bindingResult.ok
              ? { ...colleague, imConfigured: (bindingResult.value.agent_ids ?? []).includes(colleague.id) }
              : colleague,
          ),
        ),
      );
      setLoadStatus('ready');
    } catch (error) {
      setLoadError(errorMessage(error));
      setLoadStatus('failed');
    }
  }, [api]);

  const activityStatusRef = useRef(activityStatus);
  activityStatusRef.current = activityStatus;
  const activityRequest = useRef(0);
  const loadActivity = useCallback(async (silent = false) => {
    const requestId = activityRequest.current + 1;
    activityRequest.current = requestId;
    if (!silent) {
      setActivityStatus('loading');
      setActivityError('');
    }
    try {
      const next = await api.loadActivity();
      if (requestId !== activityRequest.current) return;
      setActivity(next);
      setActivityStatus('ready');
      setActivityError('');
    } catch (error) {
      if (requestId !== activityRequest.current || silent) return;
      setActivity(null);
      setActivityError(errorMessage(error));
      setActivityStatus('failed');
    }
  }, [api]);

  useEffect(() => {
    void loadColleagues();
  }, [loadColleagues]);

  useEffect(() => {
    if (loadStatus !== 'ready') return;
    const plan = officeActivityLoad(state.view, activityStatusRef.current);
    if (plan === 'idle') return;
    void loadActivity(plan === 'refresh');
  }, [loadActivity, loadStatus, connectionEpoch, state.view]);

  useEffect(() => {
    let last = webClient.getState();
    return webClient.onStateChange((next) => {
      if (next === 'ready' && last !== 'ready') setConnectionEpoch((epoch) => epoch + 1);
      last = next;
    });
  }, []);

  useEffect(() => {
    if (state.view !== 'colleague-profile' && state.view !== 'colleague-create') return undefined;
    let active = true;
    setResourceCatalog({ skills: [], connectors: [], plugins: [], knowledge: [], ready: false });
    void (async () => {
      const [skillsResult, connectorsResult, pluginsResult, knowledgeResult] = await Promise.allSettled([
        webRequest('skills.list', { with_installed: true }),
        webRequest('mcp.list', { filter: 'local' }),
        webRequest('plugin_packages.list', { filter: 'mine' }),
        webRequest('jiuwenmate.knowledge.list', {}),
      ]);
      if (!active) return;
      const selected = selectableColleagueResources(
        skillsResult.status === 'fulfilled' ? skillsResult.value : null,
        connectorsResult.status === 'fulfilled' ? connectorsResult.value : null,
      );
      const side = (result: PromiseSettledResult<unknown>, options: ColleagueResourceOption[]) =>
        result.status === 'fulfilled' ? options : null;
      setResourceCatalog({
        skills: side(skillsResult, selected.skills),
        connectors: side(connectorsResult, selected.connectors),
        plugins: side(
          pluginsResult,
          selectablePluginPackages(pluginsResult.status === 'fulfilled' ? pluginsResult.value : null),
        ),
        knowledge: side(
          knowledgeResult,
          selectableKnowledgePages(knowledgeResult.status === 'fulfilled' ? knowledgeResult.value : null),
        ),
        ready: true,
      });
    })();
    return () => {
      active = false;
    };
  }, [connectionEpoch, resourceCatalogEpoch, state.view]);

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
  const startCreation = () => {
    skillRecommendationRequest.current += 1;
    setActionError('');
    setMissingTemplateCapabilities(EMPTY_MISSING_CAPABILITIES);
    setSkillRecommendations(EMPTY_TEMPLATE_SKILL_RECOMMENDATIONS);
    setState((current) => openColleagueCreation(current));
  };
  const startCustomCreation = () => {
    skillRecommendationRequest.current += 1;
    setActionError('');
    setMissingTemplateCapabilities(EMPTY_MISSING_CAPABILITIES);
    setSkillRecommendations(EMPTY_TEMPLATE_SKILL_RECOMMENDATIONS);
    setState((current) => openCustomColleagueCreation(current));
  };

  const chooseTemplate = (template: JiuwenMateTemplate) => {
    if (!resourceCatalog.ready) {
      setActionError(t('jiuwenmate.profile.catalogLoading'));
      return;
    }
    const { available, missing } = splitAvailableColleagueCapabilities(
      template.capabilities,
      resourceCatalog,
    );
    const requestId = skillRecommendationRequest.current + 1;
    skillRecommendationRequest.current = requestId;
    const localRecommendations = recommendInstalledTemplateSkills(
      template.skillSearchTerms ?? [],
      resourceCatalog.skills ?? [],
    );
    setSkillRecommendations({
      status: 'loading',
      local: localRecommendations,
      plaza: [],
      error: null,
    });
    setActionError('');
    setMissingTemplateCapabilities(missing);
    setState((current) =>
      startColleagueRefinement(current, 'template', {
        name: template.name,
        description: template.description,
        tags: [...template.tags],
        category: template.category,
        avatarUrl: template.avatarUrl,
        capabilities: {
          ...template.capabilities,
          skills: available.skills,
          connectors: available.connectors,
          plugins: available.plugins,
          knowledge: available.knowledge,
          starterPrompts: [...template.capabilities.starterPrompts],
        },
      }),
    );
    void webRequest<{
      success?: boolean;
      detail?: string;
      items?: unknown[];
    }>(
      'skills.online_search.search',
      { q: buildTemplateSkillSearchQuery(template), limit: 50 },
      { timeoutMs: 45_000 },
    )
      .then((search) => {
        if (requestId !== skillRecommendationRequest.current) return;
        if (!search.success) {
          setSkillRecommendations({
            status: 'error',
            local: localRecommendations,
            plaza: [],
            error: search.detail || t('jiuwenmate.create.skillSearchFailed'),
          });
          return;
        }
        const plazaRecommendations = recommendMarketplaceTemplateSkills(
          search,
          (resourceCatalog.skills ?? []).map((skill) => skill.name),
        );
        setSkillRecommendations({
          status: plazaRecommendations.length > 0 ? 'ready' : 'error',
          local: localRecommendations,
          plaza: plazaRecommendations,
          error: plazaRecommendations.length > 0 ? null : t('jiuwenmate.create.skillSearchNoResults'),
        });
      })
      .catch((error) => {
        if (requestId !== skillRecommendationRequest.current) return;
        setSkillRecommendations({
          status: 'error',
          local: localRecommendations,
          plaza: [],
          error: errorMessage(error) || t('jiuwenmate.create.skillSearchFailed'),
        });
      });
  };

  const openWorkspace = (colleagueId: string, intent: JiuwenMateWorkspaceIntent | null = null) => {
    setWorkspaceIntent((current) => ({ nonce: current.nonce + 1, intent }));
    showFeature('messages', (current) => openColleagueWorkspace(current, colleagueId));
  };

  const openProfile = (colleagueId: string, section: JiuwenMateProfileSection = 'overview') => {
    setProfileIntent((current) => ({ nonce: current.nonce + 1, section }));
    showFeature('office', (current) => openColleagueProfile(current, colleagueId));
  };

  const setColleagueEnabled = async (colleagueId: string, enabled: boolean) => {
    setActionError('');
    try {
      const colleague = await api.setColleagueEnabled(colleagueId, enabled);
      setState((current) => setColleagueDisabled(current, colleagueId, colleague.disabled));
    } catch (error) {
      setActionError(t('jiuwenmate.load.toggleFailed', { reason: errorMessage(error) }));
    }
  };

  const requestToggleDisabled = (colleagueId: string) => {
    const colleague = state.colleagues.find((item) => item.id === colleagueId);
    if (!colleague) return;
    if (colleague.disabled) {
      void setColleagueEnabled(colleagueId, true);
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
      await setColleagueEnabled(colleague.id, false);
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
      const colleague: JiuwenMateColleague = {
        ...created,
        avatarUrl: created.avatarUrl ?? draft.avatarUrl,
        avatarHue: created.avatarHue ?? draft.avatarHue,
      };
      setProfileIntent((current) => ({ nonce: current.nonce + 1, section: 'overview' }));
      setActiveRailView('office');
      skillRecommendationRequest.current += 1;
      setSkillRecommendations(EMPTY_TEMPLATE_SKILL_RECOMMENDATIONS);
      setMissingTemplateCapabilities(EMPTY_MISSING_CAPABILITIES);
      setState((current) => completeColleagueCreation(current, colleague));
    } catch (error) {
      setActionError(t('jiuwenmate.load.createFailed', { reason: errorMessage(error) }));
    } finally {
      setSubmitting(false);
    }
  };

  const installTemplateSkill = async (candidate: SkillInstallCandidate): Promise<string> => {
    const installParams: Record<string, unknown> = {
      source: candidate.source,
      identifier: candidate.identifier,
      display_name: candidate.display_name,
      ...(candidate.owner_handle ? { owner_handle: candidate.owner_handle } : {}),
    };
    let result = await webRequest<SkillInstallReply>(
      'skills.online_search.install',
      installParams,
      { timeoutMs: 180_000 },
    );
    const startedAt = Date.now();
    while (result.pending && result.install_id) {
      if (Date.now() - startedAt > 15 * 60 * 1000) {
        throw new Error(t('jiuwenmate.create.skillInstallTimeout'));
      }
      await new Promise((resolve) => window.setTimeout(resolve, 800));
      result = await webRequest<SkillInstallReply>(
        'skills.skillnet.install_status',
        { install_id: result.install_id },
        { timeoutMs: 30_000 },
      );
      if (result.status === 'failed') break;
    }
    if (!result.success) throw new Error(result.detail || t('jiuwenmate.create.skillInstallFailed'));

    setResourceCatalogEpoch((current) => current + 1);
    return result.skill?.name?.trim() || candidate.name;
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
  const [boundAppId, setBoundAppId] = useState('');
  const authorizeId = authorizeColleague?.id ?? '';
  const authorizeConfigured = authorizeColleague?.imConfigured === true;

  useEffect(() => {
    if (!authorizeId || !authorizeConfigured) {
      setBoundAppId('');
      return undefined;
    }
    let cancelled = false;
    webRequest<{ config?: { apps?: { app_id?: unknown; persistent_agent_id?: unknown }[] } }>('channel.feishu.get_conf')
      .then((payload) => {
        const match = payload.config?.apps?.find((app) => app.persistent_agent_id === authorizeId);
        const appId = typeof match?.app_id === 'string' ? match.app_id : '';
        if (!cancelled) setBoundAppId(appId);
      })
      .catch(() => {
        if (!cancelled) setBoundAppId('');
      });
    return () => {
      cancelled = true;
    };
  }, [authorizeConfigured, authorizeId]);

  useEffect(() => {
    if (state.view !== 'colleague-profile' && state.view !== 'colleague-workspace') {
      setSchedules([]);
      return;
    }
    const agentIds = state.colleagues.map((item) => item.id).filter(Boolean);
    if (agentIds.length === 0) {
      setSchedules([]);
      return;
    }
    let cancelled = false;
    let refreshing = false;
    const refreshSchedules = () => {
      if (refreshing) return;
      refreshing = true;
      Promise.all(agentIds.map((agentId) => api.listSchedules(agentId)))
        .then((groups) => {
          if (!cancelled) setSchedules(groups.flat());
        })
        .catch((error: unknown) => {
          if (!cancelled) setActionError(errorMessage(error));
        })
        .finally(() => {
          refreshing = false;
        });
    };
    refreshSchedules();
    const refreshTimer = window.setInterval(refreshSchedules, SCHEDULE_REFRESH_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(refreshTimer);
    };
  }, [api, state.view, state.colleagues, connectionEpoch]);

  const failSchedule = (error: unknown) => {
    setActionError(errorMessage(error));
    throw error;
  };

  const createSchedule = (agentId: string, draft: ScheduleDraft) => {
    setSavingSchedule(true);
    setActionError('');
    return api
      .createSchedule(agentId, draft)
      .then((plan) => setSchedules((current) => [...current, plan]))
      .catch(failSchedule)
      .finally(() => setSavingSchedule(false));
  };

  const updateSchedule = (planId: string, draft: ScheduleDraft) => {
    setSavingSchedule(true);
    setActionError('');
    return api
      .updateSchedule(planId, draft)
      .then((plan) => setSchedules((current) => current.map((item) => (item.id === plan.id ? plan : item))))
      .catch(failSchedule)
      .finally(() => setSavingSchedule(false));
  };

  const setScheduleEnabled = (planId: string, enabled: boolean) =>
    api
      .setScheduleEnabled(planId, enabled)
      .then((plan) => setSchedules((current) => current.map((item) => (item.id === plan.id ? plan : item))))
      .catch(failSchedule);

  const removeSchedule = (planId: string) =>
    api
      .deleteSchedule(planId)
      .then(() => setSchedules((current) => current.filter((item) => item.id !== planId)))
      .catch(failSchedule);

  const runSchedule = (planId: string) => api.runSchedule(planId).then(() => undefined).catch(failSchedule);

  const loadScheduleHistory = (planId: string) =>
    Promise.resolve(api.scheduleHistory(planId)).catch(failSchedule);

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
            usedAvatarHues={state.colleagues.flatMap((item) => {
              const hue = customAvatarHue(item);
              return hue === null ? [] : [hue];
            })}
            draft={state.draft}
            modelCatalog={modelCatalog}
            resourceCatalog={resourceCatalog}
            resourceCatalogReady={resourceCatalog.ready}
            missingTemplateCapabilities={missingTemplateCapabilities}
            skillRecommendations={skillRecommendations}
            onDismissSkillRecommendationError={() =>
              setSkillRecommendations((current) => ({ ...current, error: null }))
            }
            onInstallSkill={installTemplateSkill}
            submitting={submitting}
            onOpenTemplateDetail={(id) => setState((current) => selectColleagueTemplate(current, id))}
            onCloseTemplateDetail={() => setState((current) => closeTemplateDetail(current))}
            onChooseTemplate={chooseTemplate}
            onOpenCustom={startCustomCreation}
            onCloseCustom={() => setState((current) => closeCustomColleagueDialog(current))}
            onConfirmCustom={({ name, description, avatarHue }) =>
              setState((current) =>
                startColleagueRefinement(current, 'custom', {
                  name,
                  description,
                  tags: [],
                  category: 'custom',
                  avatarHue,
                  capabilities: emptyCapabilities(),
                }),
              )
            }
            onOpenPlaza={openPlaza}
            onDraftChange={(patch) => setState((current) => updateColleagueDraft(current, patch))}
            onCancel={() => {
              skillRecommendationRequest.current += 1;
              setSkillRecommendations(EMPTY_TEMPLATE_SKILL_RECOMMENDATIONS);
              setMissingTemplateCapabilities(EMPTY_MISSING_CAPABILITIES);
              setState((current) => cancelColleagueCreation(current));
            }}
            onComplete={() => void completeCreation()}
          />
        );
      case 'office-dashboard':
        return (
          <OfficeDashboardPage
            colleagues={state.colleagues}
            activity={activity}
            activityStatus={activityStatus}
            activityError={activityError}
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
            resourceCatalog={resourceCatalog}
            savingResources={savingResources}
            onSaveSettings={(edit) => {
              setSavingResources(true);
              setActionError('');
              const settings = withColleagueEdit(selectedColleague, modelCatalog.models, edit);
              return api
                .updateSettings(selectedColleague.id, settings)
                .then(() => api.publish(selectedColleague.id))
                .then(() => api.getColleague(selectedColleague.id))
                .then((fresh) => {
                  setState((current) => ({
                    ...current,
                    colleagues: current.colleagues.map((item) =>
                      item.id === fresh.id
                        ? { ...fresh, imConfigured: item.imConfigured, disabled: item.disabled }
                        : item,
                    ),
                  }));
                })
                .catch((error: unknown) => {
                  setActionError(errorMessage(error));
                  throw error;
                })
                .finally(() => setSavingResources(false));
            }}
            activity={activity?.members.find((item) => item.agentId === selectedColleague.id) ?? null}
            schedules={schedules.filter((plan) => plan.agentId === selectedColleague.id)}
            savingSchedule={savingSchedule}
            onCreateSchedule={(draft) => createSchedule(selectedColleague.id, draft)}
            onUpdateSchedule={updateSchedule}
            onSetScheduleEnabled={setScheduleEnabled}
            onDeleteSchedule={removeSchedule}
            onRunSchedule={runSchedule}
            onLoadScheduleHistory={loadScheduleHistory}
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
            schedules={schedules}
            savingSchedule={savingSchedule}
            onSaveSchedule={createSchedule}
            onUpdateSchedule={updateSchedule}
            onSetScheduleEnabled={setScheduleEnabled}
            onDeleteSchedule={removeSchedule}
            onLoadScheduleHistory={loadScheduleHistory}
            channelApi={api}
            modelCatalog={modelCatalog}
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
          created={!authorizeConfigured}
          initialAppId={boundAppId}
          onSkip={() => setState((current) => finishColleagueAuthorization(current, false))}
          onConfirm={async ({ appId, appSecret }) => {
            if (!authorizeColleague) return;
            await webRequest('channel.feishu.bind_agent', {
              app_id: appId,
              app_secret: appSecret,
              persistent_agent_id: authorizeColleague.id,
            });
            setState((current) => finishColleagueAuthorization(current, true));
          }}
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
