import type { RenderItem, LiveWorkStreak, TurnWorkMeta } from './buildTurnTimeline';
import { filterDeliverableExecutions } from './buildTurnTimeline';

export function projectTimelineItems(
  items: RenderItem[],
  turnWorkMeta: Map<number, TurnWorkMeta>,
  turnFoldAnchorKeys: Map<number, string>,
  streaks: Map<string, LiveWorkStreak>,
  expandedTurns: Record<string, boolean>,
  expandedStreaks: Record<string, boolean>,
) {
  const turnKeys = new Map<number, string>();
  const streakByItemKey = new Map<string, LiveWorkStreak>();
  for (const item of items) {
    if (item.type === 'turnSummary') turnKeys.set(item.turnId, item.key);
  }
  for (const streak of streaks.values()) {
    for (const key of streak.keys) streakByItemKey.set(key, streak);
  }

  // Window only rows with visible content. Hidden work must not consume the
  // viewport's range, or separate a collapsed turn's header from its answer.
  const rows = items.flatMap((item) => {
    const meta = turnWorkMeta.get(item.turnId);
    const turnKey = turnKeys.get(item.turnId) ?? item.key;
    const turnFoldable = Boolean(meta?.completed && meta.hasWork);
    const turnOpen = !turnFoldable || Boolean(expandedTurns[turnKey]);
    const streak = streakByItemKey.get(item.key);
    const streakOpen = !streak || Boolean(expandedStreaks[streak.id]);
    const contentOpen = turnOpen && streakOpen;
    const isTurnAnchor = turnFoldAnchorKeys.get(item.turnId) === item.key;
    const deliverables = item.type === 'toolGroup' && !contentOpen ? filterDeliverableExecutions(item.executions) : [];
    if (
      (item.type === 'reasoning' || item.type === 'toolGroup') &&
      !contentOpen &&
      !(turnOpen && streak?.firstKey === item.key) &&
      deliverables.length === 0
    )
      return [];

    return [
      {
        key: item.key,
        item,
        turnKey,
        meta,
        turnFoldable,
        turnOpen,
        streak,
        streakOpen,
        contentOpen,
        isTurnAnchor,
        deliverables,
        stoppedTurnEnd: false,
      },
    ];
  });

  // A turn the user stopped keeps its partial output; flag its last visible row
  // so the list can close it with a stopped marker. Turns end where
  // buildRenderItems ends them: at a user message or at slash-command output,
  // which stands alone and belongs to no turn.
  let stopped = false;
  rows.forEach((row, index) => {
    if (isUserMessage(row.item)) stopped = row.item.message.inputOutcome === 'stopped';
    else if (isCommandOutput(row.item)) stopped = false;
    const next = rows[index + 1];
    row.stoppedTurnEnd = stopped && (!next || isUserMessage(next.item) || isCommandOutput(next.item));
  });
  return rows;
}

function isUserMessage(item: RenderItem): item is Extract<RenderItem, { type: 'message' }> {
  return item.type === 'message' && item.message.role === 'user';
}

function isCommandOutput(item: RenderItem): boolean {
  return item.type === 'message' && Boolean(item.message.isCommandOutput);
}

export type TimelineDisplayItem = ReturnType<typeof projectTimelineItems>[number];
