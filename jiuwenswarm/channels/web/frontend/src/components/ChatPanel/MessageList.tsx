import {
  Fragment,
  useCallback,
  useEffect,
  useLayoutEffect,
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
import type { ReasoningSegment } from '../../stores/chatStore';
import {
  filterPublishedHistoryBatch,
  shiftBoundedTimelineRange,
} from '../../features/historyPagination';
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
  messageHasDeliverable,
  filterDeliverableExecutions,
  completedWorkDurationMs,
  turnElapsedRangeMs,
  REASONING_COLLAPSE_DELAY_MS,
  STREAK_FOLD_TRANSITION_DELAY_MS,
  type LiveWorkStreak,
} from '../../features/chatTimeline/buildTurnTimeline';

const EMPTY_REASONING: ReasoningSegment[] = [];

interface MessageListProps {
  messages: Message[];
  renderAfterMessage?: (message: Message) => ReactNode;
  canLoadOlderHistory?: boolean;
  onLoadOlderHistory?: () => void | Promise<void>;
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

const INITIAL_TIMELINE_BLOCKS = 80;
const TIMELINE_ADMISSION_BATCH = 40;
const MAX_TIMELINE_BLOCKS = INITIAL_TIMELINE_BLOCKS + TIMELINE_ADMISSION_BATCH;

type TimelineAdmission = {
  scope: string;
  firstKey: string;
  /** null means that the window follows the live tail. */
  lastKey: string | null;
};

function alignAdmissionStart(
  items: TimelineRenderItems,
  targetIndex: number,
  endExclusive = items.length
): number {
  // 只在本批次内部向前对齐业务边界，保证单个超长 turn 也不会突破准入上限。
  const start = Math.max(0, Math.min(targetIndex, items.length));
  if (start === 0) {
    return 0;
  }
  const end = Math.max(start, Math.min(endExclusive, items.length));
  for (let index = start; index < end; index += 1) {
    const item = items[index];
    if (
      item.type === 'message' &&
      (item.message.role === 'user' ||
        item.message.isCommandOutput ||
        item.message.isProactiveRecommendation)
    ) {
      return index;
    }
  }
  return start;
}

function initialAdmission(
  items: TimelineRenderItems,
  scope: string
): TimelineAdmission {
  const start = alignAdmissionStart(items, Math.max(0, items.length - INITIAL_TIMELINE_BLOCKS));
  return { scope, firstKey: items[start]?.key ?? '', lastKey: null };
}

function resolveTimelineAdmission(
  items: TimelineRenderItems,
  scope: string,
  admission: TimelineAdmission,
  virtualized: boolean,
): TimelineAdmission {
  if (!virtualized) {
    return initialAdmission(items, scope);
  }
  if (items.length <= MAX_TIMELINE_BLOCKS) {
    return {
      scope,
      firstKey: items[0]?.key ?? '',
      lastKey: null,
    };
  }

  const start = items.findIndex((item) => item.key === admission.firstKey);
  const end = admission.lastKey === null
    ? items.length
    : items.findIndex((item) => item.key === admission.lastKey) + 1;
  if (start < 0 || end <= start) {
    return initialAdmission(items, scope);
  }
  if (admission.lastKey !== null || end - start <= MAX_TIMELINE_BLOCKS) {
    return { ...admission, scope };
  }

  const boundedStart = alignAdmissionStart(
    items,
    Math.max(0, end - MAX_TIMELINE_BLOCKS),
    end,
  );
  return {
    scope,
    firstKey: items[boundedStart]?.key ?? '',
    lastKey: null,
  };
}

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
  showAvatar,
  agentTemplateName,
  teamLayout,
}: {
  startMs: number;
  endMs: number;
  isLastTurn: boolean;
  showAvatar?: boolean;
  agentTemplateName?: string;
  teamLayout: boolean;
}) {
  const { t } = useTranslation();
  const isProcessing = useChatStore((s) => s.runtimes[s.activeSessionId ?? '']?.isProcessing ?? false);
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
          <>
            <TeamMemberAvatar member="team_leader" className="h-7 w-7" />
            <span className="chat-avatar-name">Jiuwen</span>
          </>
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
  const showPartialBadge = applyOutcome && outcomeTone === 'partial';
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
      {showPartialBadge ? (
        <span
          className="completed-work-chip__badge is-partial"
          data-testid="chat-panel-completed-work-chip-badge"
          data-variant="partial"
        >
          {t('chatUi.workOutcomePartial')}
        </span>
      ) : null}
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
            <TeamMemberAvatar member="team_leader" className="h-7 w-7" />
            <span className="chat-avatar-name">Jiuwen</span>
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
            <>
              <TeamMemberAvatar member="team_leader" className="h-7 w-7" />
              <span className="chat-avatar-name">Jiuwen</span>
            </>
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
}: {
  segment: ReasoningSegment;
  agentTemplateName?: string;
  showAvatar: boolean;
  teamLayout: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(!segment.closed);
  const userToggledRef = useRef(false);
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
  }, [segment.closed]);

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
            <TeamMemberAvatar member="team_leader" />
            <span className="chat-avatar-name">Jiuwen</span>
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
            <>
              <TeamMemberAvatar member="team_leader" />
              <span className="chat-avatar-name">Jiuwen</span>
            </>
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
}: ChatTimelineListProps) {
  const isTeamMode = mode === 'team';
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const storeIsProcessing = useChatStore((s) => s.runtimes[s.activeSessionId ?? '']?.isProcessing ?? false);
  const isLoadingHistory = useChatStore((s) => s.runtimes[s.activeSessionId ?? '']?.isLoadingHistory ?? false);
  const historyPagerMeta = useChatStore(
    (s) => s.runtimes[s.activeSessionId ?? '']?.historyPagerMeta ?? null
  );
  const storeReasoningSegments = useChatStore(
    (s) => s.runtimes[s.activeSessionId ?? '']?.reasoningSegments ?? EMPTY_REASONING
  );
  const isProcessing = staticTimeline ? false : storeIsProcessing;
  const allReasoningSegments = reasoningSegmentsProp ?? (staticTimeline ? EMPTY_REASONING : storeReasoningSegments);
  const publishedBatchSeq = staticTimeline
    ? Number.MAX_SAFE_INTEGER
    : historyPagerMeta?.publishedBatchSeq ?? Number.MAX_SAFE_INTEGER;
  const selectedMessages = useMemo(
    () => filterPublishedHistoryBatch(messages, publishedBatchSeq),
    [messages, publishedBatchSeq]
  );
  const selectedExecutions = useMemo(
    () => filterPublishedHistoryBatch(executions, publishedBatchSeq),
    [executions, publishedBatchSeq]
  );
  const reasoningSegments = useMemo(
    () => filterPublishedHistoryBatch(allReasoningSegments, publishedBatchSeq),
    [allReasoningSegments, publishedBatchSeq]
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
    [
      sessionId,
      selectedMessages,
      selectedExecutions,
      reasoningSegments,
      isTeamMode,
      isProcessing,
    ]
  );
  const renderItems = useMemo(
    () => deriveTimelineItems(derivationInput),
    [derivationInput]
  );
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
  const timelineScope = staticTimeline ? 'static' : sessionId ?? 'interactive';
  const [admissionByScope, setAdmissionByScope] = useState<Map<string, TimelineAdmission>>(() => {
    const initial = initialAdmission(renderItems, timelineScope);
    return new Map([[timelineScope, initial]]);
  });
  const admission = admissionByScope.get(timelineScope)
    ?? { scope: timelineScope, firstKey: '', lastKey: null };
  const resolvedAdmission = useMemo(
    () => resolveTimelineAdmission(renderItems, timelineScope, admission, virtualized),
    [admission, renderItems, timelineScope, virtualized],
  );
  const admittedStartIndex = !virtualized
    ? 0
    : Math.max(0, renderItems.findIndex((item) => item.key === resolvedAdmission.firstKey));
  const admittedEndExclusive = !virtualized || resolvedAdmission.lastKey === null
    ? renderItems.length
    : Math.max(
      admittedStartIndex,
      renderItems.findIndex((item) => item.key === resolvedAdmission.lastKey) + 1
    );
  const admittedRenderItems = !virtualized
    ? renderItems
    : renderItems.slice(admittedStartIndex, admittedEndExclusive);
  const timelineRef = useRef<HTMLDivElement>(null);
  const olderSentinelRef = useRef<HTMLDivElement>(null);
  const newerSentinelRef = useRef<HTMLDivElement>(null);
  const admissionPendingRef = useRef(false);
  const admissionAnchorRef = useRef<
    {
      element: HTMLElement;
      root: HTMLElement;
      top: number;
    } | null
  >(null);
  const adjustedScrollTopRef = useRef<number | null>(null);
  const lastAutoFillRequestKeyRef = useRef<string | null>(null);
  const admitOlderTimelineBlocksRef = useRef<() => void>(() => {});
  const admitNewerTimelineBlocksRef = useRef<() => void>(() => {});

  useEffect(() => {
    if (
      admission.firstKey !== resolvedAdmission.firstKey
      || admission.lastKey !== resolvedAdmission.lastKey
    ) {
      setAdmissionByScope((current) => {
        const next = new Map(current);
        next.set(timelineScope, resolvedAdmission);
        return next;
      });
    }
  }, [admission.firstKey, admission.lastKey, resolvedAdmission, timelineScope]);

  const admitOlderTimelineBlocks = useCallback(() => {
    if (!virtualized || admittedStartIndex <= 0 || admissionPendingRef.current) {
      return;
    }
    const sentinel = olderSentinelRef.current;
    const firstExistingBlock = timelineRef.current?.children[1];
    const root = timelineRef.current?.closest<HTMLElement>('.chat-scroll');
    if (!sentinel || !(firstExistingBlock instanceof HTMLElement) || !root) {
      return;
    }
    const shiftedRange = shiftBoundedTimelineRange(
      { start: admittedStartIndex, end: admittedEndExclusive },
      renderItems.length,
      'older',
      TIMELINE_ADMISSION_BATCH,
      MAX_TIMELINE_BLOCKS
    );
    const nextStart = alignAdmissionStart(
      renderItems,
      shiftedRange.start,
      admittedStartIndex
    );
    const nextEnd = Math.min(shiftedRange.end, nextStart + MAX_TIMELINE_BLOCKS);
    admissionPendingRef.current = true;
    admissionAnchorRef.current = {
      element: firstExistingBlock,
      root,
      top: firstExistingBlock.getBoundingClientRect().top,
    };
    setAdmissionByScope((current) => {
      const next = new Map(current);
      next.set(timelineScope, {
        scope: timelineScope,
        firstKey: renderItems[nextStart]?.key ?? '',
        lastKey: nextEnd >= renderItems.length ? null : renderItems[nextEnd - 1]?.key ?? null,
      });
      return next;
    });
  }, [admittedEndExclusive, admittedStartIndex, renderItems, timelineScope, virtualized]);

  const admitNewerTimelineBlocks = useCallback(() => {
    if (
      !virtualized
      || admittedEndExclusive >= renderItems.length
      || admissionPendingRef.current
    ) {
      return;
    }
    const sentinel = newerSentinelRef.current;
    const children = timelineRef.current?.children;
    const lastExistingBlock = children?.[children.length - 2];
    const root = timelineRef.current?.closest<HTMLElement>('.chat-scroll');
    if (!sentinel || !(lastExistingBlock instanceof HTMLElement) || !root) {
      return;
    }
    const shiftedRange = shiftBoundedTimelineRange(
      { start: admittedStartIndex, end: admittedEndExclusive },
      renderItems.length,
      'newer',
      TIMELINE_ADMISSION_BATCH,
      MAX_TIMELINE_BLOCKS
    );
    const nextStart = alignAdmissionStart(
      renderItems,
      shiftedRange.start,
      shiftedRange.end
    );
    admissionPendingRef.current = true;
    admissionAnchorRef.current = {
      element: lastExistingBlock,
      root,
      top: lastExistingBlock.getBoundingClientRect().top,
    };
    setAdmissionByScope((current) => {
      const next = new Map(current);
      next.set(timelineScope, {
        scope: timelineScope,
        firstKey: renderItems[nextStart]?.key ?? '',
        lastKey: shiftedRange.end >= renderItems.length
          ? null
          : renderItems[shiftedRange.end - 1]?.key ?? null,
      });
      return next;
    });
  }, [admittedEndExclusive, admittedStartIndex, renderItems, timelineScope, virtualized]);

  admitOlderTimelineBlocksRef.current = admitOlderTimelineBlocks;
  admitNewerTimelineBlocksRef.current = admitNewerTimelineBlocks;

  useLayoutEffect(() => {
    const anchor = admissionAnchorRef.current;
    if (!anchor) {
      admissionPendingRef.current = false;
      return;
    }
    if (!anchor.element.isConnected) {
      admissionAnchorRef.current = null;
      admissionPendingRef.current = false;
      return;
    }
    const nextTop = anchor.element.getBoundingClientRect().top;
    anchor.root.scrollTop += nextTop - anchor.top;
    adjustedScrollTopRef.current = anchor.root.scrollTop;
    admissionAnchorRef.current = null;
    admissionPendingRef.current = false;
  }, [
    admittedEndExclusive,
    admittedStartIndex,
    renderItems,
    resolvedAdmission.firstKey,
    resolvedAdmission.lastKey,
    timelineScope,
  ]);

  useLayoutEffect(() => {
    if (!virtualized || admissionPendingRef.current) return;
    const root = timelineRef.current?.closest<HTMLElement>('.chat-scroll');
    if (!root || root.clientHeight <= 0 || root.scrollHeight > root.clientHeight) {
      return;
    }
    if (admittedStartIndex > 0) {
      admitOlderTimelineBlocksRef.current();
      return;
    }
    if (!canLoadOlderHistory || !onLoadOlderHistory) return;

    const requestKey = [
      timelineScope,
      historyPagerMeta?.loadedBatchSeq ?? 0,
      historyPagerMeta?.publishedBatchSeq ?? 0,
      historyPagerMeta?.hasMore ? 1 : 0,
    ].join(':');
    if (lastAutoFillRequestKeyRef.current === requestKey) return;
    lastAutoFillRequestKeyRef.current = requestKey;
    void onLoadOlderHistory();
  }, [
    admittedStartIndex,
    canLoadOlderHistory,
    historyPagerMeta?.hasMore,
    historyPagerMeta?.loadedBatchSeq,
    historyPagerMeta?.publishedBatchSeq,
    onLoadOlderHistory,
    renderItems.length,
    timelineScope,
    virtualized,
  ]);

  useEffect(() => {
    const hasOlderBoundary = admittedStartIndex > 0;
    const hasNewerBoundary = admittedEndExclusive < renderItems.length;
    if (!virtualized || (!hasOlderBoundary && !hasNewerBoundary)) {
      return;
    }
    const root = timelineRef.current?.closest<HTMLElement>('.chat-scroll');
    if (!root) {
      return;
    }
    const boundaryIsCurrentlyVisible = (direction: 'older' | 'newer') => {
      const sentinel = direction === 'older'
        ? olderSentinelRef.current
        : newerSentinelRef.current;
      if (!sentinel) {
        return false;
      }
      const rootTop = root.getBoundingClientRect().top;
      const sentinelTop = sentinel.getBoundingClientRect().top;
      return sentinelTop >= rootTop && sentinelTop <= rootTop + root.clientHeight;
    };
    let previousScrollTop = root.scrollTop;
    const handleUserWheel = (event: WheelEvent) => {
      if (event.deltaY < 0 && boundaryIsCurrentlyVisible('older')) {
        admitOlderTimelineBlocksRef.current();
      } else if (event.deltaY > 0 && boundaryIsCurrentlyVisible('newer')) {
        admitNewerTimelineBlocksRef.current();
      }
    };
    const handleScroll = () => {
      const nextScrollTop = root.scrollTop;
      if (adjustedScrollTopRef.current === nextScrollTop) {
        adjustedScrollTopRef.current = null;
        previousScrollTop = nextScrollTop;
        return;
      }
      adjustedScrollTopRef.current = null;
      if (nextScrollTop < previousScrollTop && boundaryIsCurrentlyVisible('older')) {
        admitOlderTimelineBlocksRef.current();
      } else if (
        nextScrollTop > previousScrollTop
        && boundaryIsCurrentlyVisible('newer')
      ) {
        admitNewerTimelineBlocksRef.current();
      }
      previousScrollTop = nextScrollTop;
    };
    root.addEventListener('wheel', handleUserWheel, { passive: true });
    root.addEventListener('scroll', handleScroll, { passive: true });
    return () => {
      root.removeEventListener('wheel', handleUserWheel);
      root.removeEventListener('scroll', handleScroll);
    };
  }, [
    admittedEndExclusive >= renderItems.length,
    admittedStartIndex <= 0,
    renderItems.length,
    timelineScope,
    virtualized,
  ]);
  const settlingForStreak = isSettlingForStreak(renderItems, Date.now());
  const settleNow = useNow(settlingForStreak);
  const streakNowMs = settlingForStreak ? settleNow : Date.now();
  const turnWorkMeta = useMemo(
    () => buildTurnWorkMeta(renderItems, isProcessing),
    [renderItems, isProcessing]
  );
  const turnFoldAnchorKeys = useMemo(
    () => buildTurnFoldAnchorKeys(renderItems, turnWorkMeta),
    [renderItems, turnWorkMeta]
  );
  const streakInputSig = useMemo(
    () => buildStreakInputSignature(renderItems, streakNowMs),
    [renderItems, streakNowMs]
  );
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
  const liveStreakFp = useMemo(
    () => streakMapFingerprint(liveStreaksByFirstKey),
    [liveStreaksByFirstKey]
  );
  const [displayedStreakState, setDisplayedStreakState] = useState<{
    scope: string;
    streaks: Map<string, LiveWorkStreak>;
  }>(() => ({ scope: timelineScope, streaks: new Map() }));
  const displayedStreakFpRef = useRef('');
  const suppressStreakTransitionRef = useRef(true);
  const displayedStreaksByFirstKey = displayedStreakState.scope === timelineScope
    ? displayedStreakState.streaks
    : liveStreaksByFirstKey;
  const streaksForRender = staticTimeline ? liveStreaksByFirstKey : displayedStreaksByFirstKey;
  const liveStreakByItemKey = useMemo(() => {
    const map = new Map<string, LiveWorkStreak>();
    for (const streak of streaksForRender.values()) {
      for (const key of streak.keys) {
        map.set(key, streak);
      }
    }
    return map;
  }, [streaksForRender]);
  const stableTurnKeyById = useMemo(() => {
    const map = new Map<number, string>();
    for (const item of renderItems) {
      if (item.type === 'turnSummary') {
        map.set(item.turnId, item.key);
      }
    }
    return map;
  }, [renderItems]);
  const [expandedTurnState, setExpandedTurnState] = useState<{
    scope: string;
    values: Record<string, boolean>;
  }>(() => ({ scope: timelineScope, values: {} }));
  const [expandedStreakState, setExpandedStreakState] = useState<{
    scope: string;
    values: Record<string, boolean>;
  }>(() => ({ scope: timelineScope, values: {} }));
  const expandedTurns = expandedTurnState.scope === timelineScope
    ? expandedTurnState.values
    : {};
  const expandedStreaks = expandedStreakState.scope === timelineScope
    ? expandedStreakState.values
    : {};
  const incrementallyRenderItems = staticTimeline && incrementalStaticRendering;
  const [staticRenderItemCount, setStaticRenderItemCount] = useState(1);
  const visibleRenderItemCount = incrementallyRenderItems
    ? Math.min(staticRenderItemCount, renderItems.length)
    : admittedRenderItems.length;
  const staticRenderComplete = !incrementallyRenderItems
    || visibleRenderItemCount >= renderItems.length;
  const visibleRenderItems = incrementallyRenderItems
    ? renderItems.slice(0, visibleRenderItemCount)
    : admittedRenderItems;
  const chipAnchoredTurns = useRef<Set<string>>(new Set());
  chipAnchoredTurns.current = new Set();

  useEffect(() => {
    if (!incrementallyRenderItems || staticRenderComplete) return;
    const timer = window.setTimeout(() => {
      setStaticRenderItemCount(count => Math.min(count + 1, renderItems.length));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [incrementallyRenderItems, renderItems.length, staticRenderComplete, visibleRenderItemCount]);

  useEffect(() => {
    setExpandedTurnState({ scope: timelineScope, values: {} });
    setExpandedStreakState({ scope: timelineScope, values: {} });
    suppressStreakTransitionRef.current = true;
    displayedStreakFpRef.current = '';
    setDisplayedStreakState({ scope: timelineScope, streaks: new Map() });
  }, [activeSessionId, timelineScope]);

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
      setExpandedTurnState({ scope: timelineScope, values: {} });
      setExpandedStreakState({ scope: timelineScope, values: {} });
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
    setExpandedTurnState((current) => {
      const values = current.scope === timelineScope ? current.values : {};
      return {
        scope: timelineScope,
        values: { ...values, [turnKey]: !values[turnKey] },
      };
    });
  };

  const toggleStreak = (streakId: string) => {
    setExpandedStreakState((current) => {
      const values = current.scope === timelineScope ? current.values : {};
      return {
        scope: timelineScope,
        values: { ...values, [streakId]: !values[streakId] },
      };
    });
  };

  return (
    <div
      ref={timelineRef}
      className="chat-timeline"
      data-testid="chat-panel-timeline"
      data-share-image-render-state={incrementallyRenderItems ? (staticRenderComplete ? 'complete' : 'pending') : undefined}
    >
      {virtualized && admittedStartIndex > 0 ? (
        <div
          ref={olderSentinelRef}
          className="chat-timeline__history-sentinel"
          aria-hidden="true"
          data-testid="chat-panel-timeline-history-sentinel"
        />
      ) : null}
      {visibleRenderItems.map((item) => {
        if (item.type === 'message') {
          const turnKey = stableTurnKeyById.get(item.turnId) ?? item.key;
          const meta = item.turnId >= 0 ? turnWorkMeta.get(item.turnId) : undefined;
          const turnFoldable = Boolean(meta?.completed && meta.hasWork && item.hideMeta);
          const turnOpen = !turnFoldable || Boolean(expandedTurns[turnKey]);
          const isFoldAnchor = turnFoldAnchorKeys.get(item.turnId) === item.key;

          if (turnFoldable) {
            const hasDeliverable = messageHasDeliverable(item.message);
            return (
              <Fragment key={`${timelineScope}/${item.key}`}>
                {/* 工具前的开场白若可折叠，折叠条锚在这里，展开后不会跑到「已完成」上面 */}
                {isFoldAnchor && meta ? (
                  <CompletedWorkChip
                    key={`${timelineScope}/completed-work-${turnKey}`}
                    variant="turn"
                    outcomeTone={meta.outcomeTone}
                    expanded={turnOpen}
                    onToggle={() => toggleTurn(turnKey)}
                    elapsedMs={completedWorkDurationMs(meta)}
                    showAvatar
                    teamLayout={isTeamMode}
                    agentTemplateName={item.message.agentTemplateName ?? agentTemplateNameByTurn.get(item.turnId)}
                  />
                ) : null}
                {/* 折叠态：交付物与代码变更卡需留在文档流内，不能放进被 absolute 隐藏的 collapse */}
                {!turnOpen && hasDeliverable ? (
                  <>
                    <MessageItem
                      message={{ ...item.message, content: '' }}
                      showAvatar={false}
                      hideMeta
                      disableA2UIInteraction={disableA2UIInteraction}
                      enableAssistantAvatar={!isTeamMode}
                    />
                    {renderAfterMessage?.(item.message)}
                  </>
                ) : null}
                <div
                  className={clsx('timeline-collapse', turnOpen && 'is-open')}
                  data-testid="chat-panel-timeline-collapse"
                  data-variant={turnOpen ? 'open' : 'closed'}
                >
                  <div className="timeline-collapse-inner">
                    <MessageItem
                      message={item.message}
                      showAvatar={item.showAvatar}
                      hideMeta={item.hideMeta}
                      disableA2UIInteraction={disableA2UIInteraction}
                      enableAssistantAvatar={!isTeamMode}
                    />
                    {turnOpen ? renderAfterMessage?.(item.message) : null}
                  </div>
                </div>
              </Fragment>
            );
          }

          return (
            <Fragment key={`${timelineScope}/${item.key}`}>
              <MessageItem
                message={item.message}
                showAvatar={item.showAvatar}
                hideMeta={item.hideMeta}
                disableA2UIInteraction={disableA2UIInteraction}
                enableAssistantAvatar={!isTeamMode}
              />
              {renderAfterMessage?.(item.message)}
            </Fragment>
          );
        }

        if (item.type === 'reasoning' || item.type === 'toolGroup') {
          const turnKey = stableTurnKeyById.get(item.turnId) ?? item.key;
          const meta = turnWorkMeta.get(item.turnId);
          const turnFoldable = Boolean(meta?.completed && meta.hasWork);
          const turnOpen = !turnFoldable || Boolean(expandedTurns[turnKey]);
          const streak = liveStreakByItemKey.get(item.key);
          const streakOpen = !streak || Boolean(expandedStreaks[streak.id]);
          const contentOpen = turnOpen && streakOpen;
          const isFoldAnchor = turnFoldAnchorKeys.get(item.turnId) === item.key;
          const isTurnAnchor =
            Boolean(meta) &&
            (isFoldAnchor ||
              (!turnFoldAnchorKeys.has(item.turnId) &&
                (meta!.firstWorkKey === item.key ||
                  (!meta!.firstWorkKey && !chipAnchoredTurns.current.has(turnKey)))));
          if (isTurnAnchor && meta) {
            chipAnchoredTurns.current.add(turnKey);
          }

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
              />
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
              />
            );
          }

          // 折叠时交付物仍可见（不参与收起动画）
          if (!contentOpen && item.type === 'toolGroup') {
            const deliverables = filterDeliverableExecutions(item.executions);
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
                />
              );
            }
          }

          // 头像已挂在 turn/顶部 streak 上时，展开内容不再重复画。
          const turnChipOwnsAvatar = turnFoldable;
          const streakChipOwnsAvatar = Boolean(
            !turnFoldable && isTopStreakInTurn && streak?.showAvatar
          );
          const hideAvatar = Boolean(
            (turnChipOwnsAvatar && turnOpen) || (streakChipOwnsAvatar && streakOpen)
          );

          const body =
            item.type === 'reasoning' ? (
              <ReasoningSegmentBlock
                segment={item.segment}
                agentTemplateName={item.segment.agentTemplateName ?? agentTemplateNameByTurn.get(item.turnId)}
                showAvatar={hideAvatar ? false : item.showAvatar}
                teamLayout={isTeamMode}
              />
            ) : (
              <ToolGroupDisplay
                executions={item.executions}
                notices={item.notices}
                showAvatar={hideAvatar ? false : item.showAvatar}
                teamLayout={isTeamMode}
                agentTemplateName={agentTemplateNameByTurn.get(item.turnId)}
                collapseSkillTreeWhenContentStarts={item.collapseSkillTreeWhenContentStarts}
                viewedSkillIds={item.viewedSkillIds}
              />
            );

          // 可折叠时内容常驻 DOM，用与思考相同的 grid 高度过渡
          if (turnFoldable || streak) {
            nodes.push(
              <div
                key={`${timelineScope}/${item.key}-collapse`}
                className={clsx('timeline-collapse', contentOpen && 'is-open')}
                data-testid="chat-panel-timeline-collapse"
                data-variant={contentOpen ? 'open' : 'closed'}
              >
                <div className="timeline-collapse-inner">{body}</div>
              </div>
            );
          } else {
            nodes.push(<Fragment key={`${timelineScope}/${item.key}`}>{body}</Fragment>);
          }

          return nodes.length === 1 ? (
            nodes[0]
          ) : (
            <Fragment key={`${timelineScope}/work-${item.key}`}>{nodes}</Fragment>
          );
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
              showAvatar={item.showAvatar}
              agentTemplateName={agentTemplateNameByTurn.get(item.turnId)}
              teamLayout={isTeamMode}
            />
          );
        }

        return null;
      })}
      {virtualized && admittedEndExclusive < renderItems.length ? (
        <div
          ref={newerSentinelRef}
          className="chat-timeline__history-sentinel"
          aria-hidden="true"
          data-testid="chat-panel-timeline-newer-sentinel"
        />
      ) : null}
    </div>
  );
}

export function MessageList({
  messages,
  renderAfterMessage,
  canLoadOlderHistory,
  onLoadOlderHistory,
}: MessageListProps) {
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const toolExecutions = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.toolExecutions ?? new Map());
  const toolExecutionOrder = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.toolExecutionOrder ?? []);
  const mode = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.mode ?? 'agent');
  const executions = useMemo(
    () => getExecutionList(toolExecutions, toolExecutionOrder),
    [toolExecutions, toolExecutionOrder]
  );

  return (
    <ChatTimelineList
      messages={messages}
      executions={executions}
      mode={mode}
      renderAfterMessage={renderAfterMessage}
      sessionId={activeSessionId}
      canLoadOlderHistory={canLoadOlderHistory}
      onLoadOlderHistory={onLoadOlderHistory}
    />
  );
}
