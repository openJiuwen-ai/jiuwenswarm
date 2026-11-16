import { copy, reasonOf } from '../copy';
import type { PaneState } from '../paneController';
import { h, syncChildren } from './dom';
import { icon, type IconName } from './icons';

/** One line of the tray: the pane's state the conversation has no place for, and what the user can do about it. */
export interface TrayItem {
  key: string;
  tone: 'info' | 'warning' | 'error';
  icon: IconName;
  text: string;
  action?: { label: string; run: () => void };
  dismiss?: () => void;
}

const seconds = (at: number, now: number): number => Math.max(0, Math.ceil((at - now) / 1000));

/** The tray's lines for ``state`` at ``now``, the most pressing first. */
export function trayItems(
  { connection, attachment, alert }: Pick<PaneState, 'connection' | 'attachment' | 'alert'>,
  now: number,
  actions: { continueHere: () => void; dismissAlert: () => void },
  noun: string,
): TrayItem[] {
  const items: TrayItem[] = [];
  if (connection.kind === 'down') {
    const left = seconds(connection.retryAt, now);
    items.push({
      key: 'connection', tone: 'warning', icon: left ? 'unplugged' : 'spinner',
      text: left ? copy.connectionDown(left, connection.attempt) : copy.reconnecting,
    });
  } else if (attachment.kind === 'detached') {
    items.push({ key: 'attachment', tone: 'info', icon: 'spinner', text: copy.connecting(noun) });
  }
  if (attachment.kind === 'displaced') {
    items.push({
      key: 'attachment', tone: 'info', icon: 'info', text: copy.displaced(noun),
      action: { label: copy.continueHere, run: actions.continueHere },
    });
  }
  if (attachment.kind === 'failed') {
    items.push({
      key: 'attachment', tone: 'error', icon: 'alert', text: copy.failed(noun, reasonOf(attachment.error)),
      action: { label: copy.retry, run: actions.continueHere },
    });
  }
  if (alert) {
    items.push({
      key: 'alert', tone: alert.tone, icon: alert.tone === 'error' ? 'alert' : 'info', text: alert.text,
      dismiss: actions.dismissAlert,
    });
  }
  return items;
}

/**
 * The stack above the composer. A line keeps its node while only its text
 * changes, as a countdown's does every second, so a focused button in it keeps
 * the focus.
 */
export class Tray {
  readonly element = h('div', { class: 'tray' });
  private nodes = new Map<string, { item: TrayItem; node: HTMLElement; text: HTMLElement }>();

  update(items: readonly TrayItem[]): void {
    const next = new Map<string, { item: TrayItem; node: HTMLElement; text: HTMLElement }>();
    for (const item of items) {
      const before = this.nodes.get(item.key);
      if (before && sameShape(before.item, item)) {
        before.text.textContent = item.text;
        next.set(item.key, { ...before, item });
        continue;
      }
      const text = h('span', { class: 'tray-text' }, item.text);
      const node = h('div', { class: `tray-item tone-${item.tone}` },
        icon(item.icon, 'tray-icon'), text,
        item.action ? h('button', { type: 'button', class: 'tray-action', onclick: item.action.run }, item.action.label)
          : null,
        item.dismiss ? h('button', {
          type: 'button', class: 'icon-button tray-dismiss', 'aria-label': copy.dismiss, title: copy.dismiss,
          onclick: item.dismiss,
        }, icon('close')) : null);
      next.set(item.key, { item, node, text });
    }
    this.nodes = next;
    syncChildren(this.element, [...next.values()].map(value => value.node));
    this.element.hidden = !next.size;
  }
}

function sameShape(a: TrayItem, b: TrayItem): boolean {
  return a.tone === b.tone && a.icon === b.icon && a.action?.label === b.action?.label && !a.dismiss === !b.dismiss;
}
