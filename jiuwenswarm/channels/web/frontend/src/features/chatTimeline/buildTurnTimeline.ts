/**
 * 对话轮次时间线纯函数：live / history / FileViewer 共用同一套排序与折叠分组逻辑。
 */
import type { Message, ToolExecution } from '../../types';
import type { ReasoningSegment } from '../../stores/chatStore';
import { getMessageActor } from '../../components/ChatPanel/MessageItem';
import {
  collectViewedSkillIds,
  isToolExecutionFailed,
} from '../../components/ChatPanel/ToolGroupDisplay';
import { isTeamMemberCollaborationMessage } from '../../components/ChatPanel/teamEventUtils';
import { isGoalCompletedContent } from '../../components/GoalBar/goalCompletedMessage';
import { isA2UIClientEventContent } from '../a2ui/a2uiContent';
import { parseTimestampToMs } from '../../utils/timestamp';

const legacyMessageKeyCache = new WeakMap<Message, string>();
let legacyMessageKeyCounter = 0;

export function getMessageRenderKey(message: Message): string {
  if (message.renderKey) {
    return message.renderKey;
  }
  let key = legacyMessageKeyCache.get(message);
  if (!key) {
    legacyMessageKeyCounter += 1;
    key = `legacy-message-${legacyMessageKeyCounter}`;
    legacyMessageKeyCache.set(message, key);
  }
  return key;
}

export type TimelineItem =
  | {
      type: 'message';
      key: string;
      timestampMs: number;
      sourceIndex: number;
      message: Message;
    }
  | {
      type: 'toolExecution';
      key: string;
      timestampMs: number;
      sourceIndex: number;
      execution: ToolExecution;
    }
  | {
      type: 'reasoning';
      key: string;
      timestampMs: number;
      sourceIndex: number;
      segment: ReasoningSegment;
    };

export type RenderItem =
  | {
      type: 'message';
      key: string;
      showAvatar: boolean;
      message: Message;
      hideMeta: boolean;
      turnId: number;
    }
  | {
      type: 'toolGroup';
      key: string;
      showAvatar: boolean;
      executions: ToolExecution[];
      notices: string[];
      collapseSkillTreeWhenContentStarts: boolean;
      turnId: number;
      viewedSkillIds: string[];
    }
  | {
      type: 'reasoning';
      key: string;
      showAvatar: boolean;
      segment: ReasoningSegment;
      turnId: number;
    }
  | {
      type: 'turnSummary';
      key: string;
      turnId: number;
      startMs: number;
      endMs: number;
      /** 工作活动跨度（工具/思考/助手气泡），不含用户消息，供「已完成」耗时 */
      workStartMs: number;
      workEndMs: number;
      isLastTurn: boolean;
      hasWork: boolean;
      /** 时间行插在本轮内容顶部，接管了本轮顶部头像时为 true */
      showAvatar: boolean;
    };

/**
 * 将普通消息与工具执行合并为统一时间线，按时间升序渲染。
 */
export function toTimestampMs(value: string | undefined): number {
  return parseTimestampToMs(value);
}

function compareTimelineItems(a: TimelineItem, b: TimelineItem): number {
  const aTsValid = Number.isFinite(a.timestampMs);
  const bTsValid = Number.isFinite(b.timestampMs);
  if (aTsValid && bTsValid && a.timestampMs !== b.timestampMs) {
    return a.timestampMs - b.timestampMs;
  }
  if (aTsValid !== bTsValid) {
    return aTsValid ? -1 : 1;
  }
  return a.sourceIndex - b.sourceIndex;
}

export function buildTimelineItems(
  messages: Message[],
  executions: ToolExecution[],
  reasoningSegments: ReasoningSegment[]
): TimelineItem[] {
  const messageItems: TimelineItem[] = messages
    .filter((msg) => {
      if (msg.role === 'tool') return false;
      if (msg.role === 'user' && isA2UIClientEventContent(msg.content)) return false;
      return true;
    })
    .map((message, index) => ({
      type: 'message',
      key: getMessageRenderKey(message),
      timestampMs: toTimestampMs(message.timestamp),
      sourceIndex: index,
      message,
    }));

  const executionItems: TimelineItem[] = executions.map((execution, index) => ({
    type: 'toolExecution',
    key: `tool-execution-${execution.toolCallId}`,
    timestampMs: toTimestampMs(execution.startedAt),
    sourceIndex: messages.length + index,
    execution,
  }));

  const reasoningItems: TimelineItem[] = reasoningSegments.map((segment, index) => ({
    type: 'reasoning',
    key: `timeline/reasoning/${segment.id}`,
    timestampMs: segment.startedAt,
    sourceIndex: messages.length + executions.length + index,
    segment,
  }));

  return [...messageItems, ...executionItems, ...reasoningItems].sort(compareTimelineItems);
}

const IMAGE_TOOL_FALLBACK_NOTICE_PREFIX = 'notice-image_tool_fallback-';

function getImageToolFallbackNoticeRequestId(message: Message): string | undefined {
  if (message.role !== 'system' || !message.id.startsWith(IMAGE_TOOL_FALLBACK_NOTICE_PREFIX)) {
    return undefined;
  }
  const requestId = message.id.slice(IMAGE_TOOL_FALLBACK_NOTICE_PREFIX.length).trim();
  return requestId || undefined;
}

function addToolGroupNotice(notices: string[], content: string) {
  const normalized = content.trim();
  if (normalized && !notices.includes(normalized)) {
    notices.push(normalized);
  }
}

function attachToolGroupNotices(renderItems: RenderItem[]): RenderItem[] {
  const nextItems = renderItems.map((item) =>
    item.type === 'toolGroup'
      ? { ...item, notices: [...item.notices] }
      : item
  );
  const groupsByRequestId = new Map<string, Extract<RenderItem, { type: 'toolGroup' }>[]>();

  for (const item of nextItems) {
    if (item.type !== 'toolGroup') {
      continue;
    }
    const requestIds = new Set(
      item.executions
        .map((execution) => execution.requestId)
        .filter((requestId): requestId is string => Boolean(requestId))
    );
    for (const requestId of requestIds) {
      const groups = groupsByRequestId.get(requestId) || [];
      groups.push(item);
      groupsByRequestId.set(requestId, groups);
    }
  }

  const attachedNoticeIndexes = new Set<number>();
  nextItems.forEach((item, index) => {
    if (item.type !== 'message') {
      return;
    }
    const requestId = getImageToolFallbackNoticeRequestId(item.message);
    if (!requestId) {
      return;
    }
    const targetGroup = groupsByRequestId.get(requestId)?.[0];
    if (!targetGroup) {
      return;
    }
    addToolGroupNotice(targetGroup.notices, item.message.content);
    attachedNoticeIndexes.add(index);
  });

  return nextItems.filter((_, index) => !attachedNoticeIndexes.has(index));
}

function isToolGroupVisible(
  item: Extract<RenderItem, { type: 'toolGroup' }>,
  isTeamMode: boolean
): boolean {
  if (!isTeamMode) {
    return item.executions.length > 0;
  }
  return item.executions.some((execution) => !execution.toolCall.memberName);
}

function consolidateReasoning(items: RenderItem[], isTeamMode: boolean): RenderItem[] {
  const out: RenderItem[] = [];
  for (const item of items) {
    if (item.type === 'toolGroup' && !isToolGroupVisible(item, isTeamMode)) {
      continue;
    }
    if (item.type === 'reasoning') {
      const prev = out[out.length - 1];
      if (prev && prev.type === 'reasoning') {
        const mergedText = [prev.segment.text, item.segment.text]
          .filter((text) => text.trim())
          .join('\n\n');
        // 合并时推进末帧时刻，避免后一段较新的 updatedAt 被前一段覆盖导致耗时少算
        const mergedUpdatedAt =
          Math.max(prev.segment.updatedAt ?? 0, item.segment.updatedAt ?? 0) || undefined;
        out[out.length - 1] = {
          ...prev,
          segment: {
            ...prev.segment,
            text: mergedText,
            closed: item.segment.closed,
            updatedAt: mergedUpdatedAt,
          },
        };
        continue;
      }
    }
    out.push(item);
  }
  return out;
}

export function buildRenderItems(items: TimelineItem[], isTeamMode: boolean, isProcessing: boolean): RenderItem[] {
  const renderItems: RenderItem[] = [];
  let currentTurnId = 0;
  let pendingToolExecutions: ToolExecution[] = [];

  const flushToolGroup = (collapseSkillTreeWhenContentStarts = false) => {
    if (pendingToolExecutions.length === 0) {
      return;
    }
    renderItems.push({
      type: 'toolGroup',
      key: `tool-group-${pendingToolExecutions[0].toolCallId}`,
      showAvatar: true,
      executions: pendingToolExecutions,
      notices: [],
      collapseSkillTreeWhenContentStarts,
      turnId: currentTurnId,
      viewedSkillIds: [],
    });
    pendingToolExecutions = [];
  };

  const pushMessage = (item: Extract<TimelineItem, { type: 'message' }>) => {
    // 主动推荐消息是系统后台触发的主 agent 话术，不是用户这一轮的回复。
    // assistant 消息默认沿用 currentTurnId（与上一轮同 turn），会让推荐消息并入
    // 上一轮 turn——既存 proactive 补丁（buildTurnWorkMeta）据此把该 turn 的
    // hasWork 置 false，误伤上一轮：上一轮 reasoning 从折叠的 turn chip
    // （「已完成」）变成展开的 ReasoningBlock（「已完成思考」+ team_leader avatar）。
    // 给 proactive 消息推进一个独立 turnId，自成一块。insertTurnSummaries 里
    // 对 proactive 消息做了同样的 flush+turnId+1，两者保持同步。
    if (item.message.role !== 'user' && item.message.isProactiveRecommendation) {
      currentTurnId += 1;
    }
    renderItems.push({
      type: 'message',
      key: item.key,
      showAvatar: true,
      message: item.message,
      hideMeta: false,
      turnId: item.message.role === 'user' ? -1 : currentTurnId,
    });
  };

  for (const item of items) {
    if (item.type === 'toolExecution') {
      pendingToolExecutions.push(item.execution);
      continue;
    }

    if (item.type === 'reasoning') {
      flushToolGroup(true);
      renderItems.push({
        type: 'reasoning',
        key: item.key,
        showAvatar: true,
        segment: item.segment,
        turnId: currentTurnId,
      });
      continue;
    }

    if (isTeamMemberCollaborationMessage(item.message)) {
      continue;
    }

    flushToolGroup(true);
    pushMessage(item);

    if (item.message.role === 'user') {
      currentTurnId += 1;
    }
  }

  flushToolGroup();

  const activeTurnId = currentTurnId;
  let laterAssistantInTurn = false;
  for (let i = renderItems.length - 1; i >= 0; i -= 1) {
    const renderItem = renderItems[i];
    if (renderItem.type !== 'message') {
      continue;
    }
    if (renderItem.message.role === 'user') {
      laterAssistantInTurn = false;
      continue;
    }
    // Goal 完成卡片是该目标的结论卡，不是「中间文字」：自己永不折进「已完成」，
    // 也不能顶掉它上面那条真正的收尾回答（否则完成卡一到，最后一条回答就被折走）。
    if (isGoalCompletedContent(renderItem.message.content)) {
      renderItem.hideMeta = false;
      continue;
    }
    const isAssistantReply =
      renderItem.message.role === 'assistant' || getMessageActor(renderItem.message) === 'team_leader';
    if (isAssistantReply) {
      const inRunningTurn = isProcessing && renderItem.turnId === activeTurnId;
      renderItem.hideMeta = laterAssistantInTurn || inRunningTurn;
      // 主动推荐消息是系统后台插入的推荐卡片，不是用户这一轮的后续回复。
      // 若让它置 laterAssistantInTurn=true，会把它前面的上一轮回复当成「中间文字」
      // 折叠进 turn chip，导致上一轮 agent 回复正文被整个收起（只剩「已完成」）。
      // proactive 消息自成一块，不影响其前方回复的折叠判定。
      if (!renderItem.message.isProactiveRecommendation) {
        laterAssistantInTurn = true;
      }
    }
  }

  const viewedSkillIdsByTurn = new Map<number, string[]>();
  for (const renderItem of renderItems) {
    if (renderItem.type !== 'toolGroup') {
      continue;
    }
    const viewedSkillIds = collectViewedSkillIds(renderItem.executions);
    if (viewedSkillIds.length === 0) {
      continue;
    }
    const current = viewedSkillIdsByTurn.get(renderItem.turnId) || [];
    viewedSkillIdsByTurn.set(renderItem.turnId, Array.from(new Set([...current, ...viewedSkillIds])));
  }
  for (const renderItem of renderItems) {
    if (renderItem.type === 'toolGroup') {
      renderItem.viewedSkillIds = viewedSkillIdsByTurn.get(renderItem.turnId) || [];
    }
  }

  const renderItemsWithNotices = consolidateReasoning(
    attachToolGroupNotices(renderItems),
    isTeamMode
  );

  assignTurnTopAvatars(renderItemsWithNotices, isTeamMode);
  return insertTurnSummaries(renderItemsWithNotices, isProcessing);
}

/**
 * 同一轮（同一 turnId）里，leader/助手/思考/工具只允许最顶部一颗头像。
 * 成员气泡可保留自己的头像，但不得重置 leader 簇（否则同轮会冒出一串头像）。
 */
function assignTurnTopAvatars(items: RenderItem[], isTeamMode: boolean): void {
  const claimedLeaderTurns = new Set<number>();

  const claimLeaderAvatar = (turnId: number): boolean => {
    if (claimedLeaderTurns.has(turnId)) {
      return false;
    }
    claimedLeaderTurns.add(turnId);
    return true;
  };

  for (const item of items) {
    if (item.type === 'reasoning' || item.type === 'toolGroup') {
      item.showAvatar = claimLeaderAvatar(item.turnId);
      continue;
    }

    if (item.type !== 'message') {
      continue;
    }

    if (item.message.role === 'user') {
      item.showAvatar = false;
      continue;
    }

    if (!isTeamMode) {
      item.showAvatar =
        item.message.role === 'assistant' ? claimLeaderAvatar(item.turnId) : false;
      continue;
    }

    const actor = getMessageActor(item.message);
    if (actor === 'team_leader' || item.message.role === 'assistant') {
      item.showAvatar = claimLeaderAvatar(item.turnId);
      continue;
    }

    // 其他成员：自己的头像；不影响本轮 leader 是否已占用顶部位
    item.showAvatar = Boolean(actor);
  }
}

function insertTurnSummaries(items: RenderItem[], isProcessing: boolean): RenderItem[] {
  const out: RenderItem[] = [];
  let startMs = Number.POSITIVE_INFINITY;
  let endMs = Number.NEGATIVE_INFINITY;
  let workStartMs = Number.POSITIVE_INFINITY;
  let workEndMs = Number.NEGATIVE_INFINITY;
  let hasActivity = false;
  let hasWork = false;
  let turnId = 0;
  let seq = 0;
  // 时间行插入点：本轮首条 assistant 内容之前（视觉上位于头像下第一行）。
  let turnContentStart = 0;

  const acc = (value: number, asWork = false) => {
    if (!Number.isFinite(value) || value <= 0) return;
    if (value < startMs) startMs = value;
    if (value > endMs) endMs = value;
    if (asWork) {
      if (value < workStartMs) workStartMs = value;
      if (value > workEndMs) workEndMs = value;
    }
  };
  const flush = (isLastTurn: boolean) => {
    const shouldShow = (isLastTurn && isProcessing) || hasActivity;
    // 整段没有任何活动（goal 插队时「上一个提问」和「设目标」两条 user 消息紧挨着，中间
    // 空窗）：不出耗时条，起止时刻也别丢，留给真正承载这段回答的那一轮当起点，否则那一轮
    // 从首次思考才开始算，耗时显示成 0s。
    const carryTimestamps = !hasActivity;
    if (shouldShow && Number.isFinite(startMs) && Number.isFinite(endMs)) {
      const summary: Extract<RenderItem, { type: 'turnSummary' }> = {
        type: 'turnSummary',
        key: `turn-summary-${seq}`,
        turnId,
        startMs,
        endMs,
        workStartMs: Number.isFinite(workStartMs) ? workStartMs : startMs,
        workEndMs: Number.isFinite(workEndMs) ? workEndMs : endMs,
        isLastTurn,
        hasWork,
        showAvatar: false,
      };
      seq += 1;
      // 时间行统一挂到本轮内容顶部：接管首条 leader/助手内容的顶部头像（与折叠条同规则）；
      // 成员自己的头像不动，时间行不带头像直接排在成员消息上方。
      const firstContent = out[turnContentStart];
      if (
        firstContent &&
        (firstContent.type === 'reasoning' ||
          firstContent.type === 'toolGroup' ||
          (firstContent.type === 'message' && firstContent.message.role === 'assistant')) &&
        firstContent.showAvatar
      ) {
        summary.showAvatar = true;
        firstContent.showAvatar = false;
      }
      out.splice(turnContentStart, 0, summary);
    }
    if (!carryTimestamps) {
      startMs = Number.POSITIVE_INFINITY;
      endMs = Number.NEGATIVE_INFINITY;
    }
    workStartMs = Number.POSITIVE_INFINITY;
    workEndMs = Number.NEGATIVE_INFINITY;
    hasActivity = false;
    hasWork = false;
  };

  for (const item of items) {
    if (item.type === 'message' && item.message.role === 'user') {
      flush(false);
      turnId += 1;
      acc(toTimestampMs(item.message.timestamp), false);
      out.push(item);
      turnContentStart = out.length;
      continue;
    }
    // slash 命令结果不属于上一轮 assistant 工作，也不应产生自己的「任务用时」。
    // 先收束上一轮，再把 BTW/compact 等命令结果作为独立时间线块插入。
    if (item.type === 'message' && item.message.isCommandOutput) {
      flush(false);
      out.push(item);
      turnContentStart = out.length;
      continue;
    }
    // 主动推荐消息自成一块（与 buildRenderItems 里推进 currentTurnId 对齐）：
    // 先 flush 掉上一轮，再 +1 进入新 turn，避免推荐消息并入上一轮导致
    // buildTurnWorkMeta 的 proactive 补丁误把上一轮 hasWork 置 false。
    let startsOwnBlock = false;
    if (
      item.type === 'message' &&
      item.message.role !== 'user' &&
      item.message.isProactiveRecommendation
    ) {
      flush(false);
      turnId += 1;
      startsOwnBlock = true;
    }
    if (item.type === 'toolGroup') {
      hasActivity = true;
      hasWork = true;
      turnId = item.turnId;
      for (const execution of item.executions) {
        // 只采信事件时间：startedAt 始终可用；updatedAt 仅在真实结束（completed/error）时计入。
        // pending/timeout 的 updatedAt 常被巡检写成 Date.now()，会把「已完成」撑成跨夜几小时。
        acc(toTimestampMs(execution.startedAt), true);
        if (execution.status === 'completed' || execution.status === 'error') {
          acc(toTimestampMs(execution.updatedAt), true);
        }
      }
    } else if (item.type === 'message') {
      hasActivity = true;
      // timestamp：首包/落盘时间（排序用）；completedAt：chat.final 收尾（live 流式合并时才有）
      acc(toTimestampMs(item.message.timestamp), true);
      acc(toTimestampMs(item.message.completedAt), true);
    } else if (item.type === 'reasoning') {
      hasActivity = true;
      hasWork = true;
      turnId = item.turnId;
      // reasoning.startedAt 必须是真实 epoch ms；忽略 0/过小哨兵，避免撑爆耗时
      if (item.segment.startedAt > 1_000_000_000_000) {
        acc(item.segment.startedAt, true);
      }
      // 每个 delta 到达都推进 updatedAt，作为不依赖收尾事件的耗时终点兜底
      if (typeof item.segment.updatedAt === 'number' && item.segment.updatedAt > 1_000_000_000_000) {
        acc(item.segment.updatedAt, true);
      }
      if (typeof item.segment.closedAt === 'number' && item.segment.closedAt > 1_000_000_000_000) {
        acc(item.segment.closedAt, true);
      }
    }
    out.push(item);
    if (startsOwnBlock) {
      // 主动推荐卡自成一块：时间行插在卡片之后、后续内容之前。
      turnContentStart = out.length;
    }
  }
  flush(true);
  return out;
}

/** 折叠芯片图标色：全成功绿勾 / 有失败但还有成功项→部分失败 / 全失败红叉 / 无工具中性绿 */
export type WorkOutcomeTone = 'success' | 'partial' | 'error' | 'neutral';

/**
 * @param successCount 成功的工具数
 * @param failedCount 失败/超时的工具数
 * @param thinkingCount 已完成的思考段数（思考成功也算「有成功项」，避免「2 次思考 + 1 次工具失败」误标红叉）
 */
export function resolveWorkOutcomeTone(
  successCount: number,
  failedCount: number,
  thinkingCount = 0
): WorkOutcomeTone {
  const hasSuccessWork = successCount > 0 || thinkingCount > 0;
  if (failedCount <= 0) {
    return hasSuccessWork ? 'success' : 'neutral';
  }
  if (!hasSuccessWork) {
    return 'error';
  }
  return 'partial';
}

function accumulateToolOutcomes(
  executions: ToolExecution[],
  into: { toolSuccessCount: number; toolFailedCount: number }
): void {
  for (const execution of executions) {
    if (isDeliverableToolName(execution.toolCall.name)) {
      continue;
    }
    if (isExecutionRunning(execution)) {
      continue;
    }
    if (isToolExecutionFailed(execution)) {
      into.toolFailedCount += 1;
    } else {
      into.toolSuccessCount += 1;
    }
  }
}

export type TurnWorkMeta = {
  turnId: number;
  completed: boolean;
  hasWork: boolean;
  firstWorkKey: string | null;
  startMs: number;
  endMs: number;
  workStartMs: number;
  workEndMs: number;
  showAvatar: boolean;
  thinkingCount: number;
  toolCount: number;
  toolSuccessCount: number;
  toolFailedCount: number;
  outcomeTone: WorkOutcomeTone;
};

const DELIVERABLE_TOOL_NAMES = new Set([
  'send_file_to_user',
  'generate_image',
  'generate_video',
]);

export function isDeliverableToolName(name: string | undefined): boolean {
  return Boolean(name && DELIVERABLE_TOOL_NAMES.has(name));
}

export function messageHasDeliverable(message: Message): boolean {
  return Boolean(message.fileItems?.length || message.mediaItems?.length);
}

export function filterDeliverableExecutions(executions: ToolExecution[]): ToolExecution[] {
  return executions.filter((execution) => isDeliverableToolName(execution.toolCall.name));
}

function isExecutionRunning(execution: ToolExecution): boolean {
  if (
    execution.status === 'completed' ||
    execution.status === 'error' ||
    execution.status === 'timeout'
  ) {
    return false;
  }
  if (execution.result) {
    return false;
  }
  return execution.status === 'pending';
}

function countToolsInGroup(item: Extract<RenderItem, { type: 'toolGroup' }>): number {
  return item.executions.filter((execution) => !isDeliverableToolName(execution.toolCall.name)).length;
}

function emptyTurnMeta(turnId: number, partial?: Partial<TurnWorkMeta>): TurnWorkMeta {
  return {
    turnId,
    completed: false,
    hasWork: false,
    firstWorkKey: null,
    startMs: Number.NaN,
    endMs: Number.NaN,
    workStartMs: Number.NaN,
    workEndMs: Number.NaN,
    showAvatar: true,
    thinkingCount: 0,
    toolCount: 0,
    toolSuccessCount: 0,
    toolFailedCount: 0,
    outcomeTone: 'neutral',
    ...partial,
  };
}

export function buildTurnWorkMeta(items: RenderItem[], isProcessing: boolean): Map<number, TurnWorkMeta> {
  const map = new Map<number, TurnWorkMeta>();
  let lastTurnId = Number.NEGATIVE_INFINITY;
  for (const item of items) {
    if (item.type === 'turnSummary') {
      lastTurnId = Math.max(lastTurnId, item.turnId);
      const prev = map.get(item.turnId);
      map.set(
        item.turnId,
        emptyTurnMeta(item.turnId, {
          completed: !(item.isLastTurn && isProcessing),
          hasWork: item.hasWork || Boolean(prev?.hasWork),
          firstWorkKey: prev?.firstWorkKey ?? null,
          startMs: item.startMs,
          endMs: item.endMs,
          workStartMs: item.workStartMs,
          workEndMs: item.workEndMs,
          showAvatar: prev?.showAvatar ?? true,
          thinkingCount: prev?.thinkingCount ?? 0,
          toolCount: prev?.toolCount ?? 0,
          toolSuccessCount: prev?.toolSuccessCount ?? 0,
          toolFailedCount: prev?.toolFailedCount ?? 0,
          outcomeTone: prev?.outcomeTone ?? 'neutral',
        })
      );
      continue;
    }
    if (item.type !== 'reasoning' && item.type !== 'toolGroup') {
      continue;
    }
    lastTurnId = Math.max(lastTurnId, item.turnId);
    const prev = map.get(item.turnId);
    if (prev) {
      if (!prev.firstWorkKey) {
        prev.firstWorkKey = item.key;
        prev.showAvatar = item.showAvatar;
      }
      prev.hasWork = true;
      if (item.type === 'reasoning') {
        prev.thinkingCount += 1;
      } else {
        prev.toolCount += countToolsInGroup(item);
        accumulateToolOutcomes(item.executions, prev);
      }
    } else {
      const next = emptyTurnMeta(item.turnId, {
        hasWork: true,
        firstWorkKey: item.key,
        showAvatar: item.showAvatar,
        thinkingCount: item.type === 'reasoning' ? 1 : 0,
        toolCount: item.type === 'toolGroup' ? countToolsInGroup(item) : 0,
      });
      if (item.type === 'toolGroup') {
        accumulateToolOutcomes(item.executions, next);
      }
      map.set(item.turnId, next);
    }
  }
  // 收集含主动推荐消息的 turnId：proactive 消息是系统插入的推荐（带
  // isProactiveRecommendation 标记），不该和用户那轮混在一起触发 turn 折叠——
  // 否则 proactive 触发的主 agent 这轮（带工具/思考）会让用户上一轮回复被收起。
  // 把含 proactive 的 turn 的 hasWork 置 false，让它不 foldable，上一轮回复保持展开。
  const proactiveTurnIds = new Set<number>();
  for (const item of items) {
    if (item.type === 'message' && item.message?.isProactiveRecommendation) {
      proactiveTurnIds.add(item.turnId);
    }
  }
  for (const meta of map.values()) {
    if (meta.thinkingCount > 0 || meta.toolCount > 0) {
      meta.hasWork = true;
    }
    if (proactiveTurnIds.has(meta.turnId)) {
      meta.hasWork = false;
    }
    const isLast = Number.isFinite(lastTurnId) && meta.turnId === lastTurnId;
    meta.completed = !(isProcessing && isLast);
    meta.outcomeTone = resolveWorkOutcomeTone(
      meta.toolSuccessCount,
      meta.toolFailedCount,
      meta.thinkingCount
    );
    // 折叠条是该轮顶部锚点：只要本轮有可折叠工作，头像就归折叠条，避免被中间气泡抢走后整轮「没头像」。
    if (meta.hasWork) {
      meta.showAvatar = true;
    }
  }
  return map;
}

/**
 * 「已完成」折叠条应挂在该轮第一个可折叠项上。
 * 若工具前还有 hideMeta 开场白，锚在那句上，避免展开后开场白跑到折叠条上面。
 */
export function buildTurnFoldAnchorKeys(
  items: RenderItem[],
  turnWorkMeta: Map<number, TurnWorkMeta>
): Map<number, string> {
  const anchors = new Map<number, string>();
  for (const item of items) {
    if (item.type === 'turnSummary' || item.turnId < 0 || anchors.has(item.turnId)) {
      continue;
    }
    const meta = turnWorkMeta.get(item.turnId);
    if (!meta?.completed || !meta.hasWork) {
      continue;
    }
    if (item.type === 'message' && item.hideMeta) {
      anchors.set(item.turnId, item.key);
      continue;
    }
    if (item.type === 'reasoning' || item.type === 'toolGroup') {
      anchors.set(item.turnId, item.key);
    }
  }
  return anchors;
}

export type LiveWorkStreak = {
  id: string;
  turnId: number;
  /** 轮次内稳定序号（不绑易变的 item.key），供展开态持久化 */
  ordinal: number;
  firstKey: string;
  keys: Set<string>;
  thinkingCount: number;
  toolCount: number;
  toolSuccessCount: number;
  toolFailedCount: number;
  outcomeTone: WorkOutcomeTone;
  showAvatar: boolean;
};

export const REASONING_COLLAPSE_DELAY_MS = 2000;
const REASONING_STREAK_MERGE_EXTRA_MS = 700;
const TOOL_STREAK_SETTLE_MS = 1200;
export const STREAK_FOLD_TRANSITION_DELAY_MS = 160;

/** streak 展开态 key：绑 turnId + 轮次内 ordinal，避免 firstKey 变化导致展开态丢失 */
export function streakExpandKey(turnId: number, ordinal: number): string {
  return `streak-${turnId}-${ordinal}`;
}

/** 单轮耗时超过该阈值视为时间戳异常（历史脏数据/巡检污染），回退到更窄的 work 跨度。 */
const MAX_PLAUSIBLE_TURN_MS = 24 * 60 * 60 * 1000;

export function completedWorkDurationMs(meta: Pick<TurnWorkMeta, 'workStartMs' | 'workEndMs' | 'startMs' | 'endMs'>): number {
  // 从用户发话算到本轮最后一次工作活动，包含等待首包/思考的时间。
  // 不要用 workStart→workEnd：那会丢掉「发完后一直在想」的等待，出现思考 9s、已完成却只显示 3s。
  const start = Number.isFinite(meta.startMs) ? meta.startMs : meta.workStartMs;
  const end = Number.isFinite(meta.workEndMs) ? meta.workEndMs : meta.endMs;
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    return 0;
  }
  let duration = Math.max(0, end - start);
  if (duration <= MAX_PLAUSIBLE_TURN_MS) {
    return duration;
  }
  // 用户时间戳异常偏旧时，退回纯工作活动跨度，避免「任务用时」飙到数小时/数天。
  if (
    Number.isFinite(meta.workStartMs) &&
    Number.isFinite(meta.workEndMs) &&
    meta.workEndMs >= meta.workStartMs
  ) {
    duration = Math.max(0, meta.workEndMs - meta.workStartMs);
  }
  if (duration > MAX_PLAUSIBLE_TURN_MS) {
    return 0;
  }
  return duration;
}

/** TurnElapsed / CompletedWorkChip 共用：统一用同一套起止点，避免上下两处数字对不上。 */
export function turnElapsedRangeMs(meta: Pick<TurnWorkMeta, 'workStartMs' | 'workEndMs' | 'startMs' | 'endMs'>): {
  startMs: number;
  endMs: number;
} {
  const start = Number.isFinite(meta.startMs) ? meta.startMs : meta.workStartMs;
  const end = Number.isFinite(meta.workEndMs) ? meta.workEndMs : meta.endMs;
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    return {
      startMs: Number.isFinite(meta.startMs) ? meta.startMs : 0,
      endMs: Number.isFinite(meta.endMs) ? meta.endMs : 0,
    };
  }
  const duration = completedWorkDurationMs(meta);
  // 若走了异常回退，把展示区间收成与 duration 一致，避免 TurnElapsed 仍用脏 startMs。
  if (duration > 0 && duration !== Math.max(0, end - start) && Number.isFinite(meta.workStartMs)) {
    return { startMs: meta.workStartMs, endMs: meta.workStartMs + duration };
  }
  return { startMs: start, endMs: start + duration };
}

function isReasoningSettledForStreak(segment: ReasoningSegment, nowMs: number): boolean {
  if (!segment.closed) {
    return false;
  }
  const closedAt = segment.closedAt ?? 0;
  return nowMs - closedAt >= REASONING_COLLAPSE_DELAY_MS + REASONING_STREAK_MERGE_EXTRA_MS;
}

function isToolGroupSettledForStreak(
  item: Extract<RenderItem, { type: 'toolGroup' }>,
  nowMs: number
): boolean {
  const workExecs = item.executions.filter(
    (execution) => !isDeliverableToolName(execution.toolCall.name)
  );
  if (workExecs.length === 0) {
    return false;
  }
  if (workExecs.some(isExecutionRunning)) {
    return false;
  }
  let latestDone = 0;
  for (const execution of workExecs) {
    const ts = toTimestampMs(execution.updatedAt);
    if (Number.isFinite(ts) && ts > latestDone) {
      latestDone = ts;
    }
  }
  if (latestDone <= 0) {
    return true;
  }
  return nowMs - latestDone >= TOOL_STREAK_SETTLE_MS;
}

export function isSettlingForStreak(items: RenderItem[], nowMs: number): boolean {
  for (const item of items) {
    if (item.type === 'reasoning') {
      if (item.segment.closed && typeof item.segment.closedAt === 'number' && !isReasoningSettledForStreak(item.segment, nowMs)) {
        return true;
      }
      continue;
    }
    if (item.type !== 'toolGroup' || countToolsInGroup(item) === 0) continue;
    if (!item.executions.some(isExecutionRunning) && !isToolGroupSettledForStreak(item, nowMs)) {
      return true;
    }
  }
  return false;
}

export function streakMapFingerprint(streaks: Map<string, LiveWorkStreak>): string {
  return [...streaks.values()]
    .map(
      (streak) =>
        `${streak.id}:${streak.thinkingCount}:${streak.toolCount}:${streak.toolSuccessCount}:${streak.toolFailedCount}:${streak.outcomeTone}:${[...streak.keys].join(',')}`
    )
    .sort()
    .join('|');
}

/**
 * 只编码影响 streak 分组的字段。正文流式改字不触发重建；
 * settle 时钟滴答若尚未跨过阈值也不会改变签名。
 */
export function buildStreakInputSignature(items: RenderItem[], nowMs: number): string {
  const parts: string[] = [];
  for (const item of items) {
    if (item.type === 'turnSummary') {
      continue;
    }
    if (item.type === 'message') {
      parts.push(`m:${item.turnId}`);
      continue;
    }
    if (item.type === 'reasoning') {
      const settled = isReasoningSettledForStreak(item.segment, nowMs) ? 1 : 0;
      parts.push(
        `r:${item.key}:${item.turnId}:${item.segment.closed ? 1 : 0}:${settled}:${item.showAvatar ? 1 : 0}`
      );
      continue;
    }
    const workToolCount = countToolsInGroup(item);
    const running = item.executions.some(isExecutionRunning) ? 1 : 0;
    const settled = workToolCount > 0 && !running && isToolGroupSettledForStreak(item, nowMs) ? 1 : 0;
    parts.push(
      `t:${item.key}:${item.turnId}:${workToolCount}:${running}:${settled}:${item.showAvatar ? 1 : 0}`
    );
  }
  return parts.join('|');
}

export function buildLiveCompletedStreaks(
  items: RenderItem[],
  nowMs: number
): Map<string, LiveWorkStreak> {
  const sealed = new Map<string, LiveWorkStreak>();
  let streak: LiveWorkStreak | null = null;
  const ordinalByTurn = new Map<number, number>();

  const nextOrdinal = (turnId: number): number => {
    const n = ordinalByTurn.get(turnId) ?? 0;
    ordinalByTurn.set(turnId, n + 1);
    return n;
  };

  const seal = () => {
    if (streak && streak.thinkingCount + streak.toolCount >= 2) {
      streak.outcomeTone = resolveWorkOutcomeTone(
        streak.toolSuccessCount,
        streak.toolFailedCount,
        streak.thinkingCount
      );
      sealed.set(streak.firstKey, streak);
    }
    streak = null;
  };

  const startStreak = (item: Extract<RenderItem, { type: 'reasoning' | 'toolGroup' }>): LiveWorkStreak => {
    const ordinal = nextOrdinal(item.turnId);
    return {
      id: streakExpandKey(item.turnId, ordinal),
      turnId: item.turnId,
      ordinal,
      firstKey: item.key,
      keys: new Set(),
      thinkingCount: 0,
      toolCount: 0,
      toolSuccessCount: 0,
      toolFailedCount: 0,
      outcomeTone: 'neutral',
      showAvatar: item.showAvatar,
    };
  };

  for (const item of items) {
    if (item.type === 'turnSummary') {
      continue;
    }
    if (item.type === 'message') {
      seal();
      continue;
    }

    if (item.type === 'reasoning') {
      if (!isReasoningSettledForStreak(item.segment, nowMs)) {
        seal();
        continue;
      }
      if (!streak) {
        streak = startStreak(item);
      }
      streak.keys.add(item.key);
      streak.thinkingCount += 1;
      continue;
    }

    const workToolCount = countToolsInGroup(item);
    if (workToolCount === 0) {
      seal();
      continue;
    }
    const hasRunning = item.executions.some(isExecutionRunning);
    if (hasRunning || !isToolGroupSettledForStreak(item, nowMs)) {
      seal();
      continue;
    }
    if (!streak) {
      streak = startStreak(item);
    }
    streak.keys.add(item.key);
    streak.toolCount += workToolCount;
    accumulateToolOutcomes(item.executions, streak);
  }
  seal();
  return sealed;
}

export function formatStreakSummaryLabel(
  t: (key: string, options?: Record<string, unknown>) => string,
  thinkingCount: number,
  toolCount: number,
  outcomeTone: WorkOutcomeTone = 'neutral'
): string {
  // 工具全失败且无成功思考：文案用「失败」，不要「已完成」。
  if (outcomeTone === 'error' && toolCount > 0 && thinkingCount <= 0) {
    return t('chatUi.workFailedToolsNoDuration', { tools: toolCount });
  }
  if (thinkingCount > 0 && toolCount > 0) {
    return t('chatUi.workCompletedBothNoDuration', { thinking: thinkingCount, tools: toolCount });
  }
  if (thinkingCount > 0) {
    return t('chatUi.workCompletedThinkingNoDuration', { thinking: thinkingCount });
  }
  if (toolCount > 0) {
    return t('chatUi.workCompletedToolsNoDuration', { tools: toolCount });
  }
  return t('chatUi.workCompletedFallback');
}
