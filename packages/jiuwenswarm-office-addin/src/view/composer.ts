import { copy } from '../copy';
import { h } from './dom';
import { dropdown } from './dropdown';
import { icon } from './icons';
import { isImeKey } from './ime';

/** What a disabled message box waits for. */
export type Waiting = 'document' | 'displaced' | 'offline' | 'prompt' | 'history';

/** The tallest the message box grows before it scrolls. */
const MAX_INPUT_PX = 160;

/**
 * The message box: Enter sends, Shift+Enter breaks the line. Send and Stop
 * share one button: while a turn runs and the box is empty it stops the turn,
 * and a message typed meanwhile stops it and goes on as a supplement. Below
 * the box, the model the next message runs on.
 */
export class Composer {
  private readonly input = h('textarea', {
    class: 'composer-input', rows: '1', 'aria-label': copy.messageLabel, placeholder: copy.placeholder,
  });
  private readonly button = h('button', { type: 'button', class: 'send-button' });
  private readonly modelName = h('span', { class: 'model-name' });
  private readonly modelButton = h('button', { type: 'button', class: 'model-button', title: copy.model },
    this.modelName, icon('chevron'));
  private readonly modelMenu = h('div', { class: 'menu', id: 'model-menu', role: 'group', 'aria-label': copy.model });
  private readonly modelBar = h('div', { class: 'composer-foot' }, this.modelButton, this.modelMenu);
  readonly element = h('footer', { class: 'composer' },
    h('div', { class: 'composer-card' }, this.input, this.button), this.modelBar);
  private readonly models = dropdown(this.modelButton, this.modelMenu, this.modelBar);
  private shownModels: { models: readonly string[]; model: string | undefined } | undefined;
  private state = { enabled: false, running: false };
  /** The box had the focus when it was disabled, as when a question came; it gets it back when enabled. */
  private refocus = false;

  constructor(private readonly actions: {
    send: (text: string) => Promise<boolean>; stop: () => void; chooseModel: (model: string) => void;
  }) {
    this.button.addEventListener('click', () => (this.stops() ? actions.stop() : void this.submit()));
    this.input.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey && !isImeKey(event)) {
        event.preventDefault();
        void this.submit();
      }
    });
    this.input.addEventListener('input', () => {
      this.fit();
      this.paint();
    });
  }

  /** ``waiting`` says why a disabled box is disabled. */
  update({ enabled, running, waiting = 'document', models, model }: {
    enabled: boolean; running: boolean; waiting?: Waiting; models: readonly string[]; model: string | undefined;
  }): void {
    this.showModels(models, model);
    this.state = { enabled, running };
    if (!enabled && !this.input.disabled) this.refocus = document.activeElement === this.input;
    const reenabled = enabled && this.input.disabled;
    this.input.disabled = !enabled;
    if (reenabled && this.refocus && (!document.activeElement || document.activeElement === document.body)) {
      this.input.focus();
    }
    if (reenabled) this.refocus = false;
    const disabledText: Record<Waiting, string> = {
      document: copy.placeholderDisabled, displaced: copy.placeholderDisplaced, offline: copy.placeholderOffline,
      prompt: copy.placeholderPrompt, history: copy.placeholderLoading,
    };
    this.input.placeholder = !enabled ? disabledText[waiting] : running ? copy.placeholderRunning : copy.placeholder;
    this.paint();
  }

  private showModels(models: readonly string[], model: string | undefined): void {
    if (this.shownModels?.models === models && this.shownModels.model === model) return;
    this.shownModels = { models, model };
    this.modelBar.hidden = !models.length;
    this.modelName.textContent = model ?? copy.model;
    this.modelButton.setAttribute('aria-label', copy.modelOf(model ?? ''));
    this.modelMenu.replaceChildren(...models.map(name => h('button', {
      type: 'button', class: 'menu-item', 'aria-pressed': String(name === model),
      onclick: () => {
        this.models.close();
        this.actions.chooseModel(name);
      },
    }, h('span', { class: 'menu-item-title' }, name), name === model ? icon('check', 'menu-check') : null)));
  }

  /** Put a suggestion in the box for the user to send or change. */
  fill(text: string): void {
    this.input.value = text;
    this.fit();
    this.paint();
    this.input.focus();
  }

  private stops(): boolean {
    return this.state.running && !this.input.value.trim();
  }

  private paint(): void {
    const stops = this.stops();
    const label = stops ? copy.stop : copy.send;
    this.button.replaceChildren(icon(stops ? 'stop' : 'send'));
    this.button.setAttribute('aria-label', label);
    this.button.title = label;
    this.button.classList.toggle('is-stop', stops);
    this.button.disabled = stops ? false : !this.state.enabled || !this.input.value.trim();
  }

  private fit(): void {
    this.input.style.height = 'auto';
    this.input.style.height = `${Math.min(this.input.scrollHeight, MAX_INPUT_PX)}px`;
  }

  private async submit(): Promise<void> {
    const text = this.input.value;
    if (!text.trim() || !this.state.enabled) return;
    this.input.value = '';
    this.fit();
    this.paint();
    // A message that did not go out comes back to the box.
    if (!await this.actions.send(text) && !this.input.value) {
      this.input.value = text;
      this.fit();
      this.paint();
    }
  }
}
