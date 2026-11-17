import { copy } from '../copy';
import { imageDataUrl, type OutgoingImageAttachment } from '../sessionFacade';
import { h } from './dom';
import { dropdown } from './dropdown';
import { icon } from './icons';
import { isImeKey } from './ime';

/** What a disabled message box waits for. */
export type Waiting = 'document' | 'displaced' | 'offline' | 'prompt' | 'history';

/** The tallest the message box grows before it scrolls. */
const MAX_INPUT_PX = 160;

/** The images a message takes, by extension, as the web chat and ``media.persist`` take them. */
const IMAGE_TYPES: Readonly<Record<string, string>> = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif',
};
const MAX_IMAGE_MB = 10;
/** ``media.persist`` keeps the first eight images of a message. */
const MAX_IMAGES = 8;

/**
 * The message box: Enter sends, Shift+Enter breaks the line. Send and Stop
 * share one button: while a turn runs and the box is empty it stops the turn,
 * and a message typed meanwhile stops it and goes on as a supplement. Images
 * picked, pasted or dropped go with the next message; a supplement carries
 * text only, so a message with images waits for the turn to end. Below the
 * box, the model the next message runs on.
 */
export class Composer {
  private readonly input = h('textarea', {
    class: 'composer-input', rows: '1', 'aria-label': copy.messageLabel, placeholder: copy.placeholder,
  });
  private readonly button = h('button', { type: 'button', class: 'send-button' });
  private readonly picker = h('input', {
    type: 'file', accept: [...new Set(Object.values(IMAGE_TYPES))].join(','), multiple: true, hidden: true,
  });
  private readonly attachButton = h('button', {
    type: 'button', class: 'icon-button attach-button', 'aria-label': copy.addImages, title: copy.addImages,
    onclick: () => this.picker.click(),
  }, icon('image'));
  private readonly thumbnails = h('ul', { class: 'composer-images', hidden: true });
  private readonly note = h('p', { class: 'composer-note', role: 'status', hidden: true });
  private readonly card = h('div', { class: 'composer-card' },
    this.thumbnails, this.attachButton, this.picker, this.input, this.button);
  private readonly modelName = h('span', { class: 'model-name' });
  private readonly modelButton = h('button', { type: 'button', class: 'model-button', title: copy.model },
    this.modelName, icon('chevron'));
  private readonly modelMenu = h('div', { class: 'menu', id: 'model-menu', role: 'group', 'aria-label': copy.model });
  private readonly modelBar = h('div', { class: 'composer-foot' }, this.modelButton, this.modelMenu);
  readonly element = h('footer', { class: 'composer' }, this.card, this.note, this.modelBar);
  private readonly models = dropdown(this.modelButton, this.modelMenu, this.modelBar);
  private shownModels: { models: readonly string[]; model: string | undefined } | undefined;
  private state = { enabled: false, running: false };
  /** The box had the focus when it was disabled, as when a question came; it gets it back when enabled. */
  private refocus = false;
  private images: readonly OutgoingImageAttachment[] = [];
  /** Images still being read; the message waits for them. */
  private reading = 0;
  /** Why the last images added were not all taken. */
  private refusal = '';

  constructor(private readonly noun: string, private readonly actions: {
    send: (text: string, images: readonly OutgoingImageAttachment[]) => Promise<boolean>;
    stop: () => void; chooseModel: (model: string) => void;
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
    this.picker.addEventListener('change', () => {
      void this.addImages([...this.picker.files ?? []]);
      this.picker.value = '';
    });
    this.input.addEventListener('paste', event => {
      // Text wins: a host may put a picture of the text it copied on the clipboard too.
      const data = event.clipboardData;
      if (!data || data.getData('text/plain')) return;
      const files = clipboardFiles(data);
      if (!files.length) return;
      event.preventDefault();
      void this.addImages(files);
    });
    this.card.addEventListener('dragover', event => {
      if (!event.dataTransfer?.types.includes('Files')) return;
      // Taken even while the box is off: a file dropped and not taken would replace the pane.
      event.preventDefault();
      this.card.classList.toggle('is-dropping', this.state.enabled);
    });
    this.card.addEventListener('dragleave', event => {
      if (!this.card.contains(event.relatedTarget as Node | null)) this.card.classList.remove('is-dropping');
    });
    this.card.addEventListener('drop', event => {
      this.card.classList.remove('is-dropping');
      const files = [...event.dataTransfer?.files ?? []];
      if (!files.length) return;
      event.preventDefault();
      if (this.state.enabled) void this.addImages(files);
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
    this.attachButton.disabled = !enabled;
    if (reenabled && this.refocus && (!document.activeElement || document.activeElement === document.body)) {
      this.input.focus();
    }
    if (reenabled) this.refocus = false;
    const disabledText: Record<Waiting, string> = {
      document: copy.placeholderDisabled(this.noun), displaced: copy.placeholderDisplaced, offline: copy.placeholderOffline,
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

  /** Take the images a message can carry; say why the others were refused. */
  private async addImages(files: readonly File[]): Promise<void> {
    const refusals: string[] = [];
    const taken: { file: File; mimeType: string }[] = [];
    for (const file of files) {
      const mimeType = imageType(file);
      if (!mimeType) refusals.push(copy.imageType(file.name));
      else if (file.size > MAX_IMAGE_MB * 1024 * 1024) refusals.push(copy.imageSize(file.name, MAX_IMAGE_MB));
      else if (this.images.length + this.reading + taken.length >= MAX_IMAGES) {
        refusals.push(copy.imageCount(MAX_IMAGES));
        break;
      } else taken.push({ file, mimeType });
    }
    this.refusal = refusals.join('；');
    this.reading += taken.length;
    this.paint();
    const read = await Promise.allSettled(taken.map(({ file, mimeType }) => readImage(file, mimeType)));
    this.reading -= taken.length;
    this.images = [...this.images, ...read.flatMap(result => (result.status === 'fulfilled' ? [result.value] : []))];
    const unread = taken.filter((_, index) => read[index].status === 'rejected');
    if (unread.length) this.refusal = [...refusals, ...unread.map(({ file }) => copy.imageRead(file.name))].join('；');
    this.paintImages();
  }

  private removeImage(image: OutgoingImageAttachment): void {
    this.images = this.images.filter(item => item !== image);
    this.refusal = '';
    this.paintImages();
    this.input.focus();
  }

  private paintImages(): void {
    this.thumbnails.hidden = !this.images.length;
    this.thumbnails.replaceChildren(...this.images.map(image => h('li', { class: 'composer-image' },
      h('img', { src: imageDataUrl(image), alt: image.filename }),
      h('button', {
        type: 'button', class: 'icon-button', 'aria-label': copy.removeImage(image.filename),
        title: copy.removeImage(image.filename), onclick: () => this.removeImage(image),
      }, icon('close')))));
    this.paint();
  }

  private hasMessage(): boolean {
    return Boolean(this.input.value.trim() || this.images.length);
  }

  /** Images the turn cannot take leave Stop in place; they wait in the box. */
  private stops(): boolean {
    return this.state.running && !this.input.value.trim();
  }

  private sendable(): boolean {
    const { enabled, running } = this.state;
    return enabled && this.hasMessage() && !this.reading && !(running && this.images.length);
  }

  private paint(): void {
    const stops = this.stops();
    const label = stops ? copy.stop : copy.send;
    this.button.replaceChildren(icon(stops ? 'stop' : 'send'));
    this.button.setAttribute('aria-label', label);
    this.button.title = label;
    this.button.classList.toggle('is-stop', stops);
    this.button.disabled = stops ? false : !this.sendable();
    const waits = this.state.running && this.images.length > 0;
    this.note.textContent = this.refusal || (waits ? copy.imagesWait : '');
    this.note.classList.toggle('is-error', Boolean(this.refusal));
    this.note.hidden = !this.note.textContent;
  }

  private fit(): void {
    this.input.style.height = 'auto';
    this.input.style.height = `${Math.min(this.input.scrollHeight, MAX_INPUT_PX)}px`;
  }

  private async submit(): Promise<void> {
    if (!this.sendable()) return;
    const text = this.input.value;
    const { images } = this;
    this.input.value = '';
    this.images = [];
    this.refusal = '';
    this.fit();
    this.paintImages();
    // A message that did not go out comes back to the box.
    if (await this.actions.send(text, images)) return;
    if (!this.input.value) {
      this.input.value = text;
      this.fit();
    }
    if (!this.images.length) this.images = images;
    this.paintImages();
  }
}

/** The image type a file is, by its MIME type, or by its name when the browser gives none. */
function imageType(file: File): string | undefined {
  const known = Object.values(IMAGE_TYPES);
  if (known.includes(file.type)) return file.type;
  if (file.type && file.type !== 'application/octet-stream') return undefined;
  const dot = file.name.lastIndexOf('.');
  return dot < 0 ? undefined : IMAGE_TYPES[file.name.slice(dot).toLowerCase()];
}

/** The files pasted: ``items`` first, as the web chat reads them, since Chromium puts a screenshot in ``files`` too. */
function clipboardFiles(data: DataTransfer): File[] {
  const items = [...data.items ?? []].filter(item => item.kind === 'file')
    .flatMap(item => item.getAsFile() ?? []);
  return items.length ? items : [...data.files ?? []];
}

function readImage(file: File, mimeType: string): Promise<OutgoingImageAttachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result);
      resolve({ filename: file.name || 'image', mimeType, base64Data: url.slice(url.indexOf(',') + 1) });
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}
