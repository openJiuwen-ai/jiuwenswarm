/**
 * GoalBar — 持续目标（Goal）吸附条
 *
 * 位置：ChatPanel 的 chat-compose 区域内，InteractionSlot（授权/交互卡）之后、
 * InputArea 之前。视觉上是紧贴输入框的吸附条（参照 InteractionSlot.css
 * `.interaction-slot--attached` / ChatPanel.css `.chat-active-team-group` 共用的公式：
 * `width: calc(100% - 48px); margin: 0 auto -1px;` + `border-radius: 18px 18px 0 0`），
 * 不是独立悬浮卡片。
 *
 * 只在存在 goal 时渲染（不管 goal 是通过 "+" 菜单 armed 流程、还是聊天里直接说明、还是
 * 后端自动识别设置的，只要 goalStore 里有数据就显示——armed 态本身不在这里处理，那是
 * InputArea 工具栏"目标"标签的职责，见 InputArea.tsx）。goal 清除时自动消失。
 *
 * 后端协议：一个 session 同时最多一个 Goal，见
 * cjh/goal/Goal持续目标Web前端对接.md。真机联调受限于后端未实现，见 progress.md 待办清单。
 */

import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Pause, Pencil, Play, Target, Trash2 } from 'lucide-react';
import { useChatStore, useGoalStore, useSessionStore } from '../../stores';
import type { GoalRecord, GoalStatus } from '../../types';
import { EditGoalModal } from './EditGoalModal';
import './GoalBar.css';

interface GoalBarProps {
  onSetGoal: (sessionId: string, objective: string) => void;
  onPauseGoal: (sessionId: string) => void;
  onResumeGoal: (sessionId: string) => void;
  onClearGoal: (sessionId: string) => void;
}

/** disconnected/unknown 是纯前端展示态，跟后端 goal.status 无关，见真实环境联调方案。 */
type DisplayTone = 'active' | 'paused' | 'completed' | 'blocked' | 'disconnected' | 'unknown';

const STATUS_TONE: Record<string, DisplayTone> = {
  active: 'active',
  paused: 'paused',
  completed: 'completed',
  blocked: 'blocked',
};

// 暂时隐藏暂停/恢复按钮的展示入口，功能逻辑（onPauseGoal/onResumeGoal 等）保留不动，
// 后续要恢复展示只需把这个开关改回 true。
const SHOW_PAUSE_RESUME_BUTTON = false;

function formatSeconds(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainSeconds = seconds % 60;
  return remainSeconds > 0 ? `${minutes}m ${remainSeconds}s` : `${minutes}m`;
}

// 旧口径兜底：后端没下发 time_used_seconds 时，退回"创建到现在"的总时长近似
// （created_at 后端不下发，实际几乎总是走 localCreatedAt，见 goalStore.ts 里的说明）。
function formatElapsedFallback(createdAtIso: string | undefined, now: number): string {
  if (!createdAtIso) return '0s';
  const startMs = new Date(createdAtIso).getTime();
  return formatSeconds((now - startMs) / 1000);
}

// 新口径：time_used_seconds 是后端已结算的累计耗时，active_started_at 是当前 active 计时段
// 的起点；只有 active 时才叠加"从 active_started_at 到浏览器当前时间"这段实时增量。
// 见 Goal持续目标Web前端对接4.md「前端耗时展示对接」。
function formatElapsedFromGoal(goal: GoalRecord, now: number): string {
  const base = goal.time_used_seconds ?? 0;
  if (goal.status !== 'active' || !goal.active_started_at) return formatSeconds(base);
  const activeStartMs = Date.parse(goal.active_started_at);
  const activeElapsed = Number.isNaN(activeStartMs) ? 0 : Math.max(0, Math.floor((now - activeStartMs) / 1000));
  return formatSeconds(base + activeElapsed);
}

export function GoalBar({ onSetGoal, onPauseGoal, onResumeGoal, onClearGoal }: GoalBarProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const goal = useGoalStore((s) => s.runtimes[activeSessionId ?? '']?.goal ?? null);
  const pendingAction = useGoalStore((s) => s.runtimes[activeSessionId ?? '']?.pendingAction ?? null);
  const queryStatus = useGoalStore((s) => s.runtimes[activeSessionId ?? '']?.queryStatus ?? 'ok');
  // 前后端连接是否正常，复用 webClient 原生 WS 状态机的镜像（不是心跳消息判断，见真实环境
  // 联调方案）——跟专门播报 agent 心跳的 connection.heartbeat 是两回事。
  const isConnected = useSessionStore((s) => s.isConnected);
  // 后端不下发 created_at（backend-requests.md #2），优先用真实值，没有时退回本地兜底时间
  const localCreatedAt = useGoalStore((s) => (goal ? s.localCreatedAt[goal.goal_id] : undefined));
  // completed 目标的 GoalBar 显隐单独用这个标记控制，不再靠 goal 是否存在于 store 里判断——
  // goal 数据本身要一直保留（"设为目标"徽章等消费方依赖 goal.objective），见 useWebSocket.ts
  // 的 applyIncomingGoal 注释。
  const bannerHidden = useGoalStore((s) => (goal ? Boolean(s.bannerHiddenGoalIds[goal.goal_id]) : false));
  const [editing, setEditing] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  // 暂停/恢复点击后的乐观图标翻转：只影响图标选哪个，不影响按钮是否可点（仍然沿用下面的
  // isDisabled 整组置灰）、也不影响状态文字颜色（那个继续等权威数据）。pendingAction 落地
  // （不管成功还是失败兜底完）后清空，让真实 goal.status 接管。
  const [optimisticPausable, setOptimisticPausable] = useState<boolean | null>(null);

  useEffect(() => {
    // 不管有没有后端计时字段，只有 active 才跳字——paused/blocked/completed 一律冻结在当前
    // 展示值上，不再继续走（就算是没有 time_used_seconds 兜底走旧口径的场景，也不该在非
    // active 期间继续累加）。
    if (!goal || goal.status !== 'active') return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [goal]);

  useEffect(() => {
    if (pendingAction === null) setOptimisticPausable(null);
  }, [pendingAction]);

  // 编辑弹窗打开期间目标在后台跑完了（用户可能没注意到，弹窗内容也不会自动同步最新状态）——
  // 与其等自动隐藏把弹窗连带 GoalBar 一起悄悄拽没（那样用户完全不知道发生了什么，编辑内容
  // 也白写了），不如一发现就主动关掉并提示，避免用户点保存把一个已经结束的目标悄悄"复活"。
  useEffect(() => {
    if (editing && goal?.status === 'completed' && activeSessionId) {
      setEditing(false);
      useChatStore.getState().addMessage(activeSessionId, {
        id: `error-${Date.now()}`,
        role: 'system',
        content: t('goal.editStaleWarning'),
        timestamp: new Date().toISOString(),
      });
    }
  }, [editing, goal?.status, activeSessionId, t]);

  // 没有目标（goal 为 null）时严格"不用管"——即使当前处于断联/查询失败，也不展示任何占位条；
  // 只有"这个会话之前已经查到过目标"（goal 非空，数据在断联/查询失败期间原样保留，不会被清空）
  // 才叠加 disconnected/unknown 展示态。goal 是否非空天然承担了这个判断，不需要额外的标记位。
  if (!activeSessionId || !goal) return null;
  if (goal.status === 'completed' && bannerHidden) return null;

  // completed 是终态，不需要叠加断联/未知态（已完成的目标不再自动更新，见真实环境联调方案 9a）。
  const degraded: 'disconnected' | 'unknown' | null =
    goal.status === 'completed'
      ? null
      : !isConnected
        ? 'disconnected'
        : queryStatus === 'unknown'
          ? 'unknown'
          : null;

  const tone: DisplayTone = degraded ?? STATUS_TONE[goal.status as GoalStatus] ?? 'active';
  const isPausable = goal.status === 'active';
  const isResumable = goal.status === 'paused' || goal.status === 'blocked';
  const isBusy = pendingAction !== null;
  const isDisabled = isBusy || degraded !== null;
  // 图标显示用的"是否展示为可暂停"：乐观值优先，否则用真实状态。
  const displayAsPausable = optimisticPausable ?? isPausable;

  return (
    <div className="goal-bar-attached" data-testid="goal-bar-attached">
      <div className={`goal-bar goal-bar--tone-${tone}`} data-testid="goal-bar" data-variant={tone}>
        <Target size={16} strokeWidth={2} className="goal-bar__icon" data-testid="goal-bar-icon" />
        <div className="goal-bar__main" data-testid="goal-bar-main">
          <span className={`goal-bar__status goal-bar__status--${tone}`} data-testid="goal-bar-status" data-variant={tone}>
            {t(`goal.status.${degraded ?? goal.status}`, degraded ?? goal.status)}
          </span>
          <span className="goal-bar__objective" title={goal.objective} data-testid="goal-bar-objective">
            {goal.objective}
          </span>
          <span className="goal-bar__elapsed" data-testid="goal-bar-elapsed">
            ·{' '}
            {goal.time_used_seconds !== undefined
              ? formatElapsedFromGoal(goal, now)
              : formatElapsedFallback(goal.created_at ?? localCreatedAt, now)}
          </span>
        </div>
        <div className="goal-bar__actions" data-testid="goal-bar-actions">
          {goal.status !== 'completed' && (
            <button
              type="button"
              title={t('goal.action.editTooltip')}
              className="goal-bar__action-btn"
              disabled={isDisabled}
              onClick={() => setEditing(true)}
              data-testid="goal-bar-edit-button"
            >
              <Pencil size={14} strokeWidth={2} />
            </button>
          )}
          {SHOW_PAUSE_RESUME_BUTTON && (isPausable || isResumable) && (
            <button
              type="button"
              title={displayAsPausable ? t('goal.action.pauseTooltip') : t('goal.action.resumeTooltip')}
              className="goal-bar__action-btn"
              disabled={isDisabled}
              onClick={() => {
                if (isPausable) {
                  setOptimisticPausable(false);
                  onPauseGoal(activeSessionId);
                } else {
                  setOptimisticPausable(true);
                  onResumeGoal(activeSessionId);
                }
              }}
              data-testid="goal-bar-pause-resume-button"
              data-variant={displayAsPausable ? 'pause' : 'resume'}
            >
              {displayAsPausable ? <Pause size={14} strokeWidth={2} /> : <Play size={14} strokeWidth={2} />}
            </button>
          )}
          <button
            type="button"
            title={t('goal.action.deleteTooltip')}
            className="goal-bar__action-btn goal-bar__action-btn--danger"
            disabled={isDisabled}
            onClick={() => onClearGoal(activeSessionId)}
            data-testid="goal-bar-delete-button"
          >
            <Trash2 size={14} strokeWidth={2} />
          </button>
        </div>
      </div>

      {editing && (
        <div data-testid="goal-bar-edit-modal-slot">
          <EditGoalModal
            initialObjective={goal.objective}
            onCancel={() => setEditing(false)}
            onSave={(objective) => {
              // 防御性兜底：正常情况下上面那个 useEffect 会在目标转 completed 的瞬间就关掉弹窗，
              // 这里理论上不会命中，但保存动作本身是不可逆的（会把已完成的目标复活），多一层检查
              // 成本很低。
              const latestGoal = useGoalStore.getState().runtimes[activeSessionId]?.goal;
              if (!latestGoal || latestGoal.goal_id !== goal.goal_id || latestGoal.status === 'completed') {
                setEditing(false);
                useChatStore.getState().addMessage(activeSessionId, {
                  id: `error-${Date.now()}`,
                  role: 'system',
                  content: t('goal.editStaleWarning'),
                  timestamp: new Date().toISOString(),
                });
                return;
              }
              onSetGoal(activeSessionId, objective);
              setEditing(false);
            }}
          />
        </div>
      )}
    </div>
  );
}
