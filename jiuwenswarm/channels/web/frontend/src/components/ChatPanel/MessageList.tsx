import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import clsx from 'clsx';
import { LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Message, ToolExecution } from '../../types';
import { MessageItem } from './MessageItem';
import { ToolGroupDisplay } from './ToolGroupDisplay';
import { useNow, formatDurationPrecise } from './chatTimelineClock';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import WaitingStatusIcon from '../../assets/work-mode/status-waiting.svg?react';
import { AgentAvatar } from '../AgentAvatar';
import { useChatStore, useSessionStore } from '../../stores';
import type { AgentGroupIdentity } from '../../features/agentManagement';
import type { TeamLeaderIdentity } from '../../features/teamLeaderIdentity';
import type { ReasoningSegment } from '../../stores/chatStore';
import { filterPublishedHistoryBatch } from '../../features/historyPagination';
import { projectTimelineItems, type TimelineDisplayItem } from '../../features/chatTimeline/projectTimelineItems';
import { VirtualTimeline, type TimelineViewportState } from './VirtualTimeline';
import { TimelineRowStateProvider, useTimelineRowState } from './timelineRowState';
import {
  buildTimelineItems,
  buildRenderItems,
  buildTurnWorkMeta,
  buildTurnFoldAnchorKeys,
  buildLiveCompletedStreaks,
  buildStreakInputSignature,
  isSettlingForStreak,
  streakMapFingerprint,
  formatStreakSummaryLabel,
  completedWorkDurationMs,
  turnElapsedRangeMs,
  REASONING_COLLAPSE_DELAY_MS,
  STREAK_FOLD_TRANSITION_DELAY_MS,
  type LiveWorkStreak,
} from '../../features/chatTimeline/buildTurnTimeline';

const EMPTY_EXPANSIONS: Record<string, boolean> = {};
const EMPTY_REASONING: ReasoningSegment[] = [];

interface MessageListProps {
  messages: Message[];
  sessionId?: string;
  renderAfterMessage?: (message: Message) => ReactNode;
  canLoadOlderHistory?: boolean;
  onLoadOlderHistory?: () => void | Promise<void>;
  teamLeaderIdentityOverride?: TeamLeaderIdentity | null;
  teamGroupIdentityOverride?: AgentGroupIdentity | null;
  onForkFromMessage?: (message: Message) => Promise<void>;
}

interface ChatTimelineListProps {
  messages: Message[];
  executions?: ToolExecution[];
  reasoningSegments?: ReasoningSegment[];
  /**
   * 历史文件/分享图等静态时间线：强制按「已完成」折叠，
   * 不依赖当前会话的 isProcessing / store 思考段。
   */
  staticTimeline?: boolean;
  /** 静态历史预览可逐批准入；分享导出保持完整静态 DOM。 */
  virtualized?: boolean;
  mode?: string;
  disableA2UIInteraction?: boolean;
  incrementalStaticRendering?: boolean;
  renderAfterMessage?: (message: Message) => ReactNode;
  teamLeaderIdentityOverride?: TeamLeaderIdentity | null;
  teamGroupIdentityOverride?: AgentGroupIdentity | null;
  onForkFromMessage?: (message: Message) => Promise<void>;
  /** 交互时间线按会话保存派生快照和逐批准入窗口。 */
  sessionId?: string | null;
  /** 内容不足一屏时继续发布已存在的更早历史。 */
  canLoadOlderHistory?: boolean;
  onLoadOlderHistory?: () => void | Promise<void>;
}

type TimelineDerivationInput = {
  sessionId: string | null;
  messages: Message[];
  executions: ToolExecution[];
  reasoningSegments: ReasoningSegment[];
  isTeamMode: boolean;
  isProcessing: boolean;
};

type TimelineRenderItems = ReturnType<typeof buildRenderItems>;

type TimelineDerivationCacheEntry = TimelineDerivationInput & {
  renderItems: TimelineRenderItems;
};

const timelineDerivationCache = new WeakMap<Message[], TimelineDerivationCacheEntry>();
const executionListCache = new WeakMap<
  Map<string, ToolExecution>,
  WeakMap<string[], ToolExecution[]>
>();

function deriveTimelineItems(
  input: TimelineDerivationInput
): TimelineRenderItems {
  const cached = timelineDerivationCache.get(input.messages);
  if (
    cached?.sessionId === input.sessionId &&
    cached.executions === input.executions &&
    cached.reasoningSegments === input.reasoningSegments &&
    cached.isTeamMode === input.isTeamMode &&
    cached.isProcessing === input.isProcessing
  ) {
    return cached.renderItems;
  }
  const renderItems = buildRenderItems(
    buildTimelineItems(input.messages, input.executions, input.reasoningSegments),
    input.isTeamMode,
    input.isProcessing
  );
  timelineDerivationCache.set(input.messages, { ...input, renderItems });
  return renderItems;
}

function getExecutionList(
  executions: Map<string, ToolExecution>,
  order: string[]
): ToolExecution[] {
  let byOrder = executionListCache.get(executions);
  if (!byOrder) {
    byOrder = new WeakMap();
    executionListCache.set(executions, byOrder);
  }
  const cached = byOrder.get(order);
  if (cached) {
    return cached;
  }
  const next = order
    .map((toolCallId) => executions.get(toolCallId))
    .filter((item): item is ToolExecution => Boolean(item));
  byOrder.set(order, next);
  return next;
}

function TeamLeaderDisplay({
  identity,
  className,
}: {
  identity?: TeamLeaderIdentity | null;
  className?: string;
}) {
  if (identity) {
    return <AgentAvatar identityOverride={identity} alt="" className={className} showName />;
  }
  return (
    <>
      <TeamMemberAvatar member="team_leader" className={className} />
      <span className="chat-avatar-name">Jiuwen</span>
    </>
  );
}

function TeamGroupDisplay({
  identity,
  leaderIdentity,
  className,
}: {
  identity?: AgentGroupIdentity | null;
  leaderIdentity?: TeamLeaderIdentity | null;
  className?: string;
}) {
  if (identity) {
    return (
      <AgentAvatar
        identityOverride={{
          agentTemplateId: identity.id,
          displayName: identity.displayName,
          ...(identity.avatarUrl ? { avatar: identity.avatarUrl } : {}),
        }}
        alt=""
        className={className}
        showName
      />
    );
  }
  return <TeamLeaderDisplay identity={leaderIdentity} className={className} />;
}

function formatElapsedCoarse(ms: number): string {
  const whole = Math.floor(Math.max(0, ms) / 1000);
  if (whole < 60) {
    return `${whole}s`;
  }
  const minutes = Math.floor(whole / 60);
  const seconds = whole % 60;
  return `${minutes}m${seconds.toString().padStart(2, '0')}s`;
}

/** 与 buildTurnTimeline 中异常回退阈值一致：超过则视为 startMs 脏数据。 */
const MAX_PLAUSIBLE_TURN_MS = 24 * 60 * 60 * 1000;

export function TurnElapsed({
  startMs,
  endMs,
  isLastTurn,
  isProcessing,
  showAvatar,
  agentTemplateName,
  teamLayout,
  teamLeaderIdentity,
  teamGroupIdentity,
}: {
  startMs: number;
  endMs: number;
  isLastTurn: boolean;
  isProcessing: boolean;
  showAvatar?: boolean;
  agentTemplateName?: string;
  teamLayout: boolean;
  teamLeaderIdentity?: TeamLeaderIdentity | null;
  teamGroupIdentity?: AgentGroupIdentity | null;
}) {
  const { t } = useTranslation();
  const active = isLastTurn && isProcessing;
  const now = useNow(active);
  const end = active ? now : endMs;
  const rawElapsed = Math.max(0, end - startMs);
  // 进行中若 startMs 异常偏旧，停用实时计时，避免一直飙到数小时。
  const elapsed =
    active && rawElapsed > MAX_PLAUSIBLE_TURN_MS
      ? Math.max(0, endMs - startMs) > MAX_PLAUSIBLE_TURN_MS
        ? 0
        : Math.max(0, endMs - startMs)
      : rawElapsed;
  const showActive = active && rawElapsed <= MAX_PLAUSIBLE_TURN_MS;
  if (!showActive && elapsed <= 0) {
    return null;
  }
  // 不带头像的独立时间行（如成员消息轮次）：team 模式下与 920px 栅格对齐。
  const timeLine = (
    <div
      className={clsx('turn-elapsed', !showAvatar && teamLayout && 'turn-elapsed--team', showActive && 'is-active')}
      data-testid="chat-panel-turn-elapsed"
      data-variant={showActive ? 'active' : 'finished'}
    >
      {showActive && (
        <LoaderCircle className="turn-elapsed__spinner" size={12} strokeWidth={2.2} aria-hidden="true" />
      )}
      <span className="turn-elapsed__label" data-testid="chat-panel-turn-elapsed-label">
        {showActive ? t('chatUi.turnRunning') : t('chatUi.turnElapsed')}
      </span>
      <span className="turn-elapsed__value" data-testid="chat-panel-turn-elapsed-value">
        {showActive ? formatElapsedCoarse(elapsed) : formatDurationPrecise(elapsed)}
      </span>
    </div>
  );
  if (!showAvatar) {
    return timeLine;
  }
  // 与折叠条同构：头像 + 名称在第一行，耗时行紧随其下。
  return (
    <div className={clsx('completed-work-col', teamLayout && 'completed-work-col--team')} data-testid="chat-panel-turn-elapsed-block">
      <div className="completed-work-col__avatar pt-0.5">
        {!teamLayout && agentTemplateName ? (
          <AgentAvatar agentId={agentTemplateName} alt="" className="h-7 w-7" showName />
        ) : (
          <TeamGroupDisplay identity={teamGroupIdentity} leaderIdentity={teamLeaderIdentity} className="h-7 w-7" />
        )}
      </div>
      {timeLine}
    </div>
  );
}

function CompletedWorkChip({
  variant,
  thinkingCount = 0,
  toolCount = 0,
  outcomeTone = 'neutral',
  expanded,
  onToggle,
  showAvatar,
  teamLayout,
  elapsedMs = 0,
  agentTemplateName,
  teamLeaderIdentity,
  teamGroupIdentity,
}: {
  variant: 'turn' | 'streak';
  thinkingCount?: number;
  toolCount?: number;
  outcomeTone?: 'success' | 'partial' | 'error' | 'neutral';
  expanded: boolean;
  onToggle: () => void;
  showAvatar: boolean;
  teamLayout: boolean;
  elapsedMs?: number;
  agentTemplateName?: string;
  teamLeaderIdentity?: TeamLeaderIdentity | null;
  teamGroupIdentity?: AgentGroupIdentity | null;
}) {
  const { t } = useTranslation();
  // 耗时并入 turn 折叠条文案（原底部 TurnElapsed 已移除），位置唯一不再打架。
  const label =
    variant === 'turn'
      ? elapsedMs > 0
        ? `${t('chatUi.turnElapsed')} ${formatDurationPrecise(elapsedMs)}`
        : t('chatUi.workCompletedFallback')
      : formatStreakSummaryLabel(t, thinkingCount, toolCount, outcomeTone);
  // 图标统一用 status-waiting 时钟资源，状态色仍由 is-success/is-partial/is-error 通过 currentColor 区分。
  const applyOutcome = variant === 'streak';
  const toneClass = !applyOutcome
    ? 'is-success'
    : outcomeTone === 'error'
      ? 'is-error'
      : outcomeTone === 'partial'
        ? 'is-partial'
        : 'is-success';

  const chip = (
    <button
      type="button"
      className={clsx(
        'completed-work-chip',
        variant === 'streak' && 'completed-work-chip--streak',
        expanded && 'is-expanded',
        toneClass
      )}
      onClick={onToggle}
      aria-expanded={expanded}
      data-testid="chat-panel-completed-work-chip"
      data-variant={variant}
    >
      <span className={clsx('completed-work-chip__icon', toneClass)} aria-hidden="true" data-testid="chat-panel-completed-work-chip-icon">
        <WaitingStatusIcon />
      </span>
      <span className="completed-work-chip__label" data-testid="chat-panel-completed-work-chip-label">{label}</span>
      <span className={clsx('tool-tree-item__disclosure', expanded && 'is-open')} aria-hidden="true">
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
          <path strokeLinecap="round" strokeLinejoin="round" d="m8 6 4 4-4 4" />
        </svg>
      </span>
    </button>
  );

  if (teamLayout) {
    return (
      <div
        className={clsx(
          'completed-work-col',
          'completed-work-col--team',
          variant === 'streak' && 'completed-work-col--nested'
        )}
      >
        {showAvatar ? (
          <div className="completed-work-col__avatar pt-0.5">
            <TeamGroupDisplay identity={teamGroupIdentity} leaderIdentity={teamLeaderIdentity} className="h-7 w-7" />
          </div>
        ) : null}
        {chip}
      </div>
    );
  }

  return (
    <div
      className={clsx(
        'completed-work-col',
        variant === 'streak' && 'completed-work-col--nested'
      )}
    >
      {showAvatar ? (
        <div className="completed-work-col__avatar">
          {agentTemplateName ? (
            <AgentAvatar agentId={agentTemplateName} alt="" className="h-7 w-7" showName />
          ) : (
            <TeamLeaderDisplay identity={teamLeaderIdentity} className="h-7 w-7" />
          )}
        </div>
      ) : null}
      {chip}
    </div>
  );
}

function ReasoningSegmentBlock({
  segment,
  agentTemplateName,
  showAvatar,
  teamLayout,
  teamLeaderIdentity,
  teamGroupIdentity,
}: {
  segment: ReasoningSegment;
  agentTemplateName?: string;
  showAvatar: boolean;
  teamLayout: boolean;
  teamLeaderIdentity?: TeamLeaderIdentity | null;
  teamGroupIdentity?: AgentGroupIdentity | null;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useTimelineRowState('reasoning-open', !segment.closed);
  const [userToggled, setUserToggled] = useTimelineRowState('reasoning-user-toggled', false);
  const userToggledRef = useRef(userToggled);
  userToggledRef.current = userToggled;
  const prevClosedRef = useRef(segment.closed);
  const bodyRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);

  useEffect(() => {
    if (!prevClosedRef.current && segment.closed && !userToggledRef.current) {
      const timer = window.setTimeout(() => {
        if (!userToggledRef.current) {
          setOpen(false);
        }
      }, REASONING_COLLAPSE_DELAY_MS);
      prevClosedRef.current = segment.closed;
      return () => window.clearTimeout(timer);
    }
    prevClosedRef.current = segment.closed;
    return undefined;
  }, [segment.closed, setOpen]);

  const body = segment.text.replace(/\n{3,}/g, '\n\n').trim();

  useEffect(() => {
    if (!open || segment.closed) {
      return;
    }
    const el = bodyRef.current;
    if (!el || !autoScrollRef.current) {
      return;
    }
    el.scrollTop = el.scrollHeight;
  }, [open, segment.closed, body]);

  if (!body) {
    return null;
  }
  const running = !segment.closed;

  const content = (
    <div className="min-w-0 reasoning-panel" data-testid="chat-panel-reasoning-panel">
      <button
        type="button"
        className="tool-tree__header"
        onClick={() => {
          userToggledRef.current = true;
          setUserToggled(true);
          setOpen((current) => !current);
        }}
        aria-expanded={open}
        data-testid="chat-panel-reasoning-panel-header"
      >
        <span className="tool-tree__header-line">
          <span className="tool-tree__cat-icon" aria-hidden="true">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10 3.2a4.4 4.4 0 0 0-2.6 7.95v1.6a.9.9 0 0 0 .9.9h3.4a.9.9 0 0 0 .9-.9v-1.6A4.4 4.4 0 0 0 10 3.2z" />
              <path d="M8.3 16.2h3.4" />
            </svg>
          </span>
          <span
            className={clsx('tool-tree__header-line-text', running && 'is-running')}
            data-testid="chat-panel-reasoning-panel-header-text"
            data-variant={running ? 'thinking' : 'thought'}
          >
            {running ? t('chatUi.reasoning.thinking') : t('chatUi.reasoning.thought')}
          </span>
          <span className={clsx('tool-tree-item__disclosure', open && 'is-open')} aria-hidden="true">
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
              <path strokeLinecap="round" strokeLinejoin="round" d="m8 6 4 4-4 4" />
            </svg>
          </span>
        </span>
      </button>
      <div className={clsx('reasoning-panel__collapse', open && 'is-open')}>
        <div className="reasoning-panel__collapse-inner">
          <div
            ref={bodyRef}
            className="reasoning-panel__body"
            data-testid="chat-panel-reasoning-panel-body"
            onScroll={() => {
              const el = bodyRef.current;
              if (!el) {
                return;
              }
              autoScrollRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
            }}
          >
            {body}
          </div>
        </div>
      </div>
    </div>
  );

  if (teamLayout) {
    return (
      <div
        className={clsx('reasoning-col', 'reasoning-col--team')}
        data-testid="chat-panel-reasoning-block"
        data-variant="team"
      >
        {showAvatar ? (
          <div className="reasoning-col__avatar pt-0.5">
            <TeamGroupDisplay identity={teamGroupIdentity} leaderIdentity={teamLeaderIdentity} />
          </div>
        ) : null}
        {content}
      </div>
    );
  }

  return (
    <div
      className="reasoning-col"
      data-testid="chat-panel-reasoning-block"
      data-variant="default"
    >
      {showAvatar ? (
        <div className="reasoning-col__avatar">
          {agentTemplateName ? (
            <AgentAvatar agentId={agentTemplateName} alt="" className="h-7 w-7" showName />
          ) : (
            <TeamLeaderDisplay identity={teamLeaderIdentity} />
          )}
        </div>
      ) : null}
      {content}
    </div>
  );
}

export function ChatTimelineList({
  messages,
  executions = [],
  reasoningSegments: reasoningSegmentsProp,
  staticTimeline = false,
  virtualized = !staticTimeline,
  mode = 'default',
  disableA2UIInteraction = false,
  incrementalStaticRendering = false,
  renderAfterMessage,
  sessionId = null,
  canLoadOlderHistory = false,
  onLoadOlderHistory,
  teamLeaderIdentityOverride,
  teamGroupIdentityOverride,
  onForkFromMessage,
}: ChatTimelineListProps) {
  const isTeamMode = mode === 'team';
  const globalActiveSessionId = useChatStore((s) => s.activeSessionId);
  const resolvedSessionId = sessionId ?? globalActiveSessionId;
  const runtimeTeamLeaderIdentity = useSessionStore(
    (s) => s.runtimes[resolvedSessionId ?? '']?.teamLeaderIdentity ?? null,
  );
  const teamLeaderIdentity = teamLeaderIdentityOverride ?? runtimeTeamLeaderIdentity;
  const teamGroupIdentity = teamGroupIdentityOverride;
  const storeIsProcessing = useChatStore((s) => s.runtimes[resolvedSessionId ?? '']?.isProcessing ?? false);
  const isLoadingHistory = useChatStore((s) => s.runtimes[resolvedSessionId ?? '']?.isLoadingHistory ?? false);
  const historyPagerMeta = useChatStore((s) => s.runtimes[resolvedSessionId ?? '']?.historyPagerMeta ?? null);
  const storeReasoningSegments = useChatStore(
    (s) => s.runtimes[resolvedSessionId ?? '']?.reasoningSegments ?? EMPTY_REASONING,
  );
  const isProcessing = staticTimeline ? false : storeIsProcessing;
  const allReasoningSegments = reasoningSegmentsProp ?? (staticTimeline ? EMPTY_REASONING : storeReasoningSegments);
  const publishedBatchSeq = staticTimeline
    ? Number.MAX_SAFE_INTEGER
    : (historyPagerMeta?.publishedBatchSeq ?? Number.MAX_SAFE_INTEGER);
  const selectedMessages = useMemo(
    () => filterPublishedHistoryBatch(messages, publishedBatchSeq),
    [messages, publishedBatchSeq],
  );
  const selectedExecutions = useMemo(
    () => filterPublishedHistoryBatch(executions, publishedBatchSeq),
    [executions, publishedBatchSeq],
  );
  const reasoningSegments = useMemo(
    () => filterPublishedHistoryBatch(allReasoningSegments, publishedBatchSeq),
    [allReasoningSegments, publishedBatchSeq],
  );
  const derivationInput = useMemo<TimelineDerivationInput>(
    () => ({
      sessionId,
      messages: selectedMessages,
      executions: selectedExecutions,
      reasoningSegments,
      isTeamMode,
      isProcessing,
    }),
    [sessionId, selectedMessages, selectedExecutions, reasoningSegments, isTeamMode, isProcessing],
  );
  const renderItems = useMemo(() => deriveTimelineItems(derivationInput), [derivationInput]);
  const agentTemplateNameByTurn = useMemo(() => {
    const names = new Map<number, string>();
    for (const item of renderItems) {
      const name =
        item.type === 'reasoning'
          ? item.segment.agentTemplateName?.trim()
          : item.type === 'toolGroup'
            ? item.agentTemplateName?.trim()
            : item.type === 'message' && item.message.role === 'assistant'
              ? item.message.agentTemplateName?.trim()
              : undefined;
      if (name) {
        names.set(item.turnId, name);
      }
    }
    return names;
  }, [renderItems]);
  const timelineScope = staticTimeline ? 'static' : (sessionId ?? 'interactive');
  const viewportByScopeRef = useRef(new Map<string, TimelineViewportState>());
  const rowStateRef = useRef(new Map<string, unknown>());
  const settlingForStreak = isSettlingForStreak(renderItems, Date.now());
  const settleNow = useNow(settlingForStreak);
  const streakNowMs = settlingForStreak ? settleNow : Date.now();
  const turnWorkMeta = useMemo(() => buildTurnWorkMeta(renderItems, isProcessing), [renderItems, isProcessing]);
  const turnFoldAnchorKeys = useMemo(
    () => buildTurnFoldAnchorKeys(renderItems, turnWorkMeta),
    [renderItems, turnWorkMeta],
  );
  const streakInputSig = useMemo(() => buildStreakInputSignature(renderItems, streakNowMs), [renderItems, streakNowMs]);
  const streakCacheRef = useRef<{ sig: string; map: Map<string, LiveWorkStreak> }>({
    sig: '',
    map: new Map(),
  });
  if (streakCacheRef.current.sig !== streakInputSig) {
    streakCacheRef.current = {
      sig: streakInputSig,
      map: buildLiveCompletedStreaks(renderItems, streakNowMs),
    };
  }
  const liveStreaksByFirstKey = streakCacheRef.current.map;
  const liveStreakFp = useMemo(() => streakMapFingerprint(liveStreaksByFirstKey), [liveStreaksByFirstKey]);
  const [displayedStreakState, setDisplayedStreakState] = useState<{
    scope: string;
    streaks: Map<string, LiveWorkStreak>;
  }>(() => ({ scope: timelineScope, streaks: new Map() }));
  const displayedStreakFpRef = useRef('');
  const suppressStreakTransitionRef = useRef(true);
  const displayedStreaksByFirstKey =
    displayedStreakState.scope === timelineScope ? displayedStreakState.streaks : liveStreaksByFirstKey;
  const streaksForRender = staticTimeline ? liveStreaksByFirstKey : displayedStreaksByFirstKey;
  const [expandedTurnsByScope, setExpandedTurnsByScope] = useState<Record<string, Record<string, boolean>>>({});
  const [expandedStreaksByScope, setExpandedStreaksByScope] = useState<Record<string, Record<string, boolean>>>({});
  const expandedTurns = expandedTurnsByScope[timelineScope] ?? EMPTY_EXPANSIONS;
  const expandedStreaks = expandedStreaksByScope[timelineScope] ?? EMPTY_EXPANSIONS;
  const displayItems = useMemo(
    () =>
      projectTimelineItems(
        renderItems,
        turnWorkMeta,
        turnFoldAnchorKeys,
        streaksForRender,
        expandedTurns,
        expandedStreaks,
      ),
    [renderItems, turnWorkMeta, turnFoldAnchorKeys, streaksForRender, expandedTurns, expandedStreaks],
  );
  const incrementallyRenderItems = staticTimeline && incrementalStaticRendering;
  const [staticRenderItemCount, setStaticRenderItemCount] = useState(1);
  const visibleRenderItemCount = incrementallyRenderItems
    ? Math.min(staticRenderItemCount, displayItems.length)
    : displayItems.length;
  const staticRenderComplete = !incrementallyRenderItems || visibleRenderItemCount >= displayItems.length;
  const visibleRenderItems = incrementallyRenderItems ? displayItems.slice(0, visibleRenderItemCount) : displayItems;

  useEffect(() => {
    if (!incrementallyRenderItems || staticRenderComplete) return;
    const timer = window.setTimeout(() => {
      setStaticRenderItemCount((count) => Math.min(count + 1, displayItems.length));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [incrementallyRenderItems, displayItems.length, staticRenderComplete, visibleRenderItemCount]);

  useEffect(() => {
    suppressStreakTransitionRef.current = true;
    displayedStreakFpRef.current = '';
    setDisplayedStreakState({ scope: timelineScope, streaks: new Map() });
  }, [resolvedSessionId, timelineScope]);

  const wasLoadingHistoryRef = useRef(false);
  useEffect(() => {
    if (staticTimeline) {
      return;
    }
    if (isLoadingHistory) {
      wasLoadingHistoryRef.current = true;
      return;
    }
    if (wasLoadingHistoryRef.current) {
      wasLoadingHistoryRef.current = false;
      setExpandedTurnsByScope((current) => ({ ...current, [timelineScope]: {} }));
      setExpandedStreaksByScope((current) => ({ ...current, [timelineScope]: {} }));
      suppressStreakTransitionRef.current = true;
      displayedStreakFpRef.current = '';
      setDisplayedStreakState({ scope: timelineScope, streaks: new Map() });
    }
  }, [staticTimeline, isLoadingHistory, timelineScope]);

  useEffect(() => {
    if (staticTimeline) {
      return;
    }
    if (liveStreakFp === displayedStreakFpRef.current) {
      return;
    }
    const nextMap = liveStreaksByFirstKey;
    if (suppressStreakTransitionRef.current) {
      displayedStreakFpRef.current = liveStreakFp;
      suppressStreakTransitionRef.current = false;
      setDisplayedStreakState({ scope: timelineScope, streaks: nextMap });
      return;
    }
    const timer = window.setTimeout(() => {
      displayedStreakFpRef.current = liveStreakFp;
      setDisplayedStreakState({ scope: timelineScope, streaks: nextMap });
    }, STREAK_FOLD_TRANSITION_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [liveStreakFp, liveStreaksByFirstKey, staticTimeline, timelineScope]);

  if (renderItems.length === 0) {
    return null;
  }

  const toggleTurn = (turnKey: string) => {
    setExpandedTurnsByScope((current) => ({
      ...current,
      [timelineScope]: { ...current[timelineScope], [turnKey]: !current[timelineScope]?.[turnKey] },
    }));
  };

  const toggleStreak = (streakId: string) => {
    setExpandedStreaksByScope((current) => ({
      ...current,
      [timelineScope]: { ...current[timelineScope], [streakId]: !current[timelineScope]?.[streakId] },
    }));
  };

  const renderDisplayItem = (displayItem: TimelineDisplayItem) => {
    const { item, turnKey, meta, turnFoldable, turnOpen, streak, streakOpen, contentOpen, isTurnAnchor, deliverables } =
      displayItem;
    if (item.type === 'message') {
      // 正文始终展示；hideMeta 只控制时间与操作栏，不参与思考/工具折叠。
      return (
        <Fragment key={`${timelineScope}/${item.key}`}>
          <MessageItem
            message={item.message}
            showAvatar={item.showAvatar}
            hideMeta={item.hideMeta}
            disableA2UIInteraction={disableA2UIInteraction}
            enableAssistantAvatar={!isTeamMode}
            teamLeaderIdentityOverride={teamLeaderIdentity}
            teamGroupIdentityOverride={teamGroupIdentity}
            onForkFromMessage={onForkFromMessage}
          />
          {renderAfterMessage?.(item.message)}
        </Fragment>
      );
    }

    if (item.type === 'reasoning' || item.type === 'toolGroup') {
      const nodes: ReactNode[] = [];

      if (turnFoldable && isTurnAnchor && meta) {
        nodes.push(
          <CompletedWorkChip
            key={`${timelineScope}/completed-work-${turnKey}`}
            variant="turn"
            outcomeTone={meta.outcomeTone}
            expanded={turnOpen}
            onToggle={() => toggleTurn(turnKey)}
            // 折叠条就是该轮视觉顶部：头像必须挂在这里，不能跟 meta/内容区抢来抢去。
            elapsedMs={completedWorkDurationMs(meta)}
            showAvatar
            teamLayout={isTeamMode}
            agentTemplateName={agentTemplateNameByTurn.get(item.turnId)}
            teamLeaderIdentity={teamLeaderIdentity}
            teamGroupIdentity={teamGroupIdentity}
          />,
        );
      }

      // 轮次展开后才露出 streak chip；内容仍可按 streak 再折一层
      // 整轮只有最顶部一颗头像：turn 折叠条 > 该轮第一条 streak > 首条内容
      const isTopStreakInTurn = Boolean(streak && streak.ordinal === 0);
      if (turnOpen && streak && streak.firstKey === item.key) {
        nodes.push(
          <CompletedWorkChip
            key={`${timelineScope}/${streak.id}`}
            variant="streak"
            thinkingCount={streak.thinkingCount}
            toolCount={streak.toolCount}
            outcomeTone={streak.outcomeTone}
            expanded={streakOpen}
            onToggle={() => toggleStreak(streak.id)}
            // 仅当这条 streak 本身吃到了本轮顶部头像时才画；后续 streak 一律不画
            showAvatar={!turnFoldable && isTopStreakInTurn && streak.showAvatar}
            teamLayout={isTeamMode}
            agentTemplateName={agentTemplateNameByTurn.get(item.turnId)}
            teamLeaderIdentity={teamLeaderIdentity}
            teamGroupIdentity={teamGroupIdentity}
          />,
        );
      }

      // 折叠时交付物仍可见（不参与收起动画）
      if (!contentOpen && item.type === 'toolGroup') {
        if (deliverables.length > 0) {
          nodes.push(
            <ToolGroupDisplay
              key={`${timelineScope}/${item.key}-deliverable`}
              executions={deliverables}
              notices={[]}
              showAvatar={false}
              teamLayout={isTeamMode}
              collapseSkillTreeWhenContentStarts={false}
              viewedSkillIds={[]}
              teamLeaderIdentity={teamLeaderIdentity}
            />,
          );
        }
      }

      // 头像已挂在 turn/顶部 streak 上时，展开内容不再重复画。
      const turnChipOwnsAvatar = turnFoldable;
      const streakChipOwnsAvatar = Boolean(!turnFoldable && isTopStreakInTurn && streak?.showAvatar);
      const hideAvatar = Boolean((turnChipOwnsAvatar && turnOpen) || (streakChipOwnsAvatar && streakOpen));

      const body =
        item.type === 'reasoning' ? (
          <ReasoningSegmentBlock
            segment={item.segment}
            agentTemplateName={item.segment.agentTemplateName ?? agentTemplateNameByTurn.get(item.turnId)}
            showAvatar={hideAvatar ? false : item.showAvatar}
            teamLayout={isTeamMode}
            teamLeaderIdentity={teamLeaderIdentity}
            teamGroupIdentity={teamGroupIdentity}
          />
        ) : (
          <ToolGroupDisplay
            executions={item.executions}
            notices={item.notices}
            showAvatar={hideAvatar ? false : item.showAvatar}
            teamLayout={isTeamMode}
            agentTemplateName={agentTemplateNameByTurn.get(item.turnId)}
            teamLeaderIdentity={teamLeaderIdentity}
            collapseSkillTreeWhenContentStarts={item.collapseSkillTreeWhenContentStarts}
            viewedSkillIds={item.viewedSkillIds}
          />
        );

      // Only mounted, visible work participates in row measurement.
      if (contentOpen && (turnFoldable || streak)) {
        nodes.push(
          <div
            key={`${timelineScope}/${item.key}-collapse`}
            className={clsx('timeline-collapse', contentOpen && 'is-open')}
            data-testid="chat-panel-timeline-collapse"
            data-variant={contentOpen ? 'open' : 'closed'}
          >
            <div className="timeline-collapse-inner">{body}</div>
          </div>,
        );
      } else if (contentOpen) {
        nodes.push(<Fragment key={`${timelineScope}/${item.key}`}>{body}</Fragment>);
      }

      return nodes.length === 1 ? nodes[0] : <Fragment key={`${timelineScope}/work-${item.key}`}>{nodes}</Fragment>;
    }

    if (item.type === 'turnSummary') {
      const meta = turnWorkMeta.get(item.turnId);
      // 有折叠工作的已完成轮次：耗时已并入折叠条文案（头像下第一行），时间行不再重复渲染。
      if (meta?.completed && meta.hasWork) {
        return null;
      }
      const range = meta
        ? turnElapsedRangeMs(meta)
        : { startMs: item.startMs, endMs: item.hasWork ? item.workEndMs : item.endMs };
      return (
        <TurnElapsed
          key={`${timelineScope}/${item.key}`}
          startMs={range.startMs}
          endMs={range.endMs}
          isLastTurn={item.isLastTurn}
          isProcessing={isProcessing}
          showAvatar={item.showAvatar}
          agentTemplateName={agentTemplateNameByTurn.get(item.turnId)}
          teamLayout={isTeamMode}
          teamLeaderIdentity={teamLeaderIdentity}
          teamGroupIdentity={teamGroupIdentity}
        />
      );
    }

    return null;
  };

  if (virtualized) {
    return (
      <VirtualTimeline
        key={timelineScope}
        items={displayItems}
        renderItem={(item) => (
          <TimelineRowStateProvider values={rowStateRef.current} prefix={`${timelineScope}/${item.item.key}`}>
            {renderDisplayItem(item)}
          </TimelineRowStateProvider>
        )}
        initialState={viewportByScopeRef.current.get(timelineScope)}
        onSaveState={(state) => viewportByScopeRef.current.set(timelineScope, state)}
        canLoadOlderHistory={canLoadOlderHistory}
        onLoadOlderHistory={onLoadOlderHistory}
        historyRequestKey={`${timelineScope}:${historyPagerMeta?.loadedBatchSeq ?? 0}:${historyPagerMeta?.publishedBatchSeq ?? 0}:${historyPagerMeta?.hasMore ? 1 : 0}`}
      />
    );
  }
  return (
    <div
      className="chat-timeline"
      data-testid="chat-panel-timeline"
      data-share-image-render-state={
        incrementallyRenderItems ? (staticRenderComplete ? 'complete' : 'pending') : undefined
      }
    >
      {visibleRenderItems.map((item) => (
        <Fragment key={`${timelineScope}/${item.key}`}>{renderDisplayItem(item)}</Fragment>
      ))}
    </div>
  );
}

export function MessageList({
  messages,
  sessionId,
  renderAfterMessage,
  canLoadOlderHistory,
  onLoadOlderHistory,
  teamLeaderIdentityOverride,
  teamGroupIdentityOverride,
  onForkFromMessage,
}: MessageListProps) {
  const globalActiveSessionId = useChatStore((s) => s.activeSessionId);
  const resolvedSessionId = sessionId ?? globalActiveSessionId;
  const toolExecutions = useChatStore((s) => s.runtimes[resolvedSessionId ?? '']?.toolExecutions ?? new Map());
  const toolExecutionOrder = useChatStore((s) => s.runtimes[resolvedSessionId ?? '']?.toolExecutionOrder ?? []);
  const mode = useSessionStore((s) => s.runtimes[resolvedSessionId ?? '']?.mode ?? 'agent');
  const executions = useMemo(
    () => getExecutionList(toolExecutions, toolExecutionOrder),
    [toolExecutions, toolExecutionOrder]
  );

  return (
    <ChatTimelineList
      messages={messages}
      sessionId={resolvedSessionId ?? undefined}
      executions={executions}
      mode={mode}
      renderAfterMessage={renderAfterMessage}
      canLoadOlderHistory={canLoadOlderHistory}
      onLoadOlderHistory={onLoadOlderHistory}
      teamLeaderIdentityOverride={teamLeaderIdentityOverride}
      teamGroupIdentityOverride={teamGroupIdentityOverride}
      onForkFromMessage={onForkFromMessage}
    />
  );
}
