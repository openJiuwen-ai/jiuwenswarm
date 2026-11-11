import type { Prompt, PromptChoice, PromptOption, PromptQuestion } from '../conversationModel';
import { copy, toolLabel } from '../copy';
import type { JsonObject } from '../gatewayClient';
import { h } from './dom';
import { icon } from './icons';
import { isImeKey } from './ime';
import { renderMarkdown } from './markdown';

/** How long after the sheet appears or another key an Enter is taken as typing under way, not as consent. */
export const ENTER_GUARD_MS = 350;

/** The order the sheet offers the backend's permission choices; ``always_allow`` (every conversation) is not offered. */
const APPROVAL_ORDER = ['reject', 'session_allow', 'allow_once'];

/** Sends the answer; settles once it went out, or did not. */
type Answer = (choices: PromptChoice[]) => Promise<void>;

/**
 * ``send`` only while no answer is on its way: the backend takes one answer per question and turns a
 * second into an error that ends the turn. An answer that did not go out leaves the card to answer again.
 */
function once<A extends unknown[]>(send: (...args: A) => Promise<void>): (...args: A) => void {
  let sending = false;
  return (...args) => {
    if (sending) return;
    sending = true;
    void send(...args).finally(() => {
      sending = false;
    });
  };
}

export interface Interaction {
  element: HTMLElement;
  /** Stop listening to the keyboard; the interaction left the pane. */
  dispose: () => void;
}

/** Where Enter does something of its own; in a text field Escape does too. */
const OWN_ENTER = 'button, summary, a, input, textarea, select, [contenteditable]';
const OWN_ESCAPE = 'input, textarea, select, [contenteditable]';

/**
 * The backend's permission or confirmation request, pinned at the bottom of
 * the pane: what is asked, what it would act on, and the choices. Esc rejects
 * and Enter allows once, unless the focus is on a control with its own use of
 * the key; an Enter within ``ENTER_GUARD_MS`` of the sheet appearing or of
 * another key is ignored. It takes no focus of its own accord.
 */
export function approvalSheet(
  prompt: Prompt, documents: ReadonlyMap<string, string>, answer: Answer,
  now: () => number = () => performance.now(),
): Interaction {
  const [question] = prompt.questions;
  let guardStart = now();
  const known = APPROVAL_ORDER.flatMap(value => question.options.filter(option => option.value === value));
  const choices = known.length ? known : question.options;
  const choose = once((option: PromptOption) => answer([{ selected: [option] }]));
  const titleId = `approval-${prompt.requestId}`;
  const sheet = h('section', { class: 'sheet approval', role: 'region', 'aria-labelledby': titleId },
    h('div', { class: 'sheet-head' }, icon('shield', 'sheet-icon'),
      h('h2', { class: 'sheet-title', id: titleId }, copy.approvalTitle)),
    h('div', { class: 'approval-ask' }, ...(question.toolName && copy.tools[question.toolName]
      ? [document.createTextNode(copy.approvalAsk(toolLabel(question.toolName)))]
      : renderMarkdown(question.question))),
    question.toolArguments ? payloadSummary(question.toolArguments, documents) : null,
    question.toolArguments ? h('details', { class: 'approval-arguments' }, h('summary', {}, copy.approvalArguments),
      h('pre', {}, JSON.stringify(question.toolArguments, null, 2))) : null,
    h('div', { class: 'sheet-actions' }, ...choices.map(option => {
      const value = option.value ?? '';
      const key = known.length ? copy.approvalKeys[value] : undefined;
      const tone = value === 'allow_once' ? ' button-primary' : '';
      return h('button', {
        type: 'button', class: `button${tone}`, title: option.description,
        'aria-keyshortcuts': key === 'Esc' ? 'Escape' : key, onclick: () => choose(option),
      }, (known.length && copy.approvalOptions[value]) || option.label);
    })));
  const reject = known.find(option => option.value === 'reject');
  const allowOnce = known.find(option => option.value === 'allow_once');
  const onKey = (event: KeyboardEvent): void => {
    if (event.key !== 'Enter') guardStart = now();
    if (event.defaultPrevented || isImeKey(event) || event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target instanceof Element ? event.target : null;
    if (event.key === 'Escape' && reject && !target?.closest(OWN_ESCAPE)) {
      event.preventDefault();
      choose(reject);
    } else if (event.key === 'Enter' && !event.shiftKey && allowOnce && !target?.closest(OWN_ENTER)) {
      event.preventDefault();
      if (now() - guardStart >= ENTER_GUARD_MS) choose(allowOnce);
    }
  };
  document.addEventListener('keydown', onKey);
  return { element: sheet, dispose: () => document.removeEventListener('keydown', onKey) };
}

/** The arguments the user most needs to judge a request, in words; the rest stay in the full arguments. */
function payloadSummary(payload: JsonObject, documents: ReadonlyMap<string, string>): HTMLElement | null {
  const rows = Object.entries(payload).flatMap(([key, value]): [string, string][] => {
    const label = copy.payloadLabels[key];
    if (!label || (typeof value !== 'string' && typeof value !== 'number')) return [];
    return [[label, key === 'target_session_id' ? documents.get(String(value)) || copy.otherConversation : String(value)]];
  });
  if (!rows.length) return null;
  return h('dl', { class: 'approval-payload' }, ...rows.flatMap(([name, value]) => [h('dt', {}, name), h('dd', {}, value)]));
}

/** The agent's own question: one or more options, an answer of the user's own, sent together. */
export function questionCard(prompt: Prompt, answer: Answer): Interaction {
  const submit = h('button', { type: 'button', class: 'button button-primary' }, copy.submitAnswer);
  const readers = prompt.questions.map((question, index) => questionFields(prompt.requestId, question, index, () => {
    submit.disabled = !readers.every(read => read().selected.length || read().customInput);
  }));
  submit.disabled = true;
  submit.addEventListener('click', once(() => answer(readers.map(read => read()))));
  const card = h('section', { class: 'sheet question', role: 'region', 'aria-label': copy.askTitle },
    ...readers.map(read => read.fieldset),
    h('div', { class: 'sheet-actions' }, submit));
  return { element: card, dispose: () => undefined };
}

type Reader = (() => PromptChoice) & { fieldset: HTMLElement };

function questionFields(requestId: string, question: PromptQuestion, index: number, changed: () => void): Reader {
  const name = `ask-${requestId}-${index}`;
  // The backend adds an "Other" option for an answer of the user's own, which the text box below is.
  const inputs = question.options.filter(option => option.label !== 'Other').map(option => ({
    option, input: h('input', { type: question.multiSelect ? 'checkbox' : 'radio', name, onchange: changed }),
  }));
  const custom = h('textarea', {
    class: 'question-custom', rows: '1', placeholder: copy.customAnswer, 'aria-label': copy.customAnswer, oninput: changed,
  });
  const read = (): PromptChoice => ({
    selected: inputs.filter(({ input }) => input.checked).map(({ option }) => option),
    ...(custom.value.trim() ? { customInput: custom.value.trim() } : {}),
  });
  return Object.assign(read, {
    fieldset: h('fieldset', { class: 'question-fields' },
      h('legend', { class: 'question-legend' }, question.header || question.question),
      question.header ? h('div', { class: 'question-text' }, ...renderMarkdown(question.question)) : null,
      ...inputs.map(({ option, input }) => h('label', { class: 'choice' }, input,
        h('span', { class: 'choice-text' }, option.label,
          option.description ? h('small', {}, option.description) : null))),
      custom),
  });
}
