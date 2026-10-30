import {
  ArrowUp,
  BookOpen,
  CalendarDays,
  ChartLine,
  ChevronDown,
  CirclePause,
  CirclePlay,
  FileText,
  HardDrive,
  Mail,
  Monitor,
  PencilLine,
  Plus,
  Search,
  Trash2,
  UsersRound,
  type LucideIcon,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import feishuIcon from '../../../assets/jiuwenmate/icons/feishu.svg';
import dingtalkIcon from '../../../assets/settings/channels/dingtalk.svg';
import welinkIcon from '../../../assets/jiuwenmate/icons/welink.png';
import autoModelIcon from '../../../assets/jiuwenmate/icons/auto.svg';
import deepseekProviderIcon from '../../../assets/providers/deepseek.svg';
import kimiProviderIcon from '../../../assets/providers/kimi.svg';
import qwenModelIcon from '../../../assets/providers/qwen.svg';
import minimaxModelIcon from '../../../assets/providers/minimax.svg';
import doubaoModelIcon from '../../../assets/providers/doubao.svg';
import openaiModelIcon from '../../../assets/providers/openai.svg';
import anthropicModelIcon from '../../../assets/providers/anthropic.svg';
import zhipuModelIcon from '../../../assets/providers/zhipu.svg';
import baiduModelIcon from '../../../assets/providers/baidu.svg';
import customModelIcon from '../../../assets/settings/models/custom.svg';
import { ConnectorPickerDrawer } from '../../../components/ConnectorMarket/ConnectorPickerDrawer';
import { PageCard } from '../../../components/ui';
import { ActionMenu } from '../components/ActionMenu';
import { JiuwenMateConfirmDialog } from '../components/JiuwenMateDialog';
import { ActivityHeatmap, HeatmapLegend } from '../components/ActivityHeatmap';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import { GroupSharingPermissions } from '../components/GroupSharingPermissions';
import type { GroupSharingApi } from '../groupSharingApi';
import type {
  ColleagueResourceOption,
  JiuwenMateModelCatalog,
  JiuwenMateSchedule,
  JiuwenMateWireSettings,
} from '../jiuwenmateApi';
import type { ScheduleDraft, ScheduleRun } from '../schedules/types';
import { ScheduleSection } from '../schedules/ScheduleSection';
import { JIUWENMATE_WORK_DOMAINS } from '../mockData';
import {
  activeChannelCount,
  formatSummaryTokens,
  groupsInPeriod,
  halfYearHeatmap,
  mergeDailyActivity,
  minutesSince,
  responseAge,
  usageChange,
} from '../officeState';
import type {
  JiuwenMateActivityHistoryItem,
  JiuwenMateActivityKindDay,
  JiuwenMateActivityMember,
  JiuwenMateActivityTaskKind,
  JiuwenMateColleague,
  JiuwenMateConnection,
  JiuwenMateImPlatform,
} from '../types';

export type JiuwenMateProfileSection =
  | 'overview'
  | 'persona'
  | 'model'
  | 'skills'
  | 'connectors'
  | 'plugins'
  | 'knowledge'
  | 'mail'
  | 'channels'
  | 'permissions'
  | 'schedules';

export const PROFILE_SECTIONS: readonly JiuwenMateProfileSection[] = [
  'overview',
  'persona',
  'model',
  'skills',
  'connectors',
  'plugins',
  'knowledge',
  'mail',
  'channels',
  'permissions',
  'schedules',
];

type WorkLogPeriod = '7d' | '1m' | '3m' | 'all';
const WORK_LOG_PERIODS: readonly WorkLogPeriod[] = ['7d', '1m', '3m', 'all'];
const EMPTY_MODEL_CATALOG: JiuwenMateModelCatalog = { models: [], defaultModelId: '', defaultModelLabel: '' };
const DAY_MS = 24 * 60 * 60 * 1000;

export interface ColleagueResourceCatalog {
  skills: readonly ColleagueResourceOption[] | null;
  connectors: readonly ColleagueResourceOption[] | null;
  plugins: readonly ColleagueResourceOption[] | null;
  knowledge: readonly ColleagueResourceOption[] | null;
  ready: boolean;
}

const RESOURCE_SECTIONS = ['skills', 'connectors', 'plugins', 'knowledge'] as const;
type ResourceSection = (typeof RESOURCE_SECTIONS)[number];
const PENCIL_SECTIONS = ['overview'] as const;

export interface ColleagueSettingsEdit {
  groupReplyMode?: 'passive' | 'adaptive';
  permissionProfile?: 'default' | 'automatic' | 'full_access';
  name?: string;
  description?: string;
  persona?: string;
  tags?: string[];
  model?: string;
  skills?: string[];
  connectors?: string[];
  plugins?: string[];
  knowledge?: string[];
}

interface ColleagueProfilePageProps {
  colleague: JiuwenMateColleague;
  modelCatalog?: JiuwenMateModelCatalog;
  initialSection?: JiuwenMateProfileSection;
  today?: Date;
  resourceCatalog?: ColleagueResourceCatalog;
  savingResources?: boolean;
  initialEditing?: boolean;
  onSaveSettings?: (edit: ColleagueSettingsEdit) => void | Promise<void>;
  onLoadSettings?: (agentId: string) => Promise<JiuwenMateWireSettings>;
  schedules?: readonly JiuwenMateSchedule[];
  savingSchedule?: boolean;
  onCreateSchedule?: (draft: ScheduleDraft) => void | Promise<void>;
  onUpdateSchedule?: (planId: string, draft: ScheduleDraft) => void | Promise<void>;
  onSetScheduleEnabled?: (planId: string, enabled: boolean) => void | Promise<void>;
  onDeleteSchedule?: (planId: string) => void | Promise<void>;
  onRunSchedule?: (planId: string) => void | Promise<void>;
  onLoadScheduleHistory?: (planId: string) => Promise<{ plan: JiuwenMateSchedule; runs: ScheduleRun[] }>;
  sharingApi?: GroupSharingApi;
  onLoadAgentMailStatus?: (agentId: string) => Promise<{ status: string; cli_version: string | null; operation_id?: string }>;
  connectionEpoch?: number;
  onBeginAgentMailAuth?: (agentId: string) => Promise<{ status: string; operation_id?: string; auth_url?: string; frontend_opens_auth?: boolean }>;
  onPollAgentMailAuth?: (agentId: string, operationId: string) => Promise<{ status: string; cli_version: string | null }>;
  onCancelAgentMailAuth?: (agentId: string, operationId: string) => Promise<{ status: string; cli_version: string | null }>;
  onDisconnectAgentMail?: (agentId: string) => Promise<{ status: string; cli_version: string | null }>;
  activity?: JiuwenMateActivityMember | null;
  onOpenOffice: () => void;
  onChat: () => void;
  onConfigureIm: (connection?: JiuwenMateConnection, platform?: JiuwenMateImPlatform) => void;
  onRemoveConnection?: (connection: JiuwenMateConnection) => void;
  onConfigureWelink: () => void;
  onRevokeChannel?: (platform: 'feishu' | 'welink') => void | Promise<void>;
  onToggleDisabled: () => void;
  onDelete: () => void;
}

export function formatJoinedAt(joinedAt: string): string {
  const date = new Date(joinedAt);
  if (Number.isNaN(date.getTime())) return joinedAt;
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function joinedDays(joinedAt: string, today: Date): number {
  const joined = new Date(joinedAt);
  if (Number.isNaN(joined.getTime())) return 1;
  const startOfJoined = new Date(joined.getFullYear(), joined.getMonth(), joined.getDate());
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.max(1, Math.round((startOfToday.getTime() - startOfJoined.getTime()) / DAY_MS) + 1);
}

export function ColleagueProfilePage({
  colleague,
  modelCatalog = EMPTY_MODEL_CATALOG,
  initialSection = 'overview',
  today = new Date(),
  resourceCatalog,
  savingResources = false,
  initialEditing = false,
  onSaveSettings,
  onLoadSettings,
  schedules = [],
  savingSchedule = false,
  onCreateSchedule,
  onUpdateSchedule,
  onSetScheduleEnabled,
  onDeleteSchedule,
  onRunSchedule,
  onLoadScheduleHistory,
  sharingApi,
  onLoadAgentMailStatus,
  connectionEpoch = 0,
  onBeginAgentMailAuth,
  onPollAgentMailAuth,
  onCancelAgentMailAuth,
  onDisconnectAgentMail,
  activity = null,
  onOpenOffice,
  onChat,
  onConfigureIm,
  onConfigureWelink,
  onRevokeChannel,
  onRemoveConnection,
  onToggleDisabled,
  onDelete,
}: ColleagueProfilePageProps) {
  const { t } = useTranslation();
  const [section, setSection] = useState<JiuwenMateProfileSection>(initialSection);
  const [editing, setEditing] = useState(initialEditing);
  const [name, setName] = useState(colleague.name);
  const [description, setDescription] = useState(colleague.description);
  const [persona, setPersona] = useState(colleague.persona ?? '');
  const [savedPersona, setSavedPersona] = useState(colleague.persona ?? '');
  const [tags, setTags] = useState(colleague.tags);
  const [model, setModel] = useState(colleague.capabilities.model);
  const [skills, setSkills] = useState(colleague.capabilities.skills);
  const [connectors, setConnectors] = useState(colleague.capabilities.connectors);
  const [plugins, setPlugins] = useState(colleague.capabilities.plugins);
  const [knowledge, setKnowledge] = useState(colleague.capabilities.knowledge);
  const [groupReplyMode, setGroupReplyMode] = useState<'passive' | 'adaptive'>(colleague.groupReplyMode ?? 'adaptive');
  const [permissionProfile, setPermissionProfile] = useState<'default' | 'automatic' | 'full_access'>(
    colleague.permissionProfile ?? 'default',
  );
  const [agentMailStatus, setAgentMailStatus] = useState<string>('loading');
  const [agentMailEmail, setAgentMailEmail] = useState('');
  const [agentMailOperation, setAgentMailOperation] = useState('');
  const [agentMailError, setAgentMailError] = useState('');
  const agentMailAuthWindow = useRef<Window | null>(null);
  const applyAgentMailStatus = (status: { status: string; email?: string }) => {
    setAgentMailStatus(status.status);
    setAgentMailEmail(status.email ?? '');
  };
  const handleAgentMailError = (error: unknown) => {
    setAgentMailError(error instanceof Error ? error.message : String(error));
  };
  const startMailAuthorization = async () => {
    if (!onBeginAgentMailAuth) return;
    setAgentMailError('');
    const authWindow = window.open('about:blank', '_blank');
    if (authWindow) {
      authWindow.document.title = t('jiuwenmate.profile.mail.preparing');
      authWindow.document.body.textContent = t('jiuwenmate.profile.mail.preparing');
    }
    try {
      const authorization = await onBeginAgentMailAuth(colleague.id);
      setAgentMailError('');
      applyAgentMailStatus(authorization);
      if (authorization.status === 'connected') {
        authWindow?.close();
        setAgentMailOperation('');
        return;
      }
      if (!authorization.operation_id) {
        throw new Error('The Agent Mail authorization response is incomplete.');
      }
      if (authorization.frontend_opens_auth && authWindow?.closed) {
        await onCancelAgentMailAuth?.(colleague.id, authorization.operation_id);
        applyAgentMailStatus({ status: 'not_connected' });
        setAgentMailOperation('');
        agentMailAuthWindow.current = null;
        return;
      }
      if (authorization.frontend_opens_auth && !authWindow) {
        await onCancelAgentMailAuth?.(colleague.id, authorization.operation_id);
        applyAgentMailStatus({ status: 'not_connected' });
        setAgentMailError(t('jiuwenmate.profile.mail.popupBlocked'));
        return;
      }
      if (authorization.frontend_opens_auth && authorization.auth_url && authWindow) {
        agentMailAuthWindow.current = authWindow;
        authWindow.location.href = authorization.auth_url;
      } else {
        authWindow?.close();
      }
      setAgentMailOperation(authorization.operation_id);
    } catch (error) {
      authWindow?.close();
      handleAgentMailError(error);
    }
  };
  const disconnectAgentMail = async () => {
    if (!onDisconnectAgentMail) return;
    setAgentMailError('');
    try {
      const result = await onDisconnectAgentMail(colleague.id);
      setAgentMailError('');
      applyAgentMailStatus(result);
    } catch (error) {
      handleAgentMailError(error);
    }
  };
  const cancelAgentMailAuth = async () => {
    if (!onCancelAgentMailAuth || !agentMailOperation) return;
    setAgentMailError('');
    try {
      const result = await onCancelAgentMailAuth(colleague.id, agentMailOperation);
      setAgentMailError('');
      applyAgentMailStatus(result);
      setAgentMailOperation('');
      agentMailAuthWindow.current?.close();
      agentMailAuthWindow.current = null;
    } catch (error) {
      handleAgentMailError(error);
    }
  };
  useEffect(() => {
    setGroupReplyMode(colleague.groupReplyMode ?? 'adaptive');
  }, [colleague.groupReplyMode]);
  useEffect(() => {
    setPermissionProfile(colleague.permissionProfile ?? 'default');
  }, [colleague.permissionProfile]);
  useEffect(() => {
    if (section !== 'mail' || !onLoadAgentMailStatus) return;
    let active = true;
    setAgentMailStatus('loading');
    setAgentMailError('');
    void onLoadAgentMailStatus(colleague.id)
      .then((status) => {
        if (!active) return;
        setAgentMailError('');
        applyAgentMailStatus(status);
        setAgentMailOperation(status.status === 'authorizing' && status.operation_id ? status.operation_id : '');
      })
      .catch((error: unknown) => {
        if (!active) return;
        setAgentMailStatus('error');
        handleAgentMailError(error);
      });
    return () => { active = false; };
  }, [colleague.id, connectionEpoch, onLoadAgentMailStatus, section]);
  useEffect(() => {
    if (!agentMailOperation || !onPollAgentMailAuth) return;
    let active = true;
    const timer = window.setInterval(() => {
      void onPollAgentMailAuth(colleague.id, agentMailOperation)
        .then(async (status) => {
          if (!active) return;
          if (status.status === 'authorizing' && agentMailAuthWindow.current?.closed) {
            const result = onCancelAgentMailAuth
              ? await onCancelAgentMailAuth(colleague.id, agentMailOperation)
              : { status: 'not_connected', cli_version: status.cli_version };
            if (!active) return;
            applyAgentMailStatus(result);
            setAgentMailOperation('');
            agentMailAuthWindow.current = null;
            return;
          }
          setAgentMailError('');
          applyAgentMailStatus(status);
          if (status.status !== 'authorizing') {
            setAgentMailOperation('');
            agentMailAuthWindow.current?.close();
            agentMailAuthWindow.current = null;
          }
        })
        .catch(async (error: unknown) => {
          if (!active) return;
          if (!onLoadAgentMailStatus) {
            handleAgentMailError(error);
            return;
          }
          try {
            const status = await onLoadAgentMailStatus(colleague.id);
            if (!active) return;
            setAgentMailError('');
            applyAgentMailStatus(status);
            setAgentMailOperation(status.status === 'authorizing' && status.operation_id ? status.operation_id : '');
            if (status.status !== 'authorizing') {
              agentMailAuthWindow.current?.close();
              agentMailAuthWindow.current = null;
            }
          } catch {
            if (active) handleAgentMailError(error);
          }
        });
    }, 2500);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [agentMailOperation, colleague.id, onLoadAgentMailStatus, onPollAgentMailAuth]);
  useEffect(() => {
    if (section !== 'persona' || !onLoadSettings) return;
    let active = true;
    void onLoadSettings(colleague.id)
      .then((settings) => {
        if (active) {
          const loadedPersona = settings.persona ?? '';
          setPersona(loadedPersona);
          setSavedPersona(loadedPersona);
        }
      })
      .catch(() => undefined);
    return () => {
      active = false;
    };
  }, [colleague.id, onLoadSettings, section]);
  const showPencil = PENCIL_SECTIONS.some((item) => item === section);
  const saveSettings = (edit: ColleagueSettingsEdit) => {
    void Promise.resolve(onSaveSettings?.(edit))
      .then(() => {
        setEditing(false);
        if (edit.persona !== undefined) setSavedPersona(edit.persona);
      })
      .catch(() => undefined);
  };
  const changeGroupReplyMode = (mode: 'passive' | 'adaptive') => {
    const previous = groupReplyMode;
    setGroupReplyMode(mode);
    void Promise.resolve(onSaveSettings?.({ groupReplyMode: mode })).catch(() => setGroupReplyMode(previous));
  };
  const changePermissionProfile = (profile: 'default' | 'automatic' | 'full_access') => {
    if (profile === permissionProfile || savingResources || !onSaveSettings) return;
    if (profile === 'full_access' && !window.confirm(t('jiuwenmate.profile.permissions.fullAccessConfirm'))) return;
    const previous = permissionProfile;
    setPermissionProfile(profile);
    void Promise.resolve(onSaveSettings({ permissionProfile: profile })).catch(() => setPermissionProfile(previous));
  };
  const openSection = (next: JiuwenMateProfileSection) => {
    setSection(next);
    setEditing(false);
  };
  const [revokePlatform, setRevokePlatform] = useState<'feishu' | 'welink' | null>(null);
  const [revoking, setRevoking] = useState(false);
  const confirmRevoke = () => {
    if (!revokePlatform || revoking) return;
    const platform = revokePlatform;
    setRevoking(true);
    void Promise.resolve(onRevokeChannel?.(platform))
      .then(() => setRevokePlatform(null))
      .catch(() => undefined)
      .finally(() => setRevoking(false));
  };

  return (
    <section className="jiuwenmate-page jiuwenmate-profile" data-testid="jiuwenmate-colleague-profile">
      <nav className="jiuwenmate-breadcrumb" aria-label={t('jiuwenmate.create.crumbLabel')}>
        <button type="button" onClick={onOpenOffice}>
          {t('jiuwenmate.nav.office')}
        </button>
        <span aria-hidden>/</span>
        <strong>{colleague.name}</strong>
      </nav>
      <div className="jiuwenmate-profile__frame">
        <nav className="jiuwenmate-profile__subnav" aria-label={t('jiuwenmate.profile.sectionsLabel')}>
          {PROFILE_SECTIONS.map((item) => (
            <button
              key={item}
              type="button"
              aria-current={section === item ? 'true' : undefined}
              onClick={() => openSection(item)}
              data-testid="jiuwenmate-profile-section"
              data-variant={item}
            >
              {t(`jiuwenmate.profile.sections.${item}`)}
            </button>
          ))}
        </nav>
        <div className="jiuwenmate-profile__content" data-section={section}>
          <header className="jiuwenmate-profile__header">
            <ColleagueAvatar colleague={colleague} size="large" />
            <h1>{colleague.name}</h1>
            {showPencil ? (
              <button
                type="button"
                className="jiuwenmate-icon-button"
                aria-label={t(section === 'model' ? 'jiuwenmate.profile.editModel' : 'jiuwenmate.profile.edit')}
                aria-pressed={editing}
                onClick={() => setEditing((current) => !current)}
                data-testid="jiuwenmate-profile-edit"
              >
                <PencilLine aria-hidden />
              </button>
            ) : null}
            {colleague.disabled ? <em className="jiuwenmate-badge" data-tone="muted">{t('jiuwenmate.office.disabledBadge')}</em> : null}
            <span className="jiuwenmate-profile__header-actions">
              <ActionMenu
                label={t('jiuwenmate.workspace.more')}
                items={[
                  {
                    key: 'toggle',
                    label: colleague.disabled ? t('jiuwenmate.office.enable') : t('jiuwenmate.office.disable'),
                    icon: colleague.disabled ? CirclePlay : CirclePause,
                    onSelect: onToggleDisabled,
                  },
                  { key: 'delete', label: t('jiuwenmate.office.delete'), icon: Trash2, onSelect: onDelete },
                ]}
              />
              <button type="button" className="jiuwenmate-dark-button" onClick={() => onChat()} data-testid="jiuwenmate-profile-chat">
                {t('jiuwenmate.office.chat')}
              </button>
            </span>
          </header>
          {section === 'overview' ? (
            <>
              {editing ? (
                <IdentityEditor
                  name={name}
                  description={description}
                  tags={tags}
                  saving={savingResources}
                  onNameChange={setName}
                  onDescriptionChange={setDescription}
                  onTagsChange={setTags}
                  onSave={() => saveSettings({ name: name.trim(), description: description.trim(), tags: [...tags] })}
                />
              ) : null}
              <ProfileOverview
                colleague={colleague}
                activity={activity}
                today={today}
                onConfigureIm={(platform) => onConfigureIm(undefined, platform)}
                onConfigureWelink={onConfigureWelink}
                onRevokeChannel={setRevokePlatform}
                revoking={revoking}
              />
            </>
          ) : (
            <ProfileSectionDetail
              colleague={colleague}
              sharingApi={sharingApi}
              connectionEpoch={connectionEpoch}
              groupReplyMode={groupReplyMode}
              permissionProfile={permissionProfile}
              modelCatalog={modelCatalog}
              section={section}
              persona={persona}
              savedPersona={savedPersona}
              onPersonaChange={setPersona}
              model={model}
              skills={skills}
              connectors={connectors}
              plugins={plugins}
              knowledge={knowledge}
              resourceCatalog={resourceCatalog}
              savingResources={savingResources}
              onModelChange={setModel}
              onSkillsChange={setSkills}
              onConnectorsChange={setConnectors}
              onPluginsChange={setPlugins}
              onKnowledgeChange={setKnowledge}
              onSaveSettings={saveSettings}
              onGroupReplyModeChange={changeGroupReplyMode}
              onPermissionProfileChange={changePermissionProfile}
              onConfigureIm={onConfigureIm}
              onConfigureWelink={onConfigureWelink}
              onRevokeChannel={setRevokePlatform}
              revoking={revoking}
              onRemoveConnection={onRemoveConnection}
              schedules={schedules}
              savingSchedule={savingSchedule}
              onCreateSchedule={onCreateSchedule}
              onUpdateSchedule={onUpdateSchedule}
              onSetScheduleEnabled={onSetScheduleEnabled}
              onDeleteSchedule={onDeleteSchedule}
              onRunSchedule={onRunSchedule}
              onLoadScheduleHistory={onLoadScheduleHistory}
              agentMailStatus={agentMailStatus}
              agentMailEmail={agentMailEmail}
              agentMailError={agentMailError}
              onStartAgentMailAuth={startMailAuthorization}
              onDisconnectAgentMail={disconnectAgentMail}
              onCancelAgentMailAuth={cancelAgentMailAuth}
            />
          )}
        </div>
      </div>
      {revokePlatform ? (
        <JiuwenMateConfirmDialog
          title={t('jiuwenmate.profile.cancelAuthorizationTitle')}
          warning={t('jiuwenmate.profile.cancelAuthorizationWarning')}
          message={t('jiuwenmate.profile.cancelAuthorizationMessage', {
            channel: t(revokePlatform === 'welink' ? 'jiuwenmate.im.welink' : 'jiuwenmate.im.feishu'),
          })}
          onCancel={() => {
            if (!revoking) setRevokePlatform(null);
          }}
          onConfirm={confirmRevoke}
        />
      ) : null}
    </section>
  );
}

function halfBounds(year: number, half: 1 | 2): { start: string; end: string } {
  return half === 1
    ? { start: `${year}-01-01`, end: `${year}-06-30` }
    : { start: `${year}-07-01`, end: `${year}-12-31` };
}

function tasksInHalf(
  kinds: readonly JiuwenMateActivityKindDay[],
  year: number,
  half: 1 | 2,
): Record<JiuwenMateActivityTaskKind, number> {
  const bounds = halfBounds(year, half);
  const totals: Record<JiuwenMateActivityTaskKind, number> = { conversation: 0, channel: 0, schedule: 0 };
  for (const day of kinds) {
    if (day.date < bounds.start || day.date > bounds.end) continue;
    totals.conversation += day.conversation;
    totals.channel += day.channel;
    totals.schedule += day.schedule;
  }
  return totals;
}

function channelLabel(t: (key: string) => string, channel: string): string {
  if (!channel) return '—';
  const key = `cron.targets.${channel}`;
  const label = t(key);
  return label === key ? channel : label;
}

function ImBindingAction({
  platform,
  configured,
  revoking = false,
  onConfigure,
  onRevoke,
}: {
  platform: 'feishu' | 'dingtalk' | 'welink';
  configured: boolean;
  revoking?: boolean;
  onConfigure: () => void;
  onRevoke?: (platform: 'feishu' | 'welink') => void;
}) {
  const { t } = useTranslation();
  return !configured ? (
    <button type="button" className="jiuwenmate-link-button jiuwenmate-link-button--blue" onClick={onConfigure}>
      {t('jiuwenmate.profile.authorize')}
    </button>
  ) : (
    <div className="jiuwenmate-profile__im-actions">
      <em className="jiuwenmate-badge" data-tone="success">{t('jiuwenmate.profile.authorized')}</em>
      <button
        type="button"
        className="jiuwenmate-link-button jiuwenmate-link-button--blue"
        onClick={onConfigure}
        data-testid="jiuwenmate-profile-im-modify"
      >
        {t('common.modify')}
      </button>
      {onRevoke && platform === 'welink' ? (
        <button
          type="button"
          className="jiuwenmate-link-button jiuwenmate-link-button--danger"
          onClick={() => onRevoke(platform)}
          disabled={revoking}
          data-testid="jiuwenmate-profile-im-revoke"
          data-variant={platform}
        >
          {t('jiuwenmate.profile.cancelAuthorization')}
        </button>
      ) : null}
    </div>
  );
}

function historyStatusLabel(t: (key: string) => string, status: string): string {
  if (!status) return '—';
  const key = `jiuwenmate.profile.historyStatus.${status}`;
  const label = t(key);
  return label === key ? status : label;
}

function lastResponseText(t: (key: string, options?: Record<string, unknown>) => string, iso: string | null, today: Date): string {
  const minutes = minutesSince(iso, today);
  if (minutes === null) return t('jiuwenmate.office.noResponse');
  const age = responseAge(minutes);
  return t(`jiuwenmate.office.${age.unit}Ago`, { count: age.count });
}

function ProfileOverview({
  colleague,
  activity,
  today,
  onConfigureIm,
  onConfigureWelink,
}: {
  colleague: JiuwenMateColleague;
  activity: JiuwenMateActivityMember | null;
  today: Date;
  onConfigureIm: (platform: JiuwenMateImPlatform) => void;
  onConfigureWelink: () => void;
  onRevokeChannel?: (platform: 'feishu' | 'welink') => void;
  revoking?: boolean;
}) {
  const { t } = useTranslation();
  const [period, setPeriod] = useState<WorkLogPeriod>('7d');
  const [half, setHalf] = useState<1 | 2>(today.getMonth() < 6 ? 1 : 2);
  const [hoveredCellId, setHoveredCellId] = useState<string | null>(null);
  const year = today.getFullYear();
  const days = activity?.days ?? [];
  const usage = usageChange(days, period, today);
  const percent = usage.percent === null ? '—' : `${usage.percent}%`;
  const daily = useMemo(() => mergeDailyActivity(activity ? [activity] : []), [activity]);
  const heatmap = useMemo(() => halfYearHeatmap(year, half, daily), [year, half, daily]);
  const taskKinds = tasksInHalf(activity?.kinds ?? [], year, half);
  const taskTotal = taskKinds.conversation + taskKinds.channel + taskKinds.schedule;
  const configuredChannels = new Set(colleague.connections.map((connection) => connection.channel));
  const feishuConfigured = configuredChannels.has('feishu')
    || colleague.feishuConfigured === true
    || (colleague.imConfigured && colleague.imPlatform === 'feishu');
  const dingtalkConfigured = configuredChannels.has('dingtalk')
    || colleague.dingtalkConfigured === true
    || (colleague.imConfigured && colleague.imPlatform === 'dingtalk');
  const welinkConfigured = colleague.welinkConfigured === true;

  const metrics: { key: string; value: ReactNode; icon: LucideIcon }[] = [
    {
      key: 'weeklyUsage',
      value: (
        <>
          {percent}
          {usage.percent !== null && usage.percent > 0 ? <ArrowUp aria-hidden className="jiuwenmate-trend-up" /> : null}
        </>
      ),
      icon: ChartLine,
    },
    {
      key: 'resources',
      value: (
        <>
          {formatSummaryTokens(usage.tokens)}
          <small>tokens</small>
        </>
      ),
      icon: HardDrive,
    },
    { key: 'joinedDays', value: activity?.companionDays ?? joinedDays(colleague.joinedAt, today), icon: CalendarDays },
    { key: 'channels', value: activity ? activeChannelCount(activity) : 0, icon: Monitor },
    { key: 'groupChats', value: groupsInPeriod(activity?.groups ?? [], period, today), icon: UsersRound },
  ];

  return (
    <>
      <p className="jiuwenmate-profile__description">{colleague.description}</p>
      <p className="jiuwenmate-profile__joined">
        {t('jiuwenmate.profile.joinedAt', { time: formatJoinedAt(colleague.joinedAt) })}
      </p>
      {!feishuConfigured || !dingtalkConfigured || !welinkConfigured ? (
        <section className="jiuwenmate-profile__im" data-testid="jiuwenmate-profile-quick-im">
          <header>
            <strong>{t('jiuwenmate.profile.quickIm')}</strong>
          </header>
          {!feishuConfigured || !dingtalkConfigured ? (
            <div className="jiuwenmate-profile__quick-im-platforms">
              {!feishuConfigured ? (
                <div className="jiuwenmate-profile__im-row">
                  <img src={feishuIcon} alt="" aria-hidden />
                  <span>
                    <strong>{t('jiuwenmate.im.feishu')}</strong>
                    <small>{t('jiuwenmate.im.isolationHint')}</small>
                  </span>
                  <ImBindingAction platform="feishu" configured={false} onConfigure={() => onConfigureIm('feishu')} />
                </div>
              ) : null}
              {!dingtalkConfigured ? (
                <div className="jiuwenmate-profile__im-row">
                  <img src={dingtalkIcon} alt="" aria-hidden />
                  <span>
                    <strong>{t('jiuwenmate.im.dingtalk')}</strong>
                    <small>{t('jiuwenmate.im.isolationHint')}</small>
                  </span>
                  <ImBindingAction platform="dingtalk" configured={false} onConfigure={() => onConfigureIm('dingtalk')} />
                </div>
              ) : null}
            </div>
          ) : null}
          {!welinkConfigured ? (
            <div className="jiuwenmate-profile__im-row">
              <img className="jiuwenmate-welink-logo" src={welinkIcon} alt="" aria-hidden />
              <span>
                <strong>{t('jiuwenmate.im.welink')}</strong>
                <small>{t('jiuwenmate.profile.welinkHint')}</small>
              </span>
              <ImBindingAction platform="welink" configured={false} onConfigure={onConfigureWelink} />
            </div>
          ) : null}
        </section>
      ) : null}

      <section className="jiuwenmate-profile__block">
        <header>
          <h2>{t('jiuwenmate.profile.workLog')}</h2>
          <ActionMenu
            label={t('jiuwenmate.profile.workLog')}
            className="jiuwenmate-select-menu"
            trigger={
              <>
                <span>{t(`jiuwenmate.profile.periods.${period}`)}</span>
                <ChevronDown aria-hidden />
              </>
            }
            items={WORK_LOG_PERIODS.map((item) => ({
              key: item,
              label: t(`jiuwenmate.profile.periods.${item}`),
              onSelect: () => setPeriod(item),
            }))}
          />
        </header>
        <div className="jiuwenmate-metric-strip" data-testid="jiuwenmate-profile-metrics">
          {metrics.map(({ key, value, icon: Icon }) => (
            <div key={key}>
              <strong>{value}</strong>
              <span>
                <Icon aria-hidden />
                {t(`jiuwenmate.profile.metrics.${key}`)}
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="jiuwenmate-profile__block jiuwenmate-profile__block--card">
        <header>
          <h2>{t('jiuwenmate.profile.periodRecord')}</h2>
          <ActionMenu
            label={t('jiuwenmate.profile.periodRecord')}
            className="jiuwenmate-select-menu"
            trigger={
              <>
                <span>{t(half === 2 ? 'jiuwenmate.profile.secondHalf' : 'jiuwenmate.profile.firstHalf', { year })}</span>
                <ChevronDown aria-hidden />
              </>
            }
            items={[
              { key: 'second', label: t('jiuwenmate.profile.secondHalf', { year }), onSelect: () => setHalf(2) },
              { key: 'first', label: t('jiuwenmate.profile.firstHalf', { year }), onSelect: () => setHalf(1) },
            ]}
          />
        </header>
        <div className="jiuwenmate-profile__heat">
          <div>
            <ActivityHeatmap
              heatmap={heatmap}
              hoveredCellId={hoveredCellId}
              onHover={setHoveredCellId}
            />
            <HeatmapLegend />
          </div>
          <div className="jiuwenmate-task-ring">
            <span className="jiuwenmate-task-ring__circle" data-testid="jiuwenmate-profile-task-count">
              <strong>{taskTotal}</strong>
              <small>{t('jiuwenmate.profile.taskCount')}</small>
            </span>
            <dl>
              <dt>{t('jiuwenmate.profile.taskTypes')}</dt>
              {(['conversation', 'channel', 'schedule'] as const).map((kind) => (
                <dd key={kind} data-kind={kind}>
                  <i />
                  <span>{t(`jiuwenmate.profile.taskKinds.${kind}`)}</span>
                  <strong>{taskKinds[kind]}</strong>
                </dd>
              ))}
            </dl>
          </div>
        </div>
      </section>

      <ExecutionHistory history={activity?.history ?? []} today={today} />
    </>
  );
}

function ExecutionHistory({ history, today }: { history: readonly JiuwenMateActivityHistoryItem[]; today: Date }) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const [channel, setChannel] = useState('');
  const [status, setStatus] = useState('');
  const columns = ['task', 'channel', 'status', 'tokens', 'lastResponse', 'actions'] as const;
  const channels = [...new Set(history.map((item) => item.channel).filter(Boolean))];
  const statuses = [...new Set(history.map((item) => item.status).filter(Boolean))];
  const keyword = query.trim();
  const rows = history.filter((item) => {
    if (channel && item.channel !== channel) return false;
    if (status && item.status !== status) return false;
    if (!keyword) return true;
    const title = item.title || t(`jiuwenmate.profile.taskKinds.${item.kind}`);
    return title.includes(keyword);
  });
  return (
    <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-history">
      <header>
        <h2>{t('jiuwenmate.profile.history')}</h2>
        <span className="jiuwenmate-profile__filters">
          <ActionMenu
            label={t('jiuwenmate.profile.allChannels')}
            className="jiuwenmate-select-menu"
            trigger={
              <>
                <span>{channel ? channelLabel(t, channel) : t('jiuwenmate.profile.allChannels')}</span>
                <ChevronDown aria-hidden />
              </>
            }
            items={[
              { key: 'all', label: t('jiuwenmate.profile.allChannels'), onSelect: () => setChannel('') },
              ...channels.map((item) => ({
                key: item,
                label: channelLabel(t, item),
                onSelect: () => setChannel(item),
              })),
            ]}
          />
          <ActionMenu
            label={t('jiuwenmate.profile.allStatus')}
            className="jiuwenmate-select-menu"
            trigger={
              <>
                <span>{status ? historyStatusLabel(t, status) : t('jiuwenmate.profile.allStatus')}</span>
                <ChevronDown aria-hidden />
              </>
            }
            items={[
              { key: 'all', label: t('jiuwenmate.profile.allStatus'), onSelect: () => setStatus('') },
              ...statuses.map((item) => ({
                key: item,
                label: historyStatusLabel(t, item),
                onSelect: () => setStatus(item),
              })),
            ]}
          />
          <label className="jiuwenmate-search">
            <Search aria-hidden />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t('jiuwenmate.common.searchPlaceholder')}
              aria-label={t('jiuwenmate.common.searchPlaceholder')}
            />
          </label>
        </span>
      </header>
      <table className="jiuwenmate-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{t(`jiuwenmate.profile.historyColumns.${column}`)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="jiuwenmate-table__empty">
                {t('jiuwenmate.profile.historyEmpty')}
              </td>
            </tr>
          ) : (
            rows.map((item) => (
              <tr key={item.requestId} data-testid="jiuwenmate-profile-history-row">
                <td>{item.title || t(`jiuwenmate.profile.taskKinds.${item.kind}`)}</td>
                <td>{channelLabel(t, item.channel)}</td>
                <td>{historyStatusLabel(t, item.status)}</td>
                <td>{formatSummaryTokens(item.tokens)}</td>
                <td>{lastResponseText(t, item.updatedAt, today)}</td>
                <td />
              </tr>
            ))
          )}
        </tbody>
      </table>
    </section>
  );
}

function sameNames(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

function IdentityEditor({
  name,
  description,
  tags,
  saving,
  onNameChange,
  onDescriptionChange,
  onTagsChange,
  onSave,
}: {
  name: string;
  description: string;
  tags: readonly string[];
  saving: boolean;
  onNameChange: (value: string) => void;
  onDescriptionChange: (value: string) => void;
  onTagsChange: (value: string[]) => void;
  onSave: () => void;
}) {
  const { t } = useTranslation();
  const domains = [...new Set([...JIUWENMATE_WORK_DOMAINS, ...tags])];
  return (
    <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-identity-editor">
      <label className="jiuwenmate-field">
        <span>{t('jiuwenmate.create.name')}</span>
        <input value={name} onChange={(event) => onNameChange(event.target.value)} data-testid="jiuwenmate-profile-name-input" />
      </label>
      <label className="jiuwenmate-field">
        <span>{t('jiuwenmate.create.description')}</span>
        <textarea value={description} rows={3} onChange={(event) => onDescriptionChange(event.target.value)} />
      </label>
      <div className="jiuwenmate-field">
        <span>{t('jiuwenmate.create.role')}</span>
        <div className="jiuwenmate-capability-grid">
          {domains.map((domain) => (
            <button
              key={domain}
              type="button"
              className="jiuwenmate-capability-chip"
              aria-pressed={tags.includes(domain)}
              onClick={() => onTagsChange(tags.includes(domain) ? tags.filter((item) => item !== domain) : [...tags, domain])}
            >
              {domain}
            </button>
          ))}
        </div>
      </div>
      <div className="jiuwenmate-profile__resource-actions">
        <button type="button" className="jiuwenmate-dark-button" disabled={saving || name.trim().length === 0} onClick={onSave}>
          {saving ? t('jiuwenmate.profile.savingResources') : t('jiuwenmate.profile.saveResources')}
        </button>
      </div>
    </section>
  );
}

function ProfileSectionDetail({
  colleague,
  sharingApi,
  connectionEpoch,
  groupReplyMode,
  permissionProfile,
  modelCatalog,
  section,
  persona,
  savedPersona,
  model,
  skills,
  connectors,
  plugins,
  knowledge,
  resourceCatalog,
  savingResources,
  onPersonaChange,
  onModelChange,
  onSkillsChange,
  onConnectorsChange,
  onPluginsChange,
  onKnowledgeChange,
  onSaveSettings,
  onGroupReplyModeChange,
  onPermissionProfileChange,
  onConfigureIm,
  onRemoveConnection,
  onConfigureWelink,
  onRevokeChannel,
  revoking = false,
  schedules,
  savingSchedule,
  onCreateSchedule,
  onUpdateSchedule,
  onSetScheduleEnabled,
  onDeleteSchedule,
  onRunSchedule,
  onLoadScheduleHistory,
  agentMailStatus,
  agentMailEmail,
  agentMailError,
  onStartAgentMailAuth,
  onDisconnectAgentMail,
  onCancelAgentMailAuth,
}: {
  colleague: JiuwenMateColleague;
  sharingApi?: GroupSharingApi;
  connectionEpoch: number;
  groupReplyMode: 'passive' | 'adaptive';
  permissionProfile: 'default' | 'automatic' | 'full_access';
  modelCatalog: JiuwenMateModelCatalog;
  section: Exclude<JiuwenMateProfileSection, 'overview'>;
  persona: string;
  savedPersona: string;
  model: string;
  skills: readonly string[];
  connectors: readonly string[];
  plugins: readonly string[];
  knowledge: readonly string[];
  resourceCatalog?: ColleagueResourceCatalog;
  savingResources: boolean;
  onPersonaChange: (value: string) => void;
  onModelChange: (value: string) => void;
  onSkillsChange: (items: string[]) => void;
  onConnectorsChange: (items: string[]) => void;
  onPluginsChange: (items: string[]) => void;
  onKnowledgeChange: (items: string[]) => void;
  onSaveSettings?: (edit: ColleagueSettingsEdit) => void | Promise<void>;
  onGroupReplyModeChange: (mode: 'passive' | 'adaptive') => void;
  onPermissionProfileChange: (profile: 'default' | 'automatic' | 'full_access') => void;
  onConfigureIm: (connection?: JiuwenMateConnection, platform?: JiuwenMateImPlatform) => void;
  onRemoveConnection?: (connection: JiuwenMateConnection) => void;
  onConfigureWelink: () => void;
  onRevokeChannel?: (platform: 'feishu' | 'welink') => void;
  revoking?: boolean;
  schedules: readonly JiuwenMateSchedule[];
  savingSchedule: boolean;
  onCreateSchedule?: (draft: ScheduleDraft) => void | Promise<void>;
  onUpdateSchedule?: (planId: string, draft: ScheduleDraft) => void | Promise<void>;
  onSetScheduleEnabled?: (planId: string, enabled: boolean) => void | Promise<void>;
  onDeleteSchedule?: (planId: string) => void | Promise<void>;
  onRunSchedule?: (planId: string) => void | Promise<void>;
  onLoadScheduleHistory?: (planId: string) => Promise<{ plan: JiuwenMateSchedule; runs: ScheduleRun[] }>;
  agentMailStatus: string;
  agentMailEmail: string;
  agentMailError: string;
  onStartAgentMailAuth: () => void;
  onDisconnectAgentMail: () => void;
  onCancelAgentMailAuth: () => void;
}) {
  const { t } = useTranslation();
  const [resourcePickerOpen, setResourcePickerOpen] = useState(false);
  const title = section === 'model' ? t('jiuwenmate.profile.modelSelection') : t(`jiuwenmate.profile.sections.${section}`);
  const modelIcon = (provider: string, id: string) => {
    const key = `${provider} ${id}`.toLowerCase();
    if (key.includes('auto')) return autoModelIcon;
    if (key.includes('kimi') || key.includes('moonshot')) return kimiProviderIcon;
    if (key.includes('deepseek')) return deepseekProviderIcon;
    if (key.includes('qwen') || key.includes('通义')) return qwenModelIcon;
    if (key.includes('minimax') || key.includes('abab')) return minimaxModelIcon;
    if (key.includes('doubao') || key.includes('volcengine') || key.includes('豆包')) return doubaoModelIcon;
    if (key.includes('openai') || key.includes('gpt')) return openaiModelIcon;
    if (key.includes('anthropic') || key.includes('claude')) return anthropicModelIcon;
    if (key.includes('zhipu') || key.includes('智谱') || key.includes('glm')) return zhipuModelIcon;
    if (key.includes('baidu') || key.includes('文心')) return baiduModelIcon;
    return customModelIcon;
  };

  useEffect(() => {
    setResourcePickerOpen(false);
  }, [section]);

  if (section === 'persona') {
    const dirty = persona !== savedPersona;
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <label className="jiuwenmate-profile__persona-editor">
          <span className="jiuwenmate-sr-only">{t('jiuwenmate.create.description')}</span>
          <textarea value={persona} onChange={(event) => onPersonaChange(event.target.value)} />
        </label>
        {dirty ? (
          <div className="jiuwenmate-profile__resource-actions">
            <button
              type="button"
              className="jiuwenmate-dark-button jiuwenmate-profile__save-button"
              disabled={!dirty || savingResources}
              onClick={() => onSaveSettings?.({ persona: persona.trim() })}
            >
              {savingResources ? t('jiuwenmate.profile.savingResources') : t('jiuwenmate.profile.saveResources')}
            </button>
          </div>
        ) : null}
      </section>
    );
  }

  if (section === 'model') {
    const selectedModelId = model || modelCatalog.defaultModelId;
    const choices = modelCatalog.models.some((item) => item.id === model) || model.length === 0
      ? modelCatalog.models
      : [{ id: model, label: model, provider: '' }, ...modelCatalog.models];
    const dirty = model !== colleague.capabilities.model;
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <div className="jiuwenmate-profile__model-list" role="radiogroup" aria-label={title}>
          {choices.map((choice) => (
            <label key={choice.id} className="jiuwenmate-profile__model-option" data-selected={selectedModelId === choice.id}>
              <input
                type="radio"
                name="jiuwenmate-profile-model"
                value={choice.id}
                checked={selectedModelId === choice.id}
                onChange={() => onModelChange(choice.id)}
              />
              <img
                className="jiuwenmate-profile__model-icon"
                src={modelIcon(choice.provider, choice.id)}
                alt=""
                aria-hidden
              />
              <span className="jiuwenmate-profile__model-option-copy">
                <strong>{choice.label}</strong>
                {choice.provider ? <small>{choice.provider}</small> : null}
              </span>
            </label>
          ))}
        </div>
        <div className="jiuwenmate-profile__resource-actions">
          <button
            type="button"
            className="jiuwenmate-dark-button jiuwenmate-profile__save-button"
            disabled={!dirty || savingResources || model.length === 0}
            onClick={() => onSaveSettings?.({ model })}
          >
            {savingResources ? t('jiuwenmate.profile.savingResources') : t('jiuwenmate.profile.saveResources')}
          </button>
        </div>
      </section>
    );
  }

  if (section === 'channels') {
    const imConnections = colleague.connections.filter((item) => item.channel !== 'web');
    const connectionsByPlatform: Record<JiuwenMateImPlatform, JiuwenMateConnection[]> = {
      feishu: imConnections.filter((item) => item.channel === 'feishu'),
      dingtalk: imConnections.filter((item) => item.channel === 'dingtalk'),
    };
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <div className="jiuwenmate-profile__reply-mode" data-testid="jiuwenmate-profile-group-reply-mode">
          <div className="jiuwenmate-profile__reply-mode-heading">
            <strong data-testid="jiuwenmate-profile-group-reply-title">{t('jiuwenmate.profile.groupReplyMode')}</strong>
          </div>
          <div className="jiuwenmate-profile__reply-mode-options" role="radiogroup" aria-label={t('jiuwenmate.profile.groupReplyMode')}>
            {(['passive', 'adaptive'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                className="jiuwenmate-profile__reply-mode-option"
                aria-pressed={groupReplyMode === mode}
                disabled={savingResources || !onSaveSettings}
                data-testid="jiuwenmate-profile-group-reply-option"
                data-variant={mode}
                onClick={() => onGroupReplyModeChange(mode)}
              >
                <span className="jiuwenmate-profile__reply-mode-copy">
                  <strong data-testid="jiuwenmate-profile-group-reply-label" data-variant={mode}>
                    {t(`jiuwenmate.profile.groupReplyModes.${mode}`)}
                  </strong>
                  <small data-testid="jiuwenmate-profile-group-reply-hint" data-variant={mode}>
                    {t(`jiuwenmate.profile.groupReplyModes.${mode}Hint`)}
                  </small>
                </span>
              </button>
            ))}
          </div>
        </div>
        <div className="jiuwenmate-profile__im-platform-grid" data-testid="jiuwenmate-profile-im-platforms">
          {(['feishu', 'dingtalk'] as const).map((platform) => {
            const platformConnections = connectionsByPlatform[platform];
            return (
              <section
                key={platform}
                className="jiuwenmate-profile__im-platform"
                data-testid="jiuwenmate-profile-im-platform"
                data-variant={platform}
              >
                <header>
                  <span className="jiuwenmate-profile__im-platform-title">
                    <img src={platform === 'dingtalk' ? dingtalkIcon : feishuIcon} alt="" aria-hidden />
                    <strong>{t(`jiuwenmate.im.${platform}`)}</strong>
                  </span>
                  <button
                    type="button"
                    className="jiuwenmate-link-button jiuwenmate-link-button--blue"
                    onClick={() => onConfigureIm(undefined, platform)}
                    data-testid={platformConnections.length ? 'jiuwenmate-profile-im-add' : 'jiuwenmate-profile-im-authorize'}
                  >
                    {t(platformConnections.length ? 'jiuwenmate.profile.addConnection' : 'jiuwenmate.profile.authorize')}
                  </button>
                </header>
                {platformConnections.length > 0 ? (
                  <ul className="jiuwenmate-profile__connections" data-testid="jiuwenmate-profile-connections">
                    {platformConnections.map((connection) => (
                      <li
                        key={connection.agentId}
                        className="jiuwenmate-profile__im-platform-connection"
                        data-testid="jiuwenmate-profile-connection"
                        data-variant={connection.connectionKey}
                      >
                        <small data-testid="jiuwenmate-profile-connection-app">{connection.connectionKey}</small>
                        <div className="jiuwenmate-profile__im-actions">
                          <em className="jiuwenmate-badge" data-tone="success">{t('jiuwenmate.profile.authorized')}</em>
                          <button
                            type="button"
                            className="jiuwenmate-link-button jiuwenmate-link-button--blue"
                            onClick={() => onConfigureIm(connection)}
                            data-testid="jiuwenmate-profile-im-modify"
                          >
                            {t('common.modify')}
                          </button>
                          {onRemoveConnection ? (
                            <button
                              type="button"
                              className="jiuwenmate-link-button"
                              onClick={() => onRemoveConnection(connection)}
                              data-testid="jiuwenmate-profile-im-remove"
                            >
                              {t('jiuwenmate.profile.removeConnection')}
                            </button>
                          ) : null}
                        </div>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </section>
            );
          })}
        </div>
        <small className="jiuwenmate-profile__connections-hint" data-testid="jiuwenmate-profile-im-isolation-hint">
          {t('jiuwenmate.im.isolationHint')}
        </small>
        <div className="jiuwenmate-profile__im-row jiuwenmate-profile__im-row--bordered">
          <img className="jiuwenmate-welink-logo" src={welinkIcon} alt="" aria-hidden />
          <span>
            <strong>{t('jiuwenmate.im.welink')}</strong>
            <small>{t('jiuwenmate.profile.welinkHint')}</small>
          </span>
          <ImBindingAction
            platform="welink"
            configured={colleague.welinkConfigured === true}
            revoking={revoking}
            onConfigure={onConfigureWelink}
            onRevoke={onRevokeChannel}
          />
        </div>
      </section>
    );
  }

  if (section === 'permissions') {
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <div className="jiuwenmate-profile__reply-mode" data-testid="jiuwenmate-profile-permissions">
          <div className="jiuwenmate-profile__reply-mode-heading">
            <strong>{t('jiuwenmate.profile.permissions.title')}</strong>
            <p>{t('jiuwenmate.profile.permissions.description')}</p>
          </div>
          <div className="jiuwenmate-profile__reply-mode-options jiuwenmate-profile__reply-mode-options--permissions" role="radiogroup" aria-label={t('jiuwenmate.profile.permissions.title')}>
            {(['default', 'automatic', 'full_access'] as const).map((profile) => (
              <button
                key={profile}
                type="button"
                className="jiuwenmate-profile__reply-mode-option"
                aria-pressed={permissionProfile === profile}
                disabled={savingResources || !onSaveSettings}
                data-testid="jiuwenmate-profile-permission-option"
                data-variant={profile}
                onClick={() => onPermissionProfileChange(profile)}
              >
                <span className="jiuwenmate-profile__reply-mode-copy">
                  <strong>{t(`jiuwenmate.profile.permissions.profiles.${profile}`)}</strong>
                  <small>{t(`jiuwenmate.profile.permissions.profileHints.${profile}`)}</small>
                </span>
              </button>
            ))}
          </div>
          <p className="jiuwenmate-profile__permission-warning" data-testid="jiuwenmate-profile-permission-warning">
            {permissionProfile === 'full_access'
              ? t('jiuwenmate.profile.permissions.fullAccessWarning')
              : t('jiuwenmate.profile.permissions.approvalTimeout')}
          </p>
        </div>
        {sharingApi ? (
          <GroupSharingPermissions
            api={sharingApi}
            configId={colleague.configId}
            connectionEpoch={connectionEpoch}
          />
        ) : null}
      </section>
    );
  }

  if (section === 'schedules') {
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <ScheduleSection
          title={title}
          schedules={schedules}
          saving={savingSchedule}
          onCreate={onCreateSchedule}
          onUpdate={onUpdateSchedule}
          onSetEnabled={onSetScheduleEnabled}
          onDelete={onDeleteSchedule}
          onRun={onRunSchedule}
          onLoadHistory={onLoadScheduleHistory}
        />
      </section>
    );
  }

  if (section === 'mail') {
    return (
      <section className="jiuwenmate-profile__block jiuwenmate-profile__mail" data-testid="jiuwenmate-profile-detail" data-variant="mail">
        <header>
          <h2>{title}</h2>
        </header>
        <article className="jiuwenmate-profile__mail-card" data-testid="jiuwenmate-profile-mail-setup">
          <span className="jiuwenmate-profile__mail-icon" aria-hidden="true"><Mail size={22} /></span>
          <div className="jiuwenmate-profile__mail-content">
            <h3>{t('jiuwenmate.profile.mail.title')}</h3>
            <p>{t('jiuwenmate.profile.mail.description')}</p>
            <p className="jiuwenmate-profile__mail-note" data-testid="jiuwenmate-profile-mail-status">
              {t(`jiuwenmate.profile.mail.status.${agentMailStatus}`, { defaultValue: t('jiuwenmate.profile.mail.isolationHint') })}
            </p>
            {agentMailStatus === 'connected' && agentMailEmail && (
              <p className="jiuwenmate-profile__mail-note" data-testid="jiuwenmate-profile-mail-address">
                <strong>{t('jiuwenmate.profile.mail.mailboxAddress')}</strong> {agentMailEmail}
              </p>
            )}
            {agentMailStatus === 'connected' ? (
              <button type="button" className="jiuwenmate-dark-button" onClick={onDisconnectAgentMail} data-testid="jiuwenmate-profile-mail-disconnect">
                {t('jiuwenmate.profile.mail.disconnect')}
              </button>
            ) : agentMailStatus === 'authorizing' ? (
              <button type="button" className="jiuwenmate-dark-button" onClick={onCancelAgentMailAuth} data-testid="jiuwenmate-profile-mail-cancel">
                {t('jiuwenmate.profile.mail.cancel')}
              </button>
            ) : (
              <button
                type="button"
                className="jiuwenmate-dark-button"
                disabled={agentMailStatus === 'unavailable' || agentMailStatus === 'loading' || agentMailStatus === 'authorizing'}
                onClick={onStartAgentMailAuth}
                data-testid="jiuwenmate-profile-mail-start"
              >
                {t(agentMailStatus === 'authorizing' ? 'jiuwenmate.profile.mail.authorizing' : 'jiuwenmate.profile.mail.start')}
              </button>
            )}
            {agentMailError && <p role="alert" className="jiuwenmate-profile__mail-note">{agentMailError}</p>}
          </div>
        </article>
      </section>
    );
  }

  if (RESOURCE_SECTIONS.some((item) => item === section) && onSaveSettings) {
    const resourceSection = section as ResourceSection;
    const selected =
      resourceSection === 'skills'
        ? skills
        : resourceSection === 'connectors'
          ? connectors
          : resourceSection === 'plugins'
            ? plugins
            : knowledge;
    const catalog = resourceCatalog?.[resourceSection] ?? null;
    const changeSelected =
      resourceSection === 'skills'
        ? onSkillsChange
        : resourceSection === 'connectors'
          ? onConnectorsChange
          : resourceSection === 'plugins'
            ? onPluginsChange
            : onKnowledgeChange;
    const dirty =
      !sameNames(skills, colleague.capabilities.skills) ||
      !sameNames(connectors, colleague.capabilities.connectors) ||
      !sameNames(plugins, colleague.capabilities.plugins) ||
      !sameNames(knowledge, colleague.capabilities.knowledge);
    const emptyKey =
      resourceSection === 'skills'
        ? 'jiuwenmate.profile.catalogEmptySkills'
        : resourceSection === 'connectors'
          ? 'jiuwenmate.profile.catalogEmptyConnectors'
          : resourceSection === 'plugins'
            ? 'jiuwenmate.profile.catalogEmptyPlugins'
            : 'jiuwenmate.profile.catalogEmptyKnowledge';
    const selectedOptions = selected.map(
      (name) =>
        catalog?.find((option) => option.name === name || option.alias === name) ?? {
          name,
          label: name,
          description: '',
        },
    );
    const pickerItems = (catalog ?? []).map((option) => ({
      id: option.name,
      name: option.label,
      description: option.description,
    }));
    const sectionTitle = `${title}（${selected.length}）`;
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <header>
          <h2>{sectionTitle}</h2>
          <button
            type="button"
            className="jiuwenmate-profile__resource-add"
            onClick={() => setResourcePickerOpen(true)}
            data-testid="jiuwenmate-profile-resource-add"
          >
            <Plus size={14} aria-hidden />
            <span>{t(`jiuwenmate.create.add.${resourceSection}`)}</span>
          </button>
        </header>
        {resourceCatalog && !resourceCatalog.ready ? (
          <p className="jiuwenmate-capability-empty">{t('jiuwenmate.profile.catalogLoading')}</p>
        ) : null}
        {resourceCatalog?.ready && catalog === null ? (
          <p className="jiuwenmate-capability-empty">{t('jiuwenmate.profile.catalogFailed')}</p>
        ) : null}
        {resourceCatalog?.ready && catalog !== null && catalog.length === 0 && selected.length === 0 ? (
          <p className="jiuwenmate-capability-empty">{t(emptyKey)}</p>
        ) : null}
        {selected.length === 0 && catalog && catalog.length > 0 ? (
          resourceSection === 'knowledge' ? (
            <div className="jiuwenmate-profile__knowledge-empty">
              <BookOpen size={34} aria-hidden />
              <p>{t(`jiuwenmate.create.empty.${resourceSection}`)}</p>
              <button type="button" className="jiuwenmate-dark-button" onClick={() => setResourcePickerOpen(true)}>
                <Plus size={14} aria-hidden />
                {t(`jiuwenmate.create.add.${resourceSection}`)}
              </button>
            </div>
          ) : <p className="jiuwenmate-capability-empty">{t(`jiuwenmate.create.empty.${resourceSection}`)}</p>
        ) : null}
        {selected.length > 0 ? (
          <div className={resourceSection === 'knowledge' ? 'jiuwenmate-profile__knowledge-list' : 'jiuwenmate-profile__resource-grid'}>
            {selectedOptions.map((option) =>
              resourceSection === 'knowledge' ? (
                <article key={option.name} className="jiuwenmate-profile__knowledge-item" data-testid="jiuwenmate-profile-resource-card">
                  <span className="jiuwenmate-profile__knowledge-icon" aria-hidden><FileText size={20} /></span>
                  <span className="jiuwenmate-profile__knowledge-copy">
                    <strong>{option.label}</strong>
                    <small>
                      {option.source ? `${t('jiuwenmate.profile.knowledgeSource')}: ${option.source}` : option.description || t('connectorMarket.common.noDescription')}
                      {option.updatedAt ? ` · ${t('jiuwenmate.profile.knowledgeUpdatedAt')}: ${option.updatedAt}` : ''}
                    </small>
                  </span>
                  <button
                    type="button"
                    className="jiuwenmate-icon-button"
                    aria-label={`${t('jiuwenmate.create.remove')} ${option.label}`}
                    onClick={() => changeSelected(selected.filter((value) => value !== option.name))}
                  >
                    <Trash2 size={15} aria-hidden />
                  </button>
                </article>
              ) : (
                <PageCard
                  key={option.name}
                  testId="jiuwenmate-profile-resource-card"
                  variant={option.name}
                  avatar={{ name: option.label }}
                  title={option.label}
                  description={option.description || t('connectorMarket.common.noDescription')}
                  actionsHover
                  action={{
                    icon: <Trash2 size={15} aria-hidden />,
                    tooltip: `${t('jiuwenmate.create.remove')} ${option.label}`,
                    onClick: () => changeSelected(selected.filter((value) => value !== option.name)),
                  }}
                />
              ),
            )}
          </div>
        ) : null}
        <div className="jiuwenmate-profile__resource-actions">
          <button
            type="button"
            className="jiuwenmate-dark-button jiuwenmate-profile__save-button"
            disabled={!dirty || savingResources}
            onClick={() =>
              onSaveSettings({
                skills: [...skills],
                connectors: [...connectors],
                plugins: [...plugins],
                knowledge: [...knowledge],
              })
            }
            data-testid="jiuwenmate-profile-resource-save"
          >
            {savingResources ? t('jiuwenmate.profile.savingResources') : t('jiuwenmate.profile.saveResources')}
          </button>
        </div>
        {resourcePickerOpen ? (
          <ConnectorPickerDrawer
            title={t(`jiuwenmate.create.add.${resourceSection}`)}
            testId="jiuwenmate-profile-resource-picker"
            status={!resourceCatalog?.ready ? 'loading' : 'success'}
            items={pickerItems}
            getItemKey={(item) => item.id}
            initialSelectedIds={selected.map(
              (name) => catalog?.find((option) => option.name === name || option.alias === name)?.name ?? name,
            )}
            onClose={() => setResourcePickerOpen(false)}
            onConfirm={(ids) => {
              changeSelected(ids);
              setResourcePickerOpen(false);
            }}
            searchPlaceholder={t('connectorMarket.common.search')}
            loadingMessage={t('jiuwenmate.profile.catalogLoading')}
            emptyMessage={
              catalog === null
                ? t('jiuwenmate.profile.catalogFailed')
                : catalog.length === 0
                  ? t(emptyKey)
                  : t('connectorMarket.common.noResult')
            }
            fallbackDescription={t('connectorMarket.common.noDescription')}
          />
        ) : null}
      </section>
    );
  }

  const items = colleague.capabilities[section];
  return (
    <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
      <h2>{title}</h2>
      {section === 'knowledge' && colleague.workspaceDir ? (
        <div data-testid="jiuwenmate-profile-workspace-dir">
          <dl className="jiuwenmate-profile__facts">
            <dt>{t('jiuwenmate.profile.workspaceDir')}</dt>
            <dd>
              <code>{colleague.workspaceDir}</code>
            </dd>
          </dl>
          <p className="jiuwenmate-capability-empty">{t('jiuwenmate.profile.workspaceDirHint')}</p>
        </div>
      ) : null}
      {items.length === 0 ? (
        <p className="jiuwenmate-capability-empty">{t(`jiuwenmate.create.empty.${section}`)}</p>
      ) : (
        <div className="jiuwenmate-capability-grid">
          {items.map((item) => (
            <span key={item} className="jiuwenmate-capability-chip">
              {item}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
