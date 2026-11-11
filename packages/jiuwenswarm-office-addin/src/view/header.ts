import type { SessionId } from '../clientAttachment';
import { copy } from '../copy';
import type { ResourceConversation } from '../presentationSessionFacade';
import { h } from './dom';
import { icon, type IconName } from './icons';

const LOGO = '/assets/icon-80.png';

export interface HeaderState {
  conversations: readonly ResourceConversation[];
  shown: SessionId | undefined;
  /** Whether the pane can switch and start conversations now. */
  enabled: boolean;
  models: readonly string[];
  model: string | undefined;
}

/**
 * The top bar: icons only, leaving the top right corner to the host's own
 * menu. History lists the document's conversations; More holds the model the
 * next message runs on.
 */
export class Header {
  readonly element: HTMLElement;
  private readonly historyButton: HTMLButtonElement;
  private readonly newButton: HTMLButtonElement;
  private readonly moreButton: HTMLButtonElement;
  private readonly historyMenu = h('div', { class: 'menu', id: 'history-menu', role: 'group', 'aria-label': copy.history });
  private readonly moreMenu = h('div', { class: 'menu menu-end', id: 'more-menu', role: 'group', 'aria-label': copy.more });
  private open: { menu: HTMLElement; button: HTMLButtonElement } | undefined;
  private last: HeaderState | undefined;

  constructor(private readonly actions: {
    switchTo: (sessionId: SessionId) => void; newConversation: () => void; chooseModel: (model: string) => void;
  }) {
    this.historyButton = iconButton('history', copy.history, 'history-menu', () => this.toggle(this.historyMenu, this.historyButton));
    this.newButton = iconButton('compose', copy.newConversation, undefined, () => {
      this.close();
      actions.newConversation();
    });
    this.moreButton = iconButton('more', copy.more, 'more-menu', () => this.toggle(this.moreMenu, this.moreButton));
    this.historyMenu.hidden = true;
    this.moreMenu.hidden = true;
    this.element = h('header', { class: 'header' },
      h('img', { class: 'header-logo', src: LOGO, alt: copy.productName, width: '20', height: '20' }),
      h('div', { class: 'header-actions' }, this.historyButton, this.newButton, this.moreButton),
      this.historyMenu, this.moreMenu);
    this.element.addEventListener('keydown', event => {
      if (event.key === 'Escape' && this.open) {
        event.preventDefault();
        event.stopPropagation();
        const { button } = this.open;
        this.close();
        button.focus();
      }
    });
    document.addEventListener('pointerdown', event => {
      if (this.open && !this.element.contains(event.target as Node)) this.close();
    });
  }

  update(state: HeaderState): void {
    const { last } = this;
    if (last && (Object.keys(state) as (keyof HeaderState)[]).every(key => state[key] === last[key])) return;
    this.last = state;
    const { conversations, shown, enabled, models, model } = state;
    this.historyButton.disabled = !enabled;
    this.newButton.disabled = !enabled;
    if (!enabled && this.open) this.close();
    // The shown conversation is listed even before the list knows it, as right after it was created.
    const listed = shown && !conversations.some(item => item.sessionId === shown)
      ? [{ sessionId: shown, title: '', lastMessageAt: 0 }, ...conversations] : conversations;
    this.historyMenu.replaceChildren(h('p', { class: 'menu-title' }, copy.history), ...listed.map(item => {
      const current = item.sessionId === shown;
      return h('button', {
        type: 'button', class: 'menu-item', 'aria-current': current ? 'true' : undefined,
        onclick: () => {
          this.close();
          if (!current) this.actions.switchTo(item.sessionId);
        },
      }, h('span', { class: 'menu-item-title' }, item.title || copy.untitledConversation),
      current ? h('span', { class: 'chip tone-brand' }, copy.current)
        : item.lastMessageAt ? h('span', { class: 'menu-item-meta' }, relativeTime(item.lastMessageAt)) : null);
    }));
    this.moreMenu.replaceChildren(h('p', { class: 'menu-title' }, copy.model),
      ...(models.length ? models.map(name => h('button', {
        type: 'button', class: 'menu-item', 'aria-pressed': String(name === model),
        onclick: () => {
          this.close();
          this.actions.chooseModel(name);
        },
      }, h('span', { class: 'menu-item-title' }, name), name === model ? icon('check', 'menu-check') : null))
        : [h('p', { class: 'menu-empty' }, copy.modelsEmpty)]));
  }

  private toggle(menu: HTMLElement, button: HTMLButtonElement): void {
    const wasOpen = this.open?.menu === menu;
    this.close();
    if (wasOpen) return;
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    this.open = { menu, button };
  }

  private close(): void {
    if (!this.open) return;
    this.open.menu.hidden = true;
    this.open.button.setAttribute('aria-expanded', 'false');
    this.open = undefined;
  }
}

function iconButton(name: IconName, label: string, controls: string | undefined, onclick: () => void): HTMLButtonElement {
  return h('button', {
    type: 'button', class: 'icon-button', 'aria-label': label, title: label, onclick,
    ...(controls ? { 'aria-controls': controls, 'aria-expanded': 'false' } : {}),
  }, icon(name));
}

/** How long ago, from seconds since the epoch. */
function relativeTime(seconds: number, now = Date.now()): string {
  const minutes = Math.floor((now - seconds * 1000) / 60_000);
  if (minutes < 60) return copy.minutesAgo(minutes);
  if (minutes < 24 * 60) return copy.hoursAgo(Math.floor(minutes / 60));
  const date = new Date(seconds * 1000);
  return copy.day(date.getMonth() + 1, date.getDate());
}
