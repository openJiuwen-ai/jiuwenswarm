import type { SessionId } from '../clientAttachment';
import { copy } from '../copy';
import type { OpenDocument, ResourceConversation } from '../sessionFacade';
import { h } from './dom';
import { dropdown } from './dropdown';
import { icon, type IconName } from './icons';

export interface HeaderState {
  conversations: readonly ResourceConversation[];
  shown: SessionId | undefined;
  /** Whether the pane can switch and start conversations now. */
  enabled: boolean;
  /** Whether the shown conversation has anything to download. */
  canDownload: boolean;
  /** The user's open documents, by conversation; Files lists the ones of this document's kind. */
  documents: ReadonlyMap<string, OpenDocument>;
}

/**
 * The top bar: icons only, leaving the top right corner to the host's own menu. Files lists the open documents of
 * this one's kind; History lists the document's conversations; More downloads the shown one.
 */
export class Header {
  readonly element: HTMLElement;
  private readonly filesButton: HTMLButtonElement;
  private readonly filesCount = h('span', { class: 'files-count' });
  private readonly filesMenu = h('div', { class: 'menu', id: 'files-menu', role: 'group' });
  private readonly historyButton = iconButton('history', copy.history);
  private readonly newButton = iconButton('compose', copy.newConversation);
  private readonly historyMenu = h('div', { class: 'menu', id: 'history-menu', role: 'group', 'aria-label': copy.history });
  private readonly history: { close: () => void };
  private readonly moreButton = iconButton('more', copy.more);
  private readonly downloadItem: HTMLButtonElement;
  private readonly moreMenu: HTMLElement;
  private last: HeaderState | undefined;

  constructor(noun: string, private readonly actions: {
    switchTo: (sessionId: SessionId) => void; newConversation: () => void; downloadTranscript: () => void;
  }) {
    this.filesButton = iconButton('files', copy.openFiles(noun));
    this.filesButton.classList.add('files-button');
    this.filesButton.append(this.filesCount);
    this.filesMenu.setAttribute('aria-label', copy.openFiles(noun));
    this.downloadItem = h('button', { type: 'button', class: 'menu-item', onclick: () => {
      more.close();
      actions.downloadTranscript();
    } }, icon('download'), h('span', { class: 'menu-item-title' }, copy.downloadTranscript));
    this.moreMenu = h('div', { class: 'menu', id: 'more-menu', role: 'group', 'aria-label': copy.more }, this.downloadItem);
    this.element = h('header', { class: 'header' }, h('div', { class: 'header-actions' },
      this.filesButton, this.historyButton, this.newButton, this.moreButton, this.filesMenu, this.historyMenu,
      this.moreMenu));
    dropdown(this.filesButton, this.filesMenu, this.element);
    this.history = dropdown(this.historyButton, this.historyMenu, this.element);
    const more = dropdown(this.moreButton, this.moreMenu, this.element);
    this.newButton.addEventListener('click', () => {
      this.history.close();
      actions.newConversation();
    });
  }

  update(state: HeaderState): void {
    const { last } = this;
    if (last && (Object.keys(state) as (keyof HeaderState)[]).every(key => state[key] === last[key])) return;
    this.last = state;
    const { conversations, shown, enabled, canDownload, documents } = state;
    const files = sameKind(documents);
    this.filesCount.textContent = files.length ? String(files.length) : '';
    this.filesMenu.replaceChildren(...files.map(file => h('div', { class: 'menu-item' },
      h('span', { class: 'menu-item-title' }, file.name),
      file.current ? h('span', { class: 'menu-item-meta' }, copy.thisFile) : null)));
    this.downloadItem.disabled = !canDownload;
    this.historyButton.disabled = !enabled;
    this.newButton.disabled = !enabled;
    if (!enabled) this.history.close();
    // The shown conversation is listed even before the list knows it, as right after it was created.
    const listed = shown && !conversations.some(item => item.sessionId === shown)
      ? [{ sessionId: shown, title: '', lastMessageAt: 0 }, ...conversations] : conversations;
    this.historyMenu.replaceChildren(...listed.map(item => {
      const current = item.sessionId === shown;
      return h('button', {
        type: 'button', class: 'menu-item', 'aria-current': current ? 'true' : undefined,
        onclick: () => {
          this.history.close();
          if (!current) this.actions.switchTo(item.sessionId);
        },
      }, h('span', { class: 'menu-item-title' }, item.title || copy.untitledConversation),
      item.lastMessageAt ? h('span', { class: 'menu-item-meta' }, relativeTime(item.lastMessageAt)) : null);
    }));
  }
}

/** The open documents of the current one's kind, the current one first; none until the list names the current one. */
function sameKind(documents: ReadonlyMap<string, OpenDocument>): OpenDocument[] {
  const all = [...documents.values()];
  const current = all.find(file => file.current);
  return current ? [current, ...all.filter(file => file !== current && file.kind === current.kind)] : [];
}

function iconButton(name: IconName, label: string): HTMLButtonElement {
  return h('button', { type: 'button', class: 'icon-button', 'aria-label': label, title: label }, icon(name));
}

/** How long ago, from seconds since the epoch, in the fewest characters. */
function relativeTime(seconds: number, now = Date.now()): string {
  const minutes = Math.floor((now - seconds * 1000) / 60_000);
  if (minutes < 1) return copy.justNow;
  if (minutes < 60) return copy.minutesAgo(minutes);
  if (minutes < 24 * 60) return copy.hoursAgo(Math.floor(minutes / 60));
  return copy.daysAgo(Math.floor(minutes / (24 * 60)));
}
