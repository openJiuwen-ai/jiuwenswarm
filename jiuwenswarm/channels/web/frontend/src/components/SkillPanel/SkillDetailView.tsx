import { openAssetPublish } from '../../features/assetPublishEvents';
/**
 * 技能详情页（统一组件）
 *
 * - mode 'installed'：我的技能详情（头部操作 / 版本管理 / 内容详情 / 文件预览 / 技能经验）
 * - mode 'hub'：技能广场详情（安装 / 去试试 + 内容详情），骨架样式与我的技能详情一致
 *
 * 由 MySkillDetail 与 MarketplaceView 的广场详情分支合并而来。
 */
import { useTranslation } from 'react-i18next';
import type { ReactNode } from 'react';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import MoreIcon from '../../assets/work-mode/more-rimless.svg?react';
import {
  DetailSection,
  EntityHeader,
  type EntityHeaderAvatar,
  FilePreviewContent,
  FilePreviewPanel,
  FilePreviewTree,
  MarkdownPane,
  PageCard,
  PageToolbar,
  Tabs,
  isPreviewableImagePath,
  type FilePreviewContentFile,
  type FilePreviewTreeNode,
} from '../ui';
import { Switch } from '../Switch';
import { buildSkillVersionOptions } from './skillVersionOptions';
import type {
  EvolutionEntry,
  HubSkillDetail,
  InstalledPluginItem,
  LoadState,
  MarketplacePluginItem,
  SkillDetail,
  SkillFilePreview,
  SkillPackMember,
  SkillVersion,
} from './types';

/**
 * 将技能文件预览数据映射为共享 FilePreviewContent 所需结构。
 * 以当前选中路径为准：加载中/失败时 content 为空，但 header 与错误态仍正常显示
 * （对齐 agent 侧 file.loading/file.error 行为）；图片回退 raw-file 地址。
 */
function buildSkillPreviewFile(
  filePreview: SkillFilePreview | null,
  selectedPath: string,
  skillFilePath: string,
): FilePreviewContentFile {
  const matched = filePreview && filePreview.path === selectedPath ? filePreview : null;
  const skillDir = (skillFilePath || '').replace(/[\\/][^\\/]+$/, '');
  const downloadUrl =
    matched?.download_url ||
    (isPreviewableImagePath(selectedPath)
      ? `/file-api/raw-file?path=${encodeURIComponent(`${skillDir}/${selectedPath}`)}`
      : null);
  return { path: selectedPath, content: matched?.content ?? null, downloadUrl };
}

/** 技能包成员阻塞原因 → i18n key（用于"包含技能"页签成员卡片状态徽标） */
const MEMBER_BLOCKING_REASON_KEYS: Record<string, string> = {
  disabled: 'skills.memberBlockedReasonDisabled',
  missing: 'skills.memberBlockedReasonMissing',
  invalid: 'skills.memberBlockedReasonInvalid',
  uninstalled: 'skills.memberBlockedReasonUninstalled',
};

/** 技能包"包含技能"成员卡片网格（我的技能详情 / 广场详情共用），视觉与技能卡片一致 */
function PackMembersGrid({
  members,
  mode = 'hub',
  onOpenMember,
  onInstallMember,
  installingName,
}: {
  members?: SkillPackMember[];
  /** installed：已安装模式，卡片可点击进成员详情；hub：未安装，只读 */
  mode?: 'hub' | 'installed';
  onOpenMember?: (name: string) => void;
  /** 卸载成员的"安装"回调（从包内备份恢复） */
  onInstallMember?: (name: string) => void;
  /** 正在安装的成员名（loading 态） */
  installingName?: string | null;
}) {
  const { t } = useTranslation();
  // 成员为空时显示空态文案
  if (!members || members.length === 0) {
    return (
      <div className="flex items-center justify-center py-10 text-sm text-text-muted">
        {t('skills.noPackMembers')}
      </div>
    );
  }
  return (
    <div className="card-grid-auto" data-testid="skill-panel-pack-members-grid">
      {members.map((member) => {
        // enabled === false 或 available === false 时视为不可用，并展示阻塞原因
        const isUnavailable = member.enabled === false || member.available === false;
        // 硬阻塞（缺失/损坏）成员不可交互
        const isBlocked = member.available === false;
        const reasonKey = member.blocking_reason ? MEMBER_BLOCKING_REASON_KEYS[member.blocking_reason] : undefined;
        const interactable = mode === 'installed' && !isBlocked;
        const isRestorable = mode === 'installed' && isBlocked && member.restorable === true;
        const isInstalling = installingName === member.name;
        // 右上角操作区：已卸载成员显示「安装」按钮（从包内备份一键恢复）
        const actionContent = isRestorable ? (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onInstallMember?.(member.name);
            }}
            disabled={isInstalling}
            data-testid="skill-panel-pack-member-install-btn"
            className="flex items-center gap-1 h-7 px-2.5 rounded-[14px] text-xs text-text-inverse bg-control-emphasis hover:opacity-80 disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {isInstalling ? t('common.loading') : t('skills.actions.install')}
          </button>
        ) : undefined;
        return (
          <PageCard
            key={member.path || member.name}
            testId="skill-panel-pack-member-card"
            variant={member.path || member.name}
            avatar={{ name: member.display_name || member.name }}
            title={member.display_name || member.name}
            label={
              isUnavailable
                ? [`${t('skills.memberUnavailable')}${reasonKey ? ` · ${t(reasonKey)}` : ''}`]
                : undefined
            }
            description={member.description || t('skills.noDescription')}
            onClick={interactable ? () => onOpenMember?.(member.name) : undefined}
            actionSlot={actionContent}
          />
        );
      })}
    </div>
  );
}

interface SkillDetailCommonProps {
  detailState: LoadState;
  installedSkillMap: Map<string, InstalledPluginItem>;
}

export interface InstalledSkillDetailViewProps extends SkillDetailCommonProps {
  mode: 'installed';
  selectedSkill: SkillDetail;
  detailContent: string;
  detailTab: 'content' | 'files' | 'experience' | 'members';
  setDetailTab: (tab: 'content' | 'files' | 'experience' | 'members') => void;
  detailMenuOpen: boolean;
  setDetailMenuOpen: (open: boolean) => void;
  actionTarget: string | null;
  skillVersions: SkillVersion[];
  skillVersionsDefault: string | null;
  versionsLoadState: LoadState;
  onFetchSkillDetail: (skillName: string, version?: string) => void;
  filesLoadState: LoadState;
  filePreviewPath: string | null;
  filePreviewStatus: LoadState;
  previewTreeNodes: FilePreviewTreeNode[];
  filePreview: SkillFilePreview | null;
  onSelectSkillFile: (filePath: string) => void;
  onFetchSkillFiles: (skillName: string) => void;
  evolutionMessage: string | null;
  evolutionMessageType: 'success' | 'error' | null;
  evolutionFormatError: string | null;
  evolutionListState: LoadState;
  sortedEvolutionEntries: EvolutionEntry[];
  onEvolutionContentChange: (entryId: string, value: string) => void;
  onEvolutionDeleteEntry: (entryId: string) => void;
  rebuildLoading: boolean;
  onRebuild: (skillName: string, version: string | null) => void;
  setSynthesizeTooltip: (tooltip: { left: number; top: number } | null) => void;
  onBackToList: () => void;
  onEditSkill: (skillName: string, skillType?: string) => void;
  onUninstall: (pluginName: string) => void;
  onToggleSkillDisabled: (skillName: string) => void;
  onGoToChat: (skillName: string, skillType?: string) => void;
  /** "包含技能"成员：点击进成员详情 */
  onOpenPackMember: (memberName: string) => void;
  /** "包含技能"成员：已卸载成员从包内备份安装恢复 */
  onInstallPackMember: (memberName: string) => void;
  /** 正在安装（恢复）的成员名 */
  installingPackMemberName: string | null;
}

export interface HubSkillDetailViewProps extends SkillDetailCommonProps {
  mode: 'hub';
  hubSkill: MarketplacePluginItem;
  hubDetail: HubSkillDetail | null;
  /** 广场详情页签（内容详情 / 包含技能，仅技能包显示"包含技能"） */
  hubDetailTab: 'content' | 'members';
  setHubDetailTab: (tab: 'content' | 'members') => void;
  actionTarget: string | null;
  onInstallHubSkill: (skill: MarketplacePluginItem) => void;
  onGoToChat: (skillName: string, skillType?: string) => void;
  onBackToHubDetail: () => void;
}

export type SkillDetailViewProps = InstalledSkillDetailViewProps | HubSkillDetailViewProps;

export function SkillDetailView(props: SkillDetailViewProps) {
  const { t, i18n } = useTranslation();
  const tid = props.mode === 'installed' ? 'skill-panel-my-detail' : 'skill-panel-hub-detail';
  const onBack = props.mode === 'installed' ? props.onBackToList : props.onBackToHubDetail;

  const renderShell = (
    header: { avatar: EntityHeaderAvatar; title: string; titleEnd?: ReactNode; tags?: string[] },
    headerRight: ReactNode,
    basicInfoText: ReactNode,
    body: ReactNode,
    notice?: ReactNode,
  ) => (
    <>
      {props.detailState === 'loading' ? (
        /* 详情加载中：整区域状态视图（参考 agent-management），flex:1 填满详情区避免布局塌陷/抖动 */
        <div data-testid={`${tid}-state`} data-variant="loading" className="skill-detail-state">
          <button type="button" className="detail-back" onClick={onBack}>
            <BackIcon aria-hidden="true" />
            {t('agentManagement.actions.back')}
          </button>
          <p>{t('common.loading')}</p>
        </div>
      ) : (
        <div className="flex-1 flex flex-col min-h-0" data-testid={tid}>
          {/* 错误状态 */}
          {props.detailState === 'error' && (
            <div data-testid={`${tid}-state`} className="text-sm text-text-muted mb-3" data-variant="error">
              {t('skills.detailError')}
            </div>
          )}

          {/* 返回按钮 */}
          <button type="button" className="detail-back" onClick={onBack} data-testid={`${tid}-back-btn`}>
            <BackIcon aria-hidden="true" />
            {t('agentManagement.actions.back')}
          </button>

          <div className="detail-body flex-1 min-h-0 overflow-y-auto">
            {/* 顶部：头像/名称 + 元信息 + 操作按钮（skill/agent 详情共享组件） */}
            <EntityHeader
              testId={`${tid}-header`}
              titleTestId={`${tid}-name`}
              avatar={header.avatar}
              title={header.title}
              titleEnd={header.titleEnd}
              tags={header.tags}
              actions={headerRight}
            />

            {/* 基本信息（skill/agent 详情共享区块组件） */}
            <DetailSection testId={`${tid}-basic-info`} title={t('skills.detail.basicInfo')}>
              {/* 基本信息内的灰色来源提示（如"非自研"说明）：info 图标 + 弱化文本，无边框卡片 */}
              {notice ? (
                <div
                  className="flex items-center gap-1.5 text-sm text-text-muted mb-1.5"
                  data-testid={`${tid}-notice`}
                >
                  {notice}
                </div>
              ) : null}
              {basicInfoText}
            </DetailSection>

            {body}
          </div>
        </div>
      )}
    </>
  );

  if (props.mode === 'hub') {
    const { hubSkill, hubDetail, installedSkillMap, actionTarget, onInstallHubSkill, onGoToChat, hubDetailTab, setHubDetailTab } = props;
    const isInstalled = installedSkillMap.has(hubSkill.name);
    const installing = actionTarget === `install:${hubSkill.identifier || hubSkill.asset_id}`;
    // 当前广场条目是否为技能包（用于显示"包含技能"页签）
    const isHubPackSkill = hubSkill.skill_type === 'skillpack' || hubSkill.plugin_type === 'skillpack';
    return renderShell(
      {
        avatar: { name: hubSkill.display_name || hubSkill.name, iconUrl: hubSkill.icon_uri, testId: 'skill-panel-hub-avatar' },
        title: hubSkill.display_name || hubSkill.name,
      },
      /* 下载/去试试按钮 */
      <div className="flex items-center gap-2 flex-shrink-0">
        {isInstalled ? (
          <button
            onClick={() => onGoToChat(hubSkill.name, hubSkill.plugin_type === 'swarmskill' ? 'swarm_skill' : undefined)}
            className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
            style={{ height: '32px', padding: '0 24px' }}
            data-testid={`${tid}-go-try-btn`}
          >
            {t('skills.actions.goTry')}
          </button>
        ) : (
          <button
            onClick={() => onInstallHubSkill(hubSkill)}
            disabled={installing}
            className="flex items-center justify-center rounded-[16px] text-sm text-text-inverse bg-control-emphasis hover:bg-control-emphasis-hover-strong whitespace-nowrap disabled:opacity-50"
            style={{ width: '96px', height: '32px' }}
            data-testid="skill-panel-hub-detail-install-btn"
          >
            {installing ? t('skills.actions.installing') : t('skills.actions.install')}
          </button>
        )}
      </div>,
      hubDetail?.data?.short_desc || hubSkill.short_desc || t('skills.noDescription'),
      /* 页签：内容详情 / 包含技能（仅技能包显示"包含技能"） */
      <div data-testid={`${tid}-tabs-section`} className="flex flex-col min-h-0">
        <PageToolbar style={{ marginTop: 0, flexShrink: 0 }}>
          <Tabs
            role="tablist"
            itemTestId={`${tid}-tab`}
            className="text-base"
            value={hubDetailTab}
            onChange={(tab) => setHubDetailTab(tab)}
            items={[
              { value: 'content', label: t('skills.detail.tabs.contentDetail') },
              ...(isHubPackSkill
                ? [{ value: 'members' as const, label: t('skills.includedSkills'), testId: 'skill-panel-pack-members-tab' }]
                : []),
            ]}
          />
        </PageToolbar>

        {/* 内容详情 */}
        {hubDetailTab === 'content' ? (
          <MarkdownPane
            testId={`${tid}-content`}
            content={hubDetail?.data?.detail_desc || null}
            emptyText={t('skills.noContent')}
          />
        ) : (
          /* 包含技能成员列表（未安装，无启停开关） */
          <div className="flex-1 min-h-0">
            <PackMembersGrid members={hubDetail?.data?.skillpack?.members} />
          </div>
        )}
      </div>,
    );
  }

  const {
    selectedSkill,
    detailContent,
    detailTab,
    setDetailTab,
    detailMenuOpen,
    setDetailMenuOpen,
    installedSkillMap,
    actionTarget,
    skillVersions,
    skillVersionsDefault,
    versionsLoadState,
    onFetchSkillDetail,
    filesLoadState,
    filePreviewPath,
    filePreviewStatus,
    previewTreeNodes,
    filePreview,
    onSelectSkillFile,
    onFetchSkillFiles,
    evolutionMessage,
    evolutionMessageType,
    evolutionFormatError,
    evolutionListState,
    sortedEvolutionEntries,
    onEvolutionContentChange,
    onEvolutionDeleteEntry,
    rebuildLoading,
    onRebuild,
    setSynthesizeTooltip,
    onEditSkill,
    onUninstall,
    onToggleSkillDisabled,
    onGoToChat,
    onOpenPackMember,
    onInstallPackMember,
    installingPackMemberName,
  } = props;
  const skillDisplayName = selectedSkill.display_name || selectedSkill.name;
  const uninstallPluginName = installedSkillMap.get(selectedSkill.name)?.plugin_name || selectedSkill.name;
  const uninstalling = actionTarget === `uninstall:${uninstallPluginName}`;
  // 当前详情技能是否为技能包（接口标记 skillpack，或技能名与所属插件名一致）
  const packPlugin = installedSkillMap.get(selectedSkill.name);
  const isPackSkill =
    selectedSkill.skill_type === 'skillpack' ||
    Boolean(packPlugin && packPlugin.plugin_name === selectedSkill.name && packPlugin.skills.length > 1);
  // 技能包含有已卸载成员时整包被禁用，且不允许直接启用（需重新下载完整技能包）
  const isPackBlocked = (selectedSkill.blocked_members?.length ?? 0) > 0;
  return renderShell(
    {
      avatar: { name: skillDisplayName, testId: 'skill-panel-my-detail-avatar' },
      title: skillDisplayName,
      titleEnd: selectedSkill.has_evolutions ? (
        <button
          onClick={(e) => {
            e.stopPropagation();
            setDetailTab('experience');
          }}
          className="relative shrink-0 w-5 h-5 flex items-center justify-center text-text-muted hover:text-text"
          title={t('skills.actions.viewEvolution')}
          data-testid="skill-panel-my-detail-evolution-btn"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          >
            <path d="M10.268 21a2 2 0 0 0 3.464 0" />
            <path d="M11.68 2.009A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673c-.824-.85-1.678-1.731-2.21-3.348" />
            <circle cx="18" cy="5" r="3" />
          </svg>
        </button>
      ) : null,
      tags:
        selectedSkill.skill_type === 'swarm_skill'
          ? [t('skills.skillTypes.team')]
          : selectedSkill.skill_type === 'multimodal_skill'
            ? [t('skills.skillTypes.multimodal')]
            : selectedSkill.skill_type === 'skillpack'
              ? [t('skills.skillTypes.skillpack')]
              : undefined,
    },
    /* 右侧操作按钮 */
    <div className="flex items-center gap-6 flex-shrink-0">
      <button type="button" className="rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-text" data-testid="skill-panel-my-detail-publish-btn" onClick={() => openAssetPublish({ kind: 'skill', local_id: selectedSkill.name })}>{t('skills.actions.publish')}</button>
      {/* ... 菜单：编辑/卸载 */}
      <div className="relative">
        <button
          type="button"
          onClick={() => setDetailMenuOpen(!detailMenuOpen)}
          className="w-7 h-7 flex items-center justify-center rounded-md hover:bg-secondary text-text hover:text-text"
          data-testid="skill-panel-my-detail-menu"
        >
          <MoreIcon aria-hidden />
        </button>
        {detailMenuOpen ? (
          <>
            <div className="fixed inset-0 z-40" onClick={() => setDetailMenuOpen(false)} />
            <div className="dropdown-menu">
              <button
                onClick={
                  selectedSkill.enabled !== false
                    ? () => {
                        setDetailMenuOpen(false);
                        onEditSkill(selectedSkill.name, selectedSkill.skill_type);
                      }
                    : undefined
                }
                disabled={selectedSkill.enabled === false}
                className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                data-testid="skill-panel-my-detail-menu-edit"
              >
                {t('skills.actions.edit')}
              </button>
              <button
                onClick={
                  uninstalling
                    ? undefined
                    : () => {
                        setDetailMenuOpen(false);
                        onUninstall(uninstallPluginName);
                      }
                }
                disabled={uninstalling}
                className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                data-testid="skill-panel-my-detail-menu-uninstall"
              >
                {t(uninstalling ? 'skills.actions.uninstalling' : 'skills.actions.uninstall')}
              </button>
            </div>
          </>
        ) : null}
      </div>
      {/* 启用开关 + 文字（技能包被成员缺失阻塞时禁用开关） */}
      <div
        className="flex items-center gap-2"
        title={isPackBlocked ? t('skills.packBlockedHint') : undefined}
      >
        <Switch
          checked={selectedSkill.enabled !== false}
          onChange={() => onToggleSkillDisabled(selectedSkill.name)}
          disabled={actionTarget === `toggle:${selectedSkill.name}` || isPackBlocked}
        />
        <span
          className="text-sm text-text whitespace-nowrap"
          data-testid="skill-panel-my-detail-enable-label"
          data-variant={selectedSkill.enabled !== false ? 'enabled' : 'disabled'}
        >
          {selectedSkill.enabled !== false ? t('skills.enable') : t('skills.disable')}
        </span>
      </div>
      {/* 去试试 */}
      <button
        onClick={
          selectedSkill.enabled !== false ? () => onGoToChat(selectedSkill.name, selectedSkill.skill_type) : undefined
        }
        disabled={selectedSkill.enabled === false}
        className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap disabled:opacity-40 disabled:cursor-not-allowed"
        style={{ height: '32px', padding: '0 24px' }}
        data-testid={`${tid}-go-try-btn`}
      >
        {t('skills.actions.goTry')}
      </button>
    </div>,
    selectedSkill.description || t('skills.noDescription'),
    <>
      {/* 版本管理 */}
      <DetailSection testId={`${tid}-versions-section`} title={t('skills.detail.versionManage')}>
        {versionsLoadState === 'loading' ? (
          <div
            data-testid="skill-panel-my-detail-versions-state"
            className="text-sm text-text-muted"
            data-variant="loading"
          >
            {t('common.loading')}
          </div>
        ) : versionsLoadState === 'error' ? (
          <div
            data-testid="skill-panel-my-detail-versions-state"
            className="text-sm text-text-muted"
            data-variant="error"
          >
            {t('skills.detail.versionsLoadFailed')}
          </div>
        ) : skillVersions.length === 0 ? (
          <div
            data-testid="skill-panel-my-detail-versions-state"
            className="text-sm text-text-muted"
            data-variant="empty"
          >
            {selectedSkill.version ? `v${selectedSkill.version}` : t('skills.detail.noVersions')}
          </div>
        ) : (
          <div className="flex items-center gap-2 flex-wrap">
            <select
              value={selectedSkill.version || skillVersionsDefault || ''}
              onChange={(e) => {
                const ver = e.target.value;
                if (ver) onFetchSkillDetail(selectedSkill.name, ver);
              }}
              className="appearance-none rounded-[6px] border border-border bg-panel text-xs text-text outline-none focus:outline-none focus:ring-0 focus:border-border"
              style={{ width: '360px', height: '28px', paddingLeft: '12px', paddingRight: '12px' }}
              data-testid="skill-panel-my-detail-versions-select"
            >
              {buildSkillVersionOptions(skillVersions, {
                defaultSuffix: ` (${t('skills.detail.defaultVersion')})`,
                unavailableSuffix: ` (${t('skills.detail.unavailableVersion')})`,
              }).map((option) => (
                <option key={option.version} value={option.version} disabled={option.disabled} className="text-xs">
                  {option.label}
                </option>
              ))}
            </select>
            {selectedSkill.has_evolutions ? (
              <button
                onClick={() => onRebuild(selectedSkill.name, selectedSkill.version || null)}
                disabled={rebuildLoading}
                className="flex items-center justify-center rounded-[6px] text-xs text-text-muted border border-border hover:bg-secondary whitespace-nowrap disabled:opacity-50"
                style={{ height: '28px', padding: '0 12px' }}
              >
                {rebuildLoading ? t('common.processing') : t('skills.actions.rebuild')}
              </button>
            ) : null}
          </div>
        )}
      </DetailSection>

      {/* 三个页签 */}
      <div data-testid={`${tid}-tabs-section`} className="flex flex-col min-h-0">
        <PageToolbar style={{ marginTop: 0, flexShrink: 0 }}>
          <Tabs
            role="tablist"
            itemTestId={`${tid}-tab`}
            className="text-base"
            value={detailTab}
            onChange={(tab) => {
              setDetailTab(tab);
              if (tab === 'files' && filesLoadState === 'idle') {
                onFetchSkillFiles(selectedSkill.name);
              }
            }}
            items={[
              { value: 'content', label: t('skills.detail.tabs.contentDetail') },
              ...(isPackSkill
                ? [
                    {
                      value: 'members' as const,
                      label: t('skills.includedSkills'),
                      testId: 'skill-panel-pack-members-tab',
                    },
                  ]
                : []),
              { value: 'files', label: t('skills.detail.tabs.filePreview') },
              ...(selectedSkill.has_evolutions
                ? [{ value: 'experience' as const, label: t('skills.detail.tabs.skillExperience') }]
                : []),
            ]}
          />

          {/* 合成新版本按钮（仅技能经验页签时显示） */}
          {detailTab === 'experience' && selectedSkill.has_evolutions ? (
            <button
              onClick={() => onRebuild(selectedSkill.name, selectedSkill.version || null)}
              disabled={rebuildLoading}
              onMouseEnter={(e) => {
                const rect = e.currentTarget.getBoundingClientRect();
                setSynthesizeTooltip({ left: rect.left + rect.width / 2, top: rect.top });
              }}
              onMouseLeave={() => setSynthesizeTooltip(null)}
              className="mb-1 flex items-center justify-center rounded-[16px] font-semibold text-chat-accent bg-card hover:bg-secondary/30 whitespace-nowrap disabled:opacity-50"
              style={{ width: '118px', lineHeight: '22px', fontSize: '14px' }}
            >
              {rebuildLoading ? t('common.processing') : t('skills.actions.synthesizeNewVersion')}
            </button>
          ) : null}
        </PageToolbar>

        {/* 内容详情 */}
        {detailTab === 'content' && (
          <MarkdownPane
            testId={`${tid}-content`}
            content={selectedSkill.content ? detailContent : null}
            emptyText={t('skills.noContent')}
          />
        )}

        {/* 包含技能（技能包成员；已安装模式可点击进成员详情，卸载成员可安装恢复） */}
        {detailTab === 'members' && (
          <PackMembersGrid
            mode="installed"
            members={selectedSkill.skillpack?.members}
            onOpenMember={onOpenPackMember}
            onInstallMember={onInstallPackMember}
            installingName={installingPackMemberName}
          />
        )}

        {/* 文件预览 */}
        {detailTab === 'files' && (
          <FilePreviewPanel
            testId="skill-panel-file-preview"
            left={
              <FilePreviewTree
                testId="skill-panel-file-tree"
                ariaLabel={t('skills.detail.fileTree')}
                nodes={previewTreeNodes}
                status={filesLoadState}
                selectedPath={filePreviewPath}
                onSelectFile={onSelectSkillFile}
                onRetry={() => onFetchSkillFiles(selectedSkill.name)}
                labels={{
                  loading: t('common.loading'),
                  error: t('skills.detail.filesLoadFailed'),
                  empty: t('skills.detail.noFiles'),
                  retry: t('common.retry'),
                  notPreviewable: t('skills.detail.notPreviewable'),
                }}
              />
            }
            right={
              <FilePreviewContent
                testId="skill-panel-file-preview-content"
                selected={Boolean(filePreviewPath)}
                selectedPreviewable={Boolean(filePreviewPath)}
                file={
                  filePreviewPath ? buildSkillPreviewFile(filePreview, filePreviewPath, selectedSkill.file_path) : null
                }
                status={filePreviewStatus}
                labels={{
                  selectPrompt: t('skills.detail.selectFileToPreview'),
                  notPreviewable: t('skills.detail.notPreviewable'),
                  loading: t('common.loading'),
                  readError: t('agentManagement.files.readError'),
                  copy: t('agentManagement.files.copy'),
                  copied: t('agentManagement.files.copied'),
                  copyFailed: t('agentManagement.files.copyFailed'),
                  download: t('agentManagement.files.download'),
                  downloadFailed: t('artifacts.downloadFailed', {
                    name: filePreviewPath ? filePreviewPath.split('/').pop() || '' : '',
                  }),
                  noPreview: t('skills.detail.noPreview'),
                  binaryDownload: t('skills.detail.binaryFileDownload'),
                }}
              />
            }
          />
        )}

        {/* 技能经验 */}
        {detailTab === 'experience' && selectedSkill.has_evolutions && (
          <div className="flex-1 min-h-0 overflow-y-auto">
            {evolutionMessage && (
              <div
                className={`mb-3 px-3 py-2 rounded-md text-sm ${
                  evolutionMessageType === 'error' ? 'bg-secondary text-danger' : 'bg-secondary text-text'
                }`}
              >
                {evolutionMessage}
              </div>
            )}

            {evolutionFormatError && (
              <div className="mb-3 px-3 py-2 rounded-md bg-secondary text-sm text-danger">{evolutionFormatError}</div>
            )}

            {evolutionListState === 'loading' && (
              <div className="flex items-center justify-center text-text-muted">{t('common.loading')}</div>
            )}
            {evolutionListState === 'error' && (
              <div className="text-sm text-text-muted">{t('skills.evolution.errors.loadFailed')}</div>
            )}
            {evolutionListState === 'success' && !evolutionFormatError && sortedEvolutionEntries.length === 0 && (
              <div className="text-sm text-text-muted">{t('skills.evolution.empty')}</div>
            )}

            {evolutionListState === 'success' && !evolutionFormatError && sortedEvolutionEntries.length > 0 && (
              <div className="space-y-4">
                {sortedEvolutionEntries.map((entry) => (
                  <div
                    key={entry.id}
                    className="border border-border py-4 px-4 bg-[var(--color-skill-evolution-card-surface)]"
                    style={{ borderRadius: '8px' }}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 text-xs space-y-1 w-[90%]">
                        <div className="grid grid-cols-3 gap-4">
                          <div>
                            <span className="text-text-muted">{t('skills.evolution.fields.section')}:</span>
                            <span className="ml-1 text-text">{entry.change?.section || '-'}</span>
                          </div>
                          <div>
                            <span className="text-text-muted">{t('skills.evolution.fields.target')}:</span>
                            <span className="ml-1 text-text">{entry.change?.target || '-'}</span>
                          </div>
                          <div>
                            <span className="text-text-muted">{t('skills.evolution.fields.timestamp')}:</span>
                            <span className="ml-1 text-text">
                              {entry.timestamp ? new Date(entry.timestamp).toLocaleString(i18n.language) : '-'}
                            </span>
                          </div>
                        </div>
                      </div>
                      <button
                        type="button"
                        onClick={() => onEvolutionDeleteEntry(entry.id)}
                        className="w-7 h-7 flex items-center justify-center rounded-lg hover:opacity-80 text-text"
                        title={t('skills.evolution.actions.delete')}
                      >
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={2}
                            d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                          />
                        </svg>
                      </button>
                    </div>

                    <div className="mt-3">
                      <textarea
                        value={entry.change?.content || ''}
                        onChange={(event) => onEvolutionContentChange(entry.id, event.target.value)}
                        className="w-full min-h-28 px-3 py-2 rounded-md bg-card border border-border text-sm text-text placeholder:text-text-muted"
                      />
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </>,
    /* 基本信息下方灰色提示条：内置技能且不在自研名单（proprietary 为 false 或缺失）时显示"非自研" */
    (selectedSkill.is_builtin || selectedSkill.is_builtin_source || selectedSkill.source === 'builtin') &&
    !selectedSkill.proprietary ? (
      <>
        <svg className="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"
          />
        </svg>
        <span>{t('skills.proprietaryLabels.thirdPartyHint')}</span>
      </>
    ) : undefined,
  );
}
