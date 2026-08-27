import { useCallback, useEffect, useMemo, useState, type CSSProperties, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { useChatStore, useSessionStore } from '../../stores';
import { webRequest } from '../../services/webClient';
import { getSkillAvatar } from '../../utils/skillAvatar';

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

const PANEL_WIDTH = 336;
const PANEL_MAX_HEIGHT = 358;
const GAP = 8;
const LIST_ROW_HEIGHT = 50;
const LIST_ROW_GAP = 4;
const LIST_VISIBLE_ROWS = 5;

function listContentHeight(itemCount: number): number {
  if (itemCount === 0) return 0;
  return itemCount * LIST_ROW_HEIGHT + (itemCount - 1) * LIST_ROW_GAP;
}

const LIST_MAX_HEIGHT = listContentHeight(LIST_VISIBLE_ROWS);

interface SkillPickerPanelProps {
  /** "技能"菜单项的定位锚点——面板紧贴它右侧展开，右侧空间不够退化到下方。 */
  anchorRect: DOMRect;
  onClose: () => void;
  /** 挂到面板根节点——由调用方（InputArea）持有，好在一级"+"菜单自己的 outside-click 判断里
   * 把这个 portal 出去的二级面板也算作"菜单内部"，否则点二级面板（搜索框/技能项）会被一级菜单
   * 的监听器误判成"点了外面"直接把整个"+"菜单收起。 */
  panelRef: RefObject<HTMLDivElement>;
  /** 单 agent → skill_type==='skill'；集群 → skill_type==='swarm_skill' */
  isTeamMode: boolean;
  onNavigateToSkills?: () => void;
  onInsertSkill?: (skillName: string) => void;
  onRemoveSkill?: (skillName: string) => void;
}

/**
 * "+"菜单"技能"项的二级面板：搜索 + 已安装技能列表，选中态写入 sessionStore.selectedSkills
 * （随消息发送），结构与原来挂在工具条上的 SkillSelector 一致，只是搬进了"+"菜单。
 */
export function SkillPickerPanel({
  anchorRect,
  onClose,
  panelRef,
  isTeamMode,
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

  const matchedListHeight = Math.min(listContentHeight(filteredSkills.length), LIST_MAX_HEIGHT);
  const listBoxHeight: CSSProperties | undefined =
    matchedListHeight > 0 ? { height: matchedListHeight } : undefined;

  const spaceRight = window.innerWidth - anchorRect.right;
  const openRight = spaceRight >= PANEL_WIDTH + GAP * 2;
  const clampTop = (value: number) =>
    Math.min(Math.max(GAP, value), Math.max(GAP, window.innerHeight - PANEL_MAX_HEIGHT - GAP));
  const clampLeft = (value: number) =>
    Math.min(Math.max(GAP, value), Math.max(GAP, window.innerWidth - PANEL_WIDTH - GAP));
  const style: CSSProperties = openRight
    ? { position: 'fixed', left: anchorRect.right + GAP, top: clampTop(anchorRect.top), zIndex: 9999 }
    : { position: 'fixed', left: clampLeft(anchorRect.left), top: clampTop(anchorRect.bottom + GAP), zIndex: 9999 };

  return createPortal(
    <div ref={panelRef} className="chat-skill-picker" style={style} role="menu">
      {/* 顶部搜索框 */}
      <div className="chat-skill-select__search" data-testid="chat-panel-skill-select-search">
        <svg className="chat-skill-select__search-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM17.5 17.5l-3.7-3.7" />
        </svg>
        <input
          type="text"
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder={t('chat.skillsSearchPlaceholder')}
          className="chat-skill-select__search-input"
          data-testid="chat-panel-skill-select-search-input"
        />
      </div>

      {loading && (
        <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="loading">{t('skills.detailLoading')}</div>
      )}
      {!loading && errorMessage && (
        <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="error">{errorMessage}</div>
      )}
      {!loading && !errorMessage && installedSkills.length === 0 && (
        <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="no-installed" style={listBoxHeight}>{t(isTeamMode ? 'chat.noInstalledSwarmSkills' : 'chat.noInstalledSkills')}</div>
      )}
      {!loading && !errorMessage && installedSkills.length > 0 && filteredSkills.length === 0 && (
        <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="no-matches" style={listBoxHeight}>{t('skills.noMatches')}</div>
      )}
      {!loading && !errorMessage && filteredSkills.length > 0 && (
        <div className="chat-skill-select__list" style={listBoxHeight} data-testid="chat-panel-skill-select-list">
          {filteredSkills.map((skill) => {
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
                title={isSelected ? t('chat.skillsRemove') : t('chat.skillsAdd')}
              >
                <div className={`chat-skill-select__avatar ${avatar.color}`} data-testid="chat-panel-skill-select-item-avatar">
                  {avatar.firstChar}
                </div>
                <div className="chat-skill-select__item-main" data-testid="chat-panel-skill-select-item-main">
                  <div className="chat-skill-select__item-name" data-testid="chat-panel-skill-select-item-name">{skill.display_name || skill.name}</div>
                  <div className="chat-skill-select__item-desc" data-testid="chat-panel-skill-select-item-desc">
                    {skill.description || t('skills.noDescription')}
                  </div>
                </div>
                {isSelected && (
                  <svg className="chat-skill-select__item-check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2.2} aria-hidden="true">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>
      )}

      {/* 底部「技能管理」入口 */}
      <div className="chat-skill-select__footer" data-testid="chat-panel-skill-select-footer">
        <button
          type="button"
          onClick={handleOpenSkillsPage}
          className="chat-skill-select__manage-btn"
          data-testid="chat-panel-skill-select-manage"
        >
          <span className="chat-config-icon chat-config-icon--settings chat-skill-select__manage-icon" aria-hidden="true" />
          <span>{t('chat.skillsManage')}</span>
        </button>
      </div>
    </div>,
    document.body,
  );
}
