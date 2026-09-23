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
  return items.flatMap((item) => {
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
      },
    ];
  });
}

export type TimelineDisplayItem = ReturnType<typeof projectTimelineItems>[number];
