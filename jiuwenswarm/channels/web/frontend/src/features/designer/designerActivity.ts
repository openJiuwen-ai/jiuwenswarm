import type { DesignerNodeActivity, DesignerNodeState } from './executionGraphTypes';
import { DESIGNER_LEADER_NODE_ID } from './executionGraphTypes';

export type DesignerActivityItem = {
  kind: string;
  text: string;
  tool: string;
  at?: number;
};

export function isDesignerLeaderNodeId(nodeId: string | null | undefined): boolean {
  return String(nodeId || '').trim() === DESIGNER_LEADER_NODE_ID;
}

export function designerActivityItems(
  state: Pick<DesignerNodeState, 'activity' | 'activity_tail' | 'activity_log'> | null | undefined,
): DesignerActivityItem[] {
  const log = (state?.activity_log || [])
    .filter((item): item is DesignerNodeActivity => Boolean(item && typeof item === 'object'))
    .map((item) => ({
      kind: String(item.kind || 'stage'),
      text: String(item.text || '').trim(),
      tool: String(item.tool || '').trim(),
      ...(typeof item.at === 'number' ? { at: item.at } : {}),
    }))
    .filter((item) => item.text || item.tool);
  if (log.length > 0) return log.slice(-8);

  const tail = (state?.activity_tail || []).map((item) => String(item || '').trim()).filter(Boolean);
  if (tail.length > 0) {
    return tail.slice(-8).map((text, index, items) => {
      const latest = index === items.length - 1 ? state?.activity : null;
      return {
        kind: String(latest?.kind || 'stage'),
        text,
        tool: latest ? String(latest.tool || '').trim() : '',
        ...(typeof latest?.at === 'number' ? { at: latest.at } : {}),
      };
    });
  }

  const latest = state?.activity;
  if (!latest) return [];
  const text = String(latest.text || '').trim();
  const tool = String(latest.tool || '').trim();
  return text || tool
    ? [{
        kind: String(latest.kind || 'stage'),
        text,
        tool,
        ...(typeof latest.at === 'number' ? { at: latest.at } : {}),
      }]
    : [];
}

export function designerActivityLines(
  state: Pick<DesignerNodeState, 'activity' | 'activity_tail' | 'activity_log'> | null | undefined,
): string[] {
  if (!state?.activity_log?.length) {
    const tail = (state?.activity_tail || []).map((item) => String(item || '').trim()).filter(Boolean);
    if (tail.length > 0) return tail.slice(-8);
    const latest = designerActivityText(state?.activity);
    return latest ? [latest] : [];
  }
  return designerActivityItems(state).map((item) => {
    if (item.tool && item.text && item.text !== item.tool) return `${item.tool} · ${item.text}`;
    return item.text || item.tool;
  });
}

export function designerActivityText(activity: DesignerNodeActivity | null | undefined): string {
  if (!activity) return '';
  const tool = String(activity.tool || '').trim();
  const text = String(activity.text || '').trim();
  if (tool && text && text !== tool) return `${tool} · ${text}`;
  return text || tool;
}
