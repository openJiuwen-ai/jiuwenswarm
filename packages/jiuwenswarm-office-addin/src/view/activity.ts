import type { Entry } from '../conversationModel';
import { copy, toolLabel } from '../copy';
import { h } from './dom';
import { icon, type IconName } from './icons';

/** The steps of a turn the timeline shows: the tool calls, a task sent to another document among them. */
export type Step = Extract<Entry, { kind: 'tool' | 'outgoing' }>;

/** When the pane saw each step start and finish; a replayed step has neither. */
export class StepClock {
  private readonly times = new Map<string, { start: number; end?: number }>();

  note(steps: readonly Step[], now: number): void {
    for (const step of steps) {
      const time = this.times.get(step.id);
      if (!time && running(step)) this.times.set(step.id, { start: now });
      else if (time && time.end === undefined && !running(step)) time.end = now;
    }
  }

  /** The step's start, and its end once done. */
  of(id: string): { start: number; end?: number } | undefined {
    return this.times.get(id);
  }
}

export function running(step: Step): boolean {
  return step.status === 'running' || step.status === 'sending';
}

export interface ActivityView {
  documents: ReadonlyMap<string, string>;
  clock: StepClock;
  expanded: boolean;
  /** The one step whose details are open, if any. */
  openStep?: string;
  toggleGroup: () => void;
  toggleStep: (id: string) => void;
}

/**
 * One run of steps as one quiet line: the running step and its live seconds,
 * then a summary once all are done. The line opens the vertical timeline of
 * every step's purpose.
 */
export function activityGroup(id: string, steps: readonly Step[], view: ActivityView): HTMLElement {
  const active = steps.find(running);
  const listId = `steps-${id}`;
  const head = h('button', {
    type: 'button', class: 'activity-head', 'aria-expanded': String(view.expanded), 'aria-controls': listId,
    'data-key': `group:${id}`, onclick: view.toggleGroup,
  },
  icon(active ? 'spinner' : 'check', 'activity-icon'),
  h('span', { class: 'activity-summary' }, active ? stepLabel(active, view.documents)
    : steps.length === 1 ? stepLabel(steps[0], view.documents) : copy.process),
  seconds(groupTime(steps, view.clock), 'activity-time'),
  icon('chevron', 'activity-chevron'));
  const list = h('ol', { class: 'activity-steps', id: listId },
    ...steps.map(step => stepRow(step, view)));
  list.hidden = !view.expanded;
  return h('section', { class: `entry activity${active ? ' is-running' : ''}` }, head, list);
}

function stepLabel(step: Step, documents: ReadonlyMap<string, string>): string {
  return step.kind === 'outgoing'
    ? copy.outgoingTo(documents.get(step.targetSessionId) || copy.otherConversation)
    : toolLabel(step.name);
}

function groupTime(steps: readonly Step[], clock: StepClock): { start: number; end?: number } | undefined {
  const times = steps.map(step => clock.of(step.id));
  if (times.some(time => !time)) return undefined;
  const known = times as { start: number; end?: number }[];
  const ends = known.map(time => time.end);
  return {
    start: Math.min(...known.map(time => time.start)),
    end: ends.every(end => end !== undefined) ? Math.max(...(ends as number[])) : undefined,
  };
}

/** Seconds since ``start``: a running one carries ``data-start`` and the pane's clock moves it on. */
function seconds(time: { start: number; end?: number } | undefined, className: string): HTMLElement | null {
  if (!time) return null;
  const node = h('span', { class: className });
  if (time.end === undefined) node.dataset.start = String(time.start);
  node.textContent = copy.seconds(Math.max(0, Math.round(((time.end ?? Date.now()) - time.start) / 1000)));
  return node;
}

/** Move every running step's seconds on to ``now``. */
export function tickSeconds(root: HTMLElement, now: number): void {
  root.querySelectorAll<HTMLElement>('[data-start]').forEach(node => {
    node.textContent = copy.seconds(Math.max(0, Math.round((now - Number(node.dataset.start)) / 1000)));
  });
}

const STATUS_ICON: Record<Step['status'], IconName> = {
  running: 'spinner', sending: 'spinner', done: 'check', sent: 'check', failed: 'close',
};

function stepRow(step: Step, view: ActivityView): HTMLElement {
  const nodeIcon: IconName = step.kind === 'outgoing' && step.status !== 'failed' ? 'outgoing' : STATUS_ICON[step.status];
  const detail = stepDetail(step);
  const open = view.openStep === step.id && detail !== undefined;
  const detailId = `detail-${step.id}`;
  const title = h('span', { class: 'step-title' }, stepLabel(step, view.documents));
  const time = seconds(view.clock.of(step.id), 'step-time');
  const head = detail === undefined
    ? h('div', { class: 'step-head' }, title, time)
    : h('button', {
      type: 'button', class: 'step-head', 'aria-expanded': String(open), 'aria-controls': detailId,
      'data-key': `step:${step.id}`, onclick: () => view.toggleStep(step.id),
    }, title, time, icon('chevron', 'step-chevron'));
  const note = step.kind === 'outgoing' ? step.text : step.goal;
  const pre = detail === undefined ? null : h('pre', { class: 'step-detail', id: detailId }, detail);
  if (pre) pre.hidden = !open;
  return h('li', { class: `step status-${step.status}` },
    h('span', { class: 'step-node' }, icon(nodeIcon)),
    h('div', { class: 'step-body' },
      head,
      note ? h('p', { class: 'step-goal' }, note) : null,
      step.kind === 'outgoing' ? h('span', { class: 'chip' }, copy.messageStatus[step.status] ?? step.status) : null,
      step.kind === 'tool' && step.status !== 'failed' ? slideRows(step) : null,
      pre));
}

/** One row per slide a check of several slides covers; the pane runs the call at once, so they share its status. */
function slideRows(step: Extract<Step, { kind: 'tool' }>): HTMLElement | null {
  const { from_slide: from = 0, to_slide: to } = step.args ?? {};
  if (step.name !== 'verify_slides' || typeof from !== 'number' || typeof to !== 'number' || to < from || to - from > 11) {
    return null;
  }
  return h('ul', { class: 'slide-rows' }, ...Array.from({ length: to - from + 1 }, (_, offset) => h('li', {},
    icon(step.status === 'running' ? 'spinner' : 'check'), copy.slide(from + offset + 1))));
}

/** The call's arguments, shown only on request. */
function stepDetail(step: Step): string | undefined {
  if (step.kind !== 'tool' || !step.args) return undefined;
  return JSON.stringify(step.args, null, 2);
}
