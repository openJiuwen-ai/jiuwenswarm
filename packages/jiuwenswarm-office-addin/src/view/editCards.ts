/**
 * The suggestion cards under a ``propose_doc_edits`` call: per edit its section, summary and the change word by
 * word, the alternatives to pick from, and the user's choices (apply as an edit, as a tracked change, add as a
 * comment, skip; undo once applied). The cards' state is ``DocEditReview``'s; the alternative picked is the view's.
 */
import { copy } from '../copy';
import type { AppliedAs, CardView, DocEditReview, ProposedEdit } from '../docEditReview';
import type { JsonObject } from '../gatewayClient';
import { diffRuns, paragraphLines, unescapeText } from '../wordText';
import { h } from './dom';

/** Views a host shows under some of its finished tool calls. */
export interface ToolCards {
  readonly tools: ReadonlySet<string>;
  /** Moves on whenever a card shows something new. */
  version(): string;
  subscribe(listener: () => void): void;
  /** The cards of call ``callId``, or null when it made none. */
  render(callId: string, args: JsonObject): HTMLElement | null;
}

export function editCards(review: DocEditReview): ToolCards {
  const picked = new Map<string, number>();
  const listeners = new Set<() => void>();
  let picks = 0;
  const changed = () => listeners.forEach(listener => listener());
  review.subscribe(changed);
  const pick = (key: string, alternative: number) => {
    picked.set(key, alternative);
    picks += 1;
    changed();
  };
  return {
    tools: new Set(['propose_doc_edits']),
    version: () => `${review.version}:${picks}`,
    subscribe: listener => void listeners.add(listener),
    render: (callId, args) => {
      const edits = (Array.isArray(args.edits) ? args.edits : []) as unknown as ProposedEdit[];
      const cards = edits.flatMap(edit => {
        const view = review.card(callId, edit.id);
        if (!view) return [];
        const key = `${callId}\n${edit.id}`;
        const alternative = picked.get(key) ?? 0;
        return [card(edit, view, alternative, key, {
          pick: index => pick(key, index),
          apply: as => void review.apply(callId, edit, as, alternative),
          dismiss: () => review.dismiss(callId, edit.id),
          undo: () => void review.undo(callId, edit),
        })];
      });
      return cards.length ? h('section', { class: 'entry edit-cards', 'aria-label': copy.editCards }, ...cards) : null;
    },
  };
}

interface CardActions {
  pick: (alternative: number) => void;
  apply: (as: AppliedAs) => void;
  dismiss: () => void;
  undo: () => void;
}

function card(edit: ProposedEdit, view: CardView, alternative: number, key: string, actions: CardActions):
HTMLElement {
  const { state, busy } = view;
  const options = [{ label: edit.new_text_label || copy.editSuggested, text: edit.new_text },
    ...(edit.alternatives ?? [])];
  const chosen = state.status === 'applied' ? state.alternative ?? 0 : alternative;
  // The first button keeps its key from apply to undo, so focus stays on the card when the buttons change.
  const button = (label: string, onclick: () => void, primary = false) => h('button', {
    type: 'button', class: `button edit-button${primary ? ' button-primary' : ''}`, disabled: busy, onclick,
    'data-key': `edit:${key}:${primary ? 'main' : label}`,
  }, label);
  let buttons: HTMLElement[] = [];
  if (state.status === 'pending') {
    buttons = edit.comment ? [button(copy.editComment, () => actions.apply('comment'), true)]
      : [button(copy.editApply, () => actions.apply('edit'), true),
        button(copy.editTracked, () => actions.apply('tracked')), button(copy.editComment, () => actions.apply('comment'))];
    buttons.push(button(copy.editSkip, actions.dismiss));
  } else if (state.status === 'applied') {
    buttons = [button(copy.editUndo, actions.undo, true)];
  }
  const status = busy ? copy.editBusy : state.status === 'applied' ? copy.editApplied[state.as]
    : copy.editStatus[state.status];
  const error = state.status === 'failed' ? state.error : view.error;
  return h('article', { class: `edit-card status-${state.status}` },
    h('div', { class: 'edit-head' }, h('span', { class: 'edit-summary' }, edit.change_summary),
      h('span', { class: 'chip' }, status)),
    h('p', { class: 'edit-section' }, edit.section),
    edit.comment ? h('p', { class: 'edit-diff' }, edit.comment.text)
      : diff(edit, options[chosen]?.text ?? edit.new_text),
    options.length > 1 && state.status === 'pending' ? h('div', { class: 'edit-options', role: 'radiogroup' },
      ...options.map((option, index) => h('label', { class: 'choice' },
        h('input', { type: 'radio', name: key, checked: index === chosen, disabled: busy,
          'data-key': `pick:${key}:${index}`, onchange: () => actions.pick(index) }),
        h('span', { class: 'choice-text' }, option.label)))) : null,
    edit.reasoning ? h('p', { class: 'edit-reason' }, edit.reasoning) : null,
    error ? h('p', { class: 'edit-error', role: 'alert' }, error) : null,
    buttons.length ? h('div', { class: 'edit-actions' }, ...buttons) : null);
}

/** The old and new text word by word, between the words around them. */
function diff(edit: ProposedEdit, text: string): HTMLElement {
  const runs = diffRuns(paragraphLines(edit.old_text).join('\n'), unescapeText(text));
  return h('p', { class: 'edit-diff' },
    edit.context_before ? h('span', { class: 'edit-context' }, `…${edit.context_before}`) : null,
    ...runs.map(run => (run.kind === 'same' ? run.text : h(run.kind === 'removed' ? 'del' : 'ins', {}, run.text))),
    edit.context_after ? h('span', { class: 'edit-context' }, `${edit.context_after}…`) : null);
}
