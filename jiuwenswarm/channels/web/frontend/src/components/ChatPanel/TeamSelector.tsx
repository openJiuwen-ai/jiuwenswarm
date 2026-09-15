/**
 * "集群模式"旁边的 team 下拉列表。
 *
 * 一个 session 可以同时挂多个 team（openjiuwen 的 TeamRuntimePool 以 team_name 为键、
 * 按 current_session_id 归属）。这里展示当前 session 在运行时生成/挂载的 team，切换时
 * 由 teamSelectorStore.selectTeam 拉该 team 的 team.snapshot 并灌回 SessionRuntime，
 * 于是成员列表、任务板、推理内容都跟着切。
 *
 * 交互样式刻意复用 chat-mode-select 那一套（触发按钮 + portal 菜单 + 上/下翻方向兜底），
 * 两个下拉并排放置时视觉一致。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Users } from 'lucide-react';
import { useTeamSelectorStore, type RuntimeTeamInfo } from '../../stores';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';

const MENU_GAP = 10;
const MENU_MAX_HEIGHT = 260;
const MENU_WIDTH = 260;

/** 运行中重拉 team 列表的间隔。team 建成到出现在 pool 里是秒级的，2s 足够跟手又不吵后端。 */
const TEAM_POLL_MS = 2000;

function resolveMenuDirection(anchorBottom: number, menuHeight: number) {
  const spaceBelow = window.innerHeight - anchorBottom - MENU_GAP;
  if (spaceBelow >= menuHeight) return 'down' as const;
  return 'up' as const;
}

/** 后端给的是 'running' / 'pending' / 'configured' 之类的原始字符串，未知值一律按灰点处理。 */
function stateTone(state: string): 'running' | 'idle' | 'unknown' {
  const normalized = String(state || '').toLowerCase();
  if (normalized === 'running' || normalized === 'active') return 'running';
  if (normalized === 'pending' || normalized === 'paused' || normalized === 'configured') {
    return 'idle';
  }
  return 'unknown';
}

/**
 * 下拉展示名优先用唯一的 team_id：多个 org leader 常共用同一个 agent card.name
 * （例如都叫 "Market Research and Data Analysis"），只用 display_name 会看起来像只有一项。
 */
function teamLabel(team: RuntimeTeamInfo, fallback: string): string {
  const id = (team.team_id || team.team_name || '').trim();
  const leader = team.leader_id?.trim();
  if (id && leader && leader !== id) return `${id} · ${leader}`;
  return id || team.display_name?.trim() || fallback;
}

export interface TeamSelectorProps {
  sessionId: string | null;
  /** 当前 session 是否正在跑一轮。运行中会周期性重拉 team 列表：team 是这一轮里由 org 工具现建的。 */
  isProcessing?: boolean;
}

export function TeamSelector({ sessionId, isProcessing = false }: TeamSelectorProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const [direction, setDirection] = useState<'up' | 'down'>('up');
  const containerRef = useRef<HTMLDivElement>(null);
  const portalRef = useRef<HTMLDivElement>(null);

  const runtime = useTeamSelectorStore((s) => (sessionId ? s.runtimes[sessionId] : undefined));
  const teamCount = runtime?.teams.length ?? 0;
  const fetchTeams = useTeamSelectorStore((s) => s.fetchTeams);
  const selectTeam = useTeamSelectorStore((s) => s.selectTeam);
  const removeRuntime = useTeamSelectorStore((s) => s.removeRuntime);

  // 只有真正的会话才去问后端；新会话（NEW_CONVERSATION_ID）与空态直接跳过。
  const isRealSession = Boolean(
    sessionId && sessionId !== 'new' && sessionId !== NEW_CONVERSATION_ID,
  );

  // 首次拿到 team 列表后，把默认项也真正"选中"一次：store 里 fetchTeams 已经根据
  // 列表把 selectedTeamId 定下来了，但那只影响事件过滤，视图数据还得靠 team.snapshot
  // 灌一次，否则进历史会话（成员/任务事件早就不推了）只会看到空面板。
  const autoSelectedRef = useRef<string | null>(null);

  useEffect(() => {
    if (!isRealSession || !sessionId) return;
    if (runtime?.loaded !== true) return;
    const teamId = runtime.selectedTeamId;
    if (!teamId) return;
    if (autoSelectedRef.current === `${sessionId}:${teamId}`) return;
    autoSelectedRef.current = `${sessionId}:${teamId}`;
    void selectTeam(sessionId, teamId);
  }, [isRealSession, runtime?.loaded, runtime?.selectedTeamId, sessionId, selectTeam]);

  useEffect(() => {
    if (!isRealSession || !sessionId) return;
    void fetchTeams(sessionId);
  }, [isRealSession, sessionId, fetchTeams]);

  /**
   * 运行期轮询。
   *
   * team 不是会话一开始就存在的：集群模式下 agent 会在这一轮里陆续调
   * org_create_and_invite_expert_team 现建多个 team。owner 先入 pool 时列表已非空，
   * 若此时停轮询，后面的 expert team 要等本轮结束才出现在下拉里。
   * 因此整个 isProcessing 期间都按 POLL_MS 重拉；结束后再收一次尾。
   */
  useEffect(() => {
    if (!isRealSession || !sessionId) return;
    if (!isProcessing) return;
    const timer = window.setInterval(() => {
      void fetchTeams(sessionId);
    }, TEAM_POLL_MS);
    return () => window.clearInterval(timer);
  }, [isRealSession, sessionId, isProcessing, fetchTeams]);

  // 一轮结束时再收一次尾：建 team 的工具可能刚好在 isProcessing 翻 false 的同一拍完成，
  // 轮询来不及看到。
  const wasProcessingRef = useRef(false);
  useEffect(() => {
    if (!isRealSession || !sessionId) return;
    const wasProcessing = wasProcessingRef.current;
    wasProcessingRef.current = isProcessing;
    if (wasProcessing && !isProcessing) void fetchTeams(sessionId);
  }, [isRealSession, sessionId, isProcessing, fetchTeams]);

  // 会话被真正清掉时顺手回收运行态，避免 runtimes 表无限增长。
  useEffect(() => {
    if (!sessionId) return;
    return () => {
      removeRuntime(sessionId);
    };
  }, [sessionId, removeRuntime]);

  const selected = useMemo(() => {
    const teams = runtime?.teams ?? [];
    if (!teams.length) return null;
    return (
      teams.find((team) => team.team_id === runtime?.selectedTeamId) ??
      teams.find((team) => team.team_id === runtime?.defaultTeamId) ??
      teams[0]
    );
  }, [runtime?.teams, runtime?.selectedTeamId, runtime?.defaultTeamId]);

  const openMenu = useCallback(() => {
    if (containerRef.current) {
      const rect = containerRef.current.getBoundingClientRect();
      setDirection(resolveMenuDirection(rect.bottom, MENU_MAX_HEIGHT));
      setAnchor(rect);
    }
    setOpen((prev) => !prev);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null;
      if (!target) return;
      if (containerRef.current?.contains(target)) return;
      if (portalRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const teams = runtime?.teams ?? [];
  const loading = runtime?.loading ?? false;
  // 没拉到 team 时整体不渲染：集群模式的老会话/未启用 team 的场景不该多出一个空下拉。
  if (!loading && teams.length === 0) return null;

  const label = selected
    ? teamLabel(selected, t('chat.teamSelect.fallbackLabel'))
    : t('chat.teamSelect.loadingLabel');

  return (
    <div
      ref={containerRef}
      className={clsx('chat-mode-select chat-team-select', open && 'chat-mode-select--open')}
      data-testid="chat-panel-team-select"
    >
      <button
        type="button"
        className="chat-mode-select__trigger"
        data-testid="chat-panel-team-select-trigger"
        data-variant={selected ? stateTone(String(selected.state)) : 'unknown'}
        onClick={openMenu}
        aria-haspopup="menu"
        aria-expanded={open}
        title={
          teamCount > 1
            ? `${t('chat.teamSelect.tooltip')} · ${teamCount}`
            : (selected?.team_id ?? undefined)
        }
      >
        <span className="chat-mode-select__value" data-testid="chat-panel-team-select-value">
          <span className="chat-mode-select__icon" aria-hidden="true">
            <TeamStateDot state={String(selected?.state ?? '')} />
          </span>
          <span className="chat-mode-select__label">{label}</span>
        </span>
        <svg
          className="chat-mode-select__chevron"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.8}
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
        </svg>
      </button>

      {open && anchor && createPortal(
        <div
          ref={portalRef}
          className="chat-mode-select__menu"
          role="menu"
          data-testid="chat-panel-team-select-menu"
          style={
            direction === 'up'
              ? {
                  position: 'fixed',
                  bottom: window.innerHeight - anchor.top + MENU_GAP,
                  left: anchor.left,
                  width: MENU_WIDTH,
                  maxHeight: MENU_MAX_HEIGHT,
                  overflowY: 'auto',
                  zIndex: 9999,
                }
              : {
                  position: 'fixed',
                  top: anchor.bottom + MENU_GAP,
                  left: anchor.left,
                  width: MENU_WIDTH,
                  maxHeight: MENU_MAX_HEIGHT,
                  overflowY: 'auto',
                  zIndex: 9999,
                }
          }
        >
          {teams.map((team) => {
            const isActive = selected?.team_id === team.team_id;
            return (
              <button
                type="button"
                key={team.team_id}
                className={clsx(
                  'chat-mode-select__option',
                  isActive && 'chat-mode-select__option--active',
                )}
                role="menuitemradio"
                aria-checked={isActive}
                data-testid="chat-panel-team-select-option"
                data-team-id={team.team_id}
                data-team-state={team.state}
                title={team.team_id}
                onClick={() => {
                  if (sessionId) void selectTeam(sessionId, team.team_id);
                  setOpen(false);
                }}
              >
                <span className="chat-mode-select__option-main">
                  <span className="chat-mode-select__icon" aria-hidden="true">
                    <TeamStateDot state={String(team.state ?? '')} />
                  </span>
                  <span className="chat-mode-select__label">{teamLabel(team, team.team_id)}</span>
                </span>
                {isActive && (
                  <svg
                    className="chat-mode-select__check"
                    viewBox="0 0 20 20"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={2}
                    aria-hidden="true"
                  >
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>,
        document.body,
      )}
    </div>
  );
}

/** 状态点：running 绿、pending/configured 灰，未知状态退回 Users 图标。 */
function TeamStateDot({ state }: { state: string }) {
  const tone = stateTone(state);
  if (tone === 'unknown') {
    return <Users className="w-4 h-4" />;
  }
  return (
    <span
      className={clsx('chat-team-select__dot', `chat-team-select__dot--${tone}`)}
      data-testid="chat-panel-team-select-dot"
      data-tone={tone}
    />
  );
}

export default TeamSelector;
