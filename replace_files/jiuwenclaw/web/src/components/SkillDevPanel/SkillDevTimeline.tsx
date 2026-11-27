import { useMemo } from 'react';
import type { ToolExecution } from '../../types';
import type { SkillDevMessage } from '../../stores';
import type { SkillDevPlan, ClarifyAnswer } from '../../types/skilldev';
import { SkillDevMessageItem } from './SkillDevMessageItem';
import { SkillDevToolGroupDisplay } from './SkillDevToolGroupDisplay';

interface SkillDevTimelineProps {
  messages: SkillDevMessage[];
  toolExecutions: ToolExecution[];
  onConfirm?: (
    action: string,
    data?: {
      plan?: SkillDevPlan;
      feedback?: string;
      answers?: ClarifyAnswer[];
      messageId?: string;
    }
  ) => Promise<void> | void;
}

type TimelineItem =
  | { type: 'message'; key: string; ts: number; idx: number; message: SkillDevMessage }
  | { type: 'toolExecution'; key: string; ts: number; idx: number; execution: ToolExecution };

function toTs(value?: string): number {
  if (!value) return Number.NaN;
  const t = Date.parse(value);
  return Number.isNaN(t) ? Number.NaN : t;
}

function buildTimeline(messages: SkillDevMessage[], executions: ToolExecution[]): TimelineItem[] {
  const messageItems: TimelineItem[] = messages.map((m, idx) => ({
    type: 'message',
    key: `m-${m.id}`,
    ts: toTs(m.timestamp),
    idx,
    message: m,
  }));
  const execItems: TimelineItem[] = executions.map((e, idx) => ({
    type: 'toolExecution',
    key: `te-${e.toolCallId}`,
    ts: toTs(e.startedAt),
    idx: messages.length + idx,
    execution: e,
  }));
  return [...messageItems, ...execItems].sort((a, b) => {
    const av = Number.isFinite(a.ts);
    const bv = Number.isFinite(b.ts);
    if (av && bv && a.ts !== b.ts) return a.ts - b.ts;
    if (av !== bv) return av ? -1 : 1;
    return a.idx - b.idx;
  });
}

export function SkillDevTimeline({ messages, toolExecutions, onConfirm }: SkillDevTimelineProps) {
  const items = useMemo(() => buildTimeline(messages, toolExecutions), [messages, toolExecutions]);
  const renderItems = useMemo(() => {
    const result: Array<
      | { type: 'message'; key: string; message: SkillDevMessage }
      | { type: 'toolGroup'; key: string; executions: ToolExecution[] }
    > = [];
    let pendingExecutions: ToolExecution[] = [];
    const flush = () => {
      if (pendingExecutions.length === 0) return;
      result.push({
        type: 'toolGroup',
        key: `tg-${pendingExecutions[0].toolCallId}`,
        executions: pendingExecutions,
      });
      pendingExecutions = [];
    };
    for (const item of items) {
      if (item.type === 'toolExecution') {
        pendingExecutions.push(item.execution);
      } else {
        flush();
        result.push({ type: 'message', key: item.key, message: item.message });
      }
    }
    flush();
    return result;
  }, [items]);

  return (
    <>
      {renderItems.map((item) =>
        item.type === 'message' ? (
          <SkillDevMessageItem key={item.key} message={item.message} onConfirm={onConfirm} />
        ) : (
          <SkillDevToolGroupDisplay key={item.key} executions={item.executions} />
        )
      )}
    </>
  );
}
