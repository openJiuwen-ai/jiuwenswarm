import { useCallback, useEffect, useMemo, useState, type RefObject } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useChatStore, useSessionStore } from '../../stores';
import { webRequest } from '../../services/webClient';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import { PickerPanel } from './PickerPanel';
import SearchIcon from '../../assets/agent-management/agent-search.svg?react';

/** 输入栏下拉所需的最小技能数据结构（与 SkillPanel 中的 SkillItem 保持一致） */
type SkillItem = {
  name: string;
  display_name?: string;
  description: string;
  source: string;
 is_builtin?: boolean;
 is_builtin_source?: boolean;
 enabled?: boolean;
 installed?: boolean;
 tags?: string[];
  /** 技能类型：skill | swarm_skill | multimodal_skill（后端 skills.list 返回） */
  skill_type?: SkillType;
};

/** 技能类型：skill | swarm_skill | multimodal_skill（后端 skills.list 返回） */
type SkillType = 'skill' | 'swarm_skill' | 'multimodal_skill';

/** 已安装插件信息（用于判定技能是否已安装） */
type InstalledPlugin = {
  plugin_name: string;
  marketplace: string;
  spec: string;
  version: string;
  installed_at: string;
  git_commit?: string | null;
  skills: string[];
};

/** 列表行高与间距：单行 50px（与 ChatPanel.css 的 .chat-skill-select__item 一致） */
const LIST_ROW_HEIGHT = 50;

interface SkillPickerPanelProps {
  onClose: () => void;
  /** 挂到面板根节点——由调用方（InputArea）持有，好在一级"+"菜单自己的 outside-click 判断里
   * 把这个二级面板也算作"菜单内部"，否则点二级面板（搜索框/技能项）会被一级菜单
   * 的监听器误判成"点了外面"直接把整个"+"菜单收起。 */
  panelRef: RefObject<HTMLDivElement>;
  /** 单 agent → skill_type==='skill'；集群 → skill_type==='swarm_skill' */
  isTeamMode: boolean;
  /** 一级"+"菜单展开方向：up 时面板与触发项底边齐平向上生长（见 PickerPanel direction） */
  direction?: 'up' | 'down';
  onNavigateToSkills?: () => void;
  onInsertSkill?: (skillName: string) => void;
  onRemoveSkill?: (skillName: string) => void;
}

/**
 * "+"菜单"技能"项的二级面板：搜索 + 已安装技能列表，选中态写入 sessionStore.selectedSkills
 * （随消息发送）。渲染在"+"菜单内部，与智能体/扩展面板同款定位（见 ChatPanel.css
 * .chat-agent-picker 规则）。
 */
export function SkillPickerPanel({
  onClose,
  panelRef,
  isTeamMode,
  direction,
  onNavigateToSkills,
  onInsertSkill,
  onRemoveSkill,
}: SkillPickerPanelProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const selectedSkills = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.selectedSkills ?? []);
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [plugins, setPlugins] = useState<InstalledPlugin[]>([]);
  const [searchQuery, setSearchQuery] = useState('');

  const installedSkillMap = useMemo(() => {
    const map = new Map<string, InstalledPlugin>();
    plugins.forEach((plugin) => {
      plugin.skills.forEach((skillName) => {
        if (!map.has(skillName)) map.set(skillName, plugin);
      });
    });
    return map;
  }, [plugins]);

  const isSkillInstalled = useMemo(
    () => (skill: SkillItem) =>
      skill.installed === true ||
      installedSkillMap.has(skill.name) ||
      skill.source === 'local' ||
      skill.source === 'project',
    [installedSkillMap],
  );

  const installedSkills = useMemo(
    () =>
      skills.filter(
        (s) =>
          isSkillInstalled(s) &&
          s.enabled !== false &&
          (isTeamMode ? s.skill_type === 'swarm_skill' : !s.skill_type || s.skill_type === 'skill'),
      ),
    [skills, isSkillInstalled, isTeamMode],
  );

  const filteredSkills = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return installedSkills;
    return installedSkills.filter((s) => {
      const name = s.name.toLowerCase();
      const displayName = (s.display_name || '').toLowerCase();
      const desc = s.description.toLowerCase();
      return name.includes(q) || displayName.includes(q) || desc.includes(q);
    });
  }, [installedSkills, searchQuery]);

  const { tooltip, handlers } = useAdaptiveTooltip({ offsetX: -50 });

  const fetchInstalledSkills = useCallback(async () => {
    if (!activeSessionId) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      const data = await webRequest<{ skills?: SkillItem[]; plugins?: InstalledPlugin[] }>(
        'skills.list',
        { with_installed: true },
        { timeoutMs: 30_000 },
      );
      setSkills(data.skills || []);
      setPlugins(data.plugins || []);
    } catch (err) {
      console.error('Failed to load installed skills:', err);
      setErrorMessage(t('skills.listError'));
    } finally {
      setLoading(false);
    }
  }, [activeSessionId, t]);

  useEffect(() => {
    void fetchInstalledSkills();
  }, [fetchInstalledSkills]);

  // 点击外部关闭面板（一级"+"菜单的 outside-click 监听已把 panelRef 算作内部，
  // 这里作为面板自身的独立兜底：一级菜单已关闭但面板还开着时也能关掉）
  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      if (!panelRef.current?.contains(event.target as Node)) onClose();
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [onClose, panelRef]);

  const handleOpenSkillsPage = useCallback(() => {
    onClose();
    onNavigateToSkills?.();
  }, [onClose, onNavigateToSkills]);

  const handleToggleSkill = useCallback(
    (skillName: string) => {
      const sid = useChatStore.getState().activeSessionId;
      if (!sid) return;
      const store = useSessionStore.getState();
      if (selectedSkills.includes(skillName)) {
        store.removeSelectedSkill(sid, skillName);
        onRemoveSkill?.(skillName);
      } else {
        store.addSelectedSkill(sid, skillName);
        onInsertSkill?.(skillName);
      }
    },
    [selectedSkills, onInsertSkill, onRemoveSkill],
  );

  return (
    <>
    <PickerPanel
      panelRef={panelRef}
      className="chat-skill-picker"
      direction={direction}
      testId="chat-panel-skill-select-panel"
      rowHeight={LIST_ROW_HEIGHT}
      itemCount={filteredSkills.length}
      search={
        <div className="chat-picker-panel__search" data-testid="chat-panel-skill-select-search">
          <div className="chat-picker-panel__search-inner">
            <SearchIcon aria-hidden="true" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t('chat.skillsSearchPlaceholder')}
              data-testid="chat-panel-skill-select-search-input"
            />
          </div>
        </div>
      }
      footer={{ label: t('chat.skillsManage'), onClick: handleOpenSkillsPage }}
    >
      {loading && (
        <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="loading">{t('skills.detailLoading')}</div>
      )}
      {!loading && errorMessage && (
        <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="error">{errorMessage}</div>
      )}
      {!loading && !errorMessage && installedSkills.length === 0 && (
        <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="no-installed">{t(isTeamMode ? 'chat.noInstalledSwarmSkills' : 'chat.noInstalledSkills')}</div>
      )}
      {!loading && !errorMessage && installedSkills.length > 0 && filteredSkills.length === 0 && (
        <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="no-matches">{t('skills.noMatches')}</div>
      )}
      {!loading && !errorMessage && filteredSkills.map((skill) => {
        const avatar = getSkillAvatar(skill.name);
        const isSelected = selectedSkills.includes(skill.name);
        return (
          <button
            type="button"
            key={skill.name}
            onClick={() => handleToggleSkill(skill.name)}
            className={clsx(
              'chat-skill-select__item',
              isSelected && 'chat-skill-select__item--selected',
            )}
            aria-pressed={isSelected}
            data-testid="chat-panel-skill-select-item"
            data-variant={skill.name}
            data-tooltip={skill.description || t('skills.noDescription')}
            {...handlers}
          >
            <div className="chat-skill-select__item-main" data-testid="chat-panel-skill-select-item-main">
              <div className="chat-skill-select__item-head">
                <div className={`chat-skill-select__avatar ${avatar.color}`} data-testid="chat-panel-skill-select-item-avatar">
                  {avatar.firstChar}
                </div>
                <div className="chat-skill-select__item-name" data-testid="chat-panel-skill-select-item-name">{skill.display_name || skill.name}</div>
              </div>
              <div className="chat-skill-select__item-desc" data-testid="chat-panel-skill-select-item-desc">
                {skill.description || t('skills.noDescription')}
              </div>
            </div>
            {isSelected && (
              <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
              </svg>
            )}
          </button>
        );
      })}
    </PickerPanel>
    {tooltip}
    </>
  );
}
