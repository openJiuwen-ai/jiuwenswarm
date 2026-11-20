import { copy } from '../copy';
import type { QueuedMessage } from '../paneController';
import { attachmentBytes, imageDataUrl, isImage, type OutgoingAttachment } from '../sessionFacade';
import { h } from './dom';
import { dropdown } from './dropdown';
import { icon } from './icons';
import { isImeKey } from './ime';

/** What a disabled message box waits for. */
export type Waiting = 'document' | 'displaced' | 'offline' | 'prompt' | 'history';

/** The tallest the message box grows before it scrolls. */
const MAX_INPUT_PX = 160;

/**
 * The files a message takes, by extension: images as ``media.persist`` takes them, and the documents the agent reads
 * by their path once ``document.persist`` stores them.
 */
const FILE_TYPES: Readonly<Record<string, string>> = {
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif',
  '.pdf': 'application/pdf', '.doc': 'application/msword',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xls': 'application/vnd.ms-excel', '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.ppt': 'application/vnd.ms-powerpoint',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.potx': 'application/vnd.openxmlformats-officedocument.presentationml.template',
  '.csv': 'text/csv', '.txt': 'text/plain', '.json': 'application/json', '.md': 'text/markdown',
};
const MAX_IMAGE_MB = 10;
const MAX_DOCUMENT_MB = 30;
/** ``media.persist`` keeps the first eight images of a message. */
const MAX_IMAGES = 8;
const MAX_FILES = 20;
/** A read the host never ends is given up after this long, so the file stops holding the message. */
const READ_TIMEOUT_MS = 10_000;

/** A file in the box: shown from the moment it is added, sendable once read. */
interface BoxFile {
  filename: string;
  size: number;
  image: boolean;
  read?: OutgoingAttachment;
}

interface Candidate { name: string; size: number; mimeType: string | undefined }

/** Which of ``candidates`` the box can take besides what it ``holds``, and why it cannot take the others. */
function admit<C extends Candidate>(
  holds: readonly BoxFile[], candidates: readonly C[],
): { taken: (C & { mimeType: string })[]; refusals: string[] } {
  const refusals: string[] = [];
  const taken: (C & { mimeType: string })[] = [];
  let images = holds.filter(file => file.image).length;
  for (const candidate of candidates) {
    const { name, size, mimeType } = candidate;
    const image = mimeType?.startsWith('image/');
    if (!mimeType) refusals.push(copy.fileType(name));
    else if (image && size > MAX_IMAGE_MB * 1024 * 1024) refusals.push(copy.imageSize(name, MAX_IMAGE_MB));
    else if (!image && size > MAX_DOCUMENT_MB * 1024 * 1024) refusals.push(copy.documentSize(name, MAX_DOCUMENT_MB));
    else if (holds.length + taken.length >= MAX_FILES) {
      refusals.push(copy.fileCount(MAX_FILES));
      break;
    } else if (image && images >= MAX_IMAGES) refusals.push(copy.imageCount(MAX_IMAGES));
    else {
      taken.push({ ...candidate, mimeType });
      if (image) images += 1;
    }
  }
  return { taken, refusals: [...new Set(refusals)] };
}

/**
 * The message box: Enter sends, Shift+Enter breaks the line. Send and Stop
 * share one button: while a turn runs and the box is empty it stops the turn,
 * and a message written meanwhile waits in the queue above the box. Images
 * and documents picked, pasted or dropped go with the next message. Below the
 * box, the model the next message runs on.
 */
export class Composer {
  private readonly input = h('textarea', {
    class: 'composer-input', rows: '1', 'aria-label': copy.messageLabel, placeholder: copy.placeholder,
  });
  private readonly button = h('button', { type: 'button', class: 'send-button' });
  private readonly picker = h('input', {
    type: 'file', accept: Object.keys(FILE_TYPES).join(','), multiple: true, hidden: true,
  });
  private readonly attachButton = h('button', {
    type: 'button', class: 'icon-button attach-button', 'aria-label': copy.addFiles, title: copy.addFiles,
    onclick: () => this.picker.click(),
  }, icon('attach'));
  private readonly thumbnails = h('ul', { class: 'composer-images', hidden: true });
  private readonly note = h('p', { class: 'composer-note', role: 'status', hidden: true });
  private readonly queueList = h('ul', { class: 'queue', 'aria-label': copy.queue, hidden: true });
  private shownQueue: { queue: readonly QueuedMessage[]; running: boolean } | undefined;
  private readonly card = h('div', { class: 'composer-card' },
    this.thumbnails, this.attachButton, this.picker, this.input, this.button);
  private readonly modelName = h('span', { class: 'model-name' });
  private readonly modelButton = h('button', { type: 'button', class: 'model-button', title: copy.model },
    this.modelName, icon('chevron'));
  private readonly modelMenu = h('div', { class: 'menu', id: 'model-menu', role: 'group', 'aria-label': copy.model });
  private readonly modelBar = h('div', { class: 'composer-foot' }, this.modelButton, this.modelMenu);
  readonly element = h('footer', { class: 'composer' }, this.queueList, this.card, this.note, this.modelBar);
  private readonly models = dropdown(this.modelButton, this.modelMenu, this.modelBar);
  private shownModels: { models: readonly string[]; model: string | undefined } | undefined;
  private state = { enabled: false, running: false };
  /** The box had the focus when it was disabled, as when a question came; it gets it back when enabled. */
  private refocus = false;
  private files: readonly BoxFile[] = [];
  /** Why the last files added were not all taken. */
  private refusal = '';

  constructor(private readonly noun: string, private readonly actions: {
    send: (text: string, attachments: readonly OutgoingAttachment[]) => Promise<boolean>;
    stop: () => void; chooseModel: (model: string) => void;
    sendNow: (id: string) => void; unqueue: (id: string) => QueuedMessage | undefined;
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
      void this.addFiles([...this.picker.files ?? []]);
      this.picker.value = '';
    });
    this.input.addEventListener('paste', event => {
      // Text wins: a host may put a picture of the text it copied on the clipboard too.
      const data = event.clipboardData;
      if (!data || data.getData('text/plain')) return;
      const files = clipboardFiles(data);
      if (!files.length) return;
      event.preventDefault();
      void this.addFiles(files);
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
      if (this.state.enabled) void this.addFiles(files);
    });
  }

  /** ``waiting`` says why a disabled box is disabled. */
  update({ enabled, running, waiting = 'document', models, model, queue }: {
    enabled: boolean; running: boolean; waiting?: Waiting; models: readonly string[]; model: string | undefined;
    queue: readonly QueuedMessage[];
  }): void {
    this.showModels(models, model);
    this.showQueue(queue, running);
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

  /**
   * The waiting messages: one goes back to the box for editing, goes now, or is dropped. While the turn runs, one
   * with files cannot go now, as a supplement carries text only.
   */
  private showQueue(queue: readonly QueuedMessage[], running: boolean): void {
    if (this.shownQueue?.queue === queue && this.shownQueue.running === running) return;
    this.shownQueue = { queue, running };
    this.queueList.hidden = !queue.length;
    this.queueList.replaceChildren(...queue.map(item => {
      const preview = item.text.trim() || item.attachments.map(file => file.filename).join('、');
      return h('li', { class: 'queue-item' },
        h('button', {
          type: 'button', class: 'queue-text', title: copy.editQueued, onclick: () => this.edit(item.id),
        }, preview),
        item.attachments.length ? h('span', { class: 'chip' }, icon('attach'), String(item.attachments.length)) : null,
        running && item.attachments.length ? null : h('button', {
          type: 'button', class: 'icon-button', 'aria-label': copy.sendNow, title: copy.sendNow,
          onclick: () => this.actions.sendNow(item.id),
        }, icon('send')),
        h('button', {
          type: 'button', class: 'icon-button', 'aria-label': copy.removeQueued(preview),
          title: copy.removeQueued(preview), onclick: () => this.actions.unqueue(item.id),
        }, icon('close')));
    }));
  }

  /** A waiting message back in the box, after what the box already holds. */
  private edit(id: string): void {
    const item = this.actions.unqueue(id);
    if (!item) return;
    const { taken, refusals } = admit(this.files, item.attachments.map(read => (
      { read, name: read.filename, size: attachmentBytes(read), mimeType: read.mimeType })));
    this.refusal = refusals.join('；');
    this.files = [...this.files, ...taken.map(({ read }) => (
      { filename: read.filename, size: attachmentBytes(read), image: isImage(read), read }))];
    this.paintFiles();
    this.fill(this.input.value.trim() ? `${this.input.value}\n${item.text}` : item.text);
  }

  /** Put a suggestion in the box for the user to send or change. */
  fill(text: string): void {
    this.input.value = text;
    this.fit();
    this.paint();
    this.input.focus();
  }

  /** Take the files a message can carry; say why the others were refused. */
  private async addFiles(files: readonly File[]): Promise<void> {
    const { taken, refusals } = admit(this.files, files.map(file => (
      { file, name: file.name, size: file.size, mimeType: fileType(file) })));
    this.refusal = refusals.join('；');
    const added: BoxFile[] = taken.map(({ file, mimeType }) => (
      { filename: file.name || 'image', size: file.size, image: mimeType.startsWith('image/') }));
    this.files = [...this.files, ...added];
    this.paintFiles();
    const unread = new Set<BoxFile>();
    await Promise.all(added.map(async (box, index) => {
      try {
        box.read = await readFile(taken[index].file, taken[index].mimeType);
      } catch {
        unread.add(box);
      }
      this.paintFiles();
    }));
    // A file removed meanwhile is not reported.
    const failed = added.filter(box => unread.has(box) && this.files.includes(box));
    this.files = this.files.filter(box => !unread.has(box));
    if (failed.length) {
      this.refusal = [...refusals, ...failed.map(box => (
        box.image ? copy.imageRead(box.filename) : copy.fileRead(box.filename)))].join('；');
    }
    this.paintFiles();
  }

  private removeFile(box: BoxFile): void {
    this.files = this.files.filter(item => item !== box);
    this.refusal = '';
    this.paintFiles();
    this.input.focus();
  }

  /** An image shows as its thumbnail, a document as its name and size. */
  private paintFiles(): void {
    this.thumbnails.hidden = !this.files.length;
    this.thumbnails.replaceChildren(...this.files.map(box => {
      const remove = box.image ? copy.removeImage(box.filename) : copy.removeFile(box.filename);
      const removeButton = h('button', {
        type: 'button', class: 'icon-button', 'aria-label': remove, title: remove, onclick: () => this.removeFile(box),
      }, icon('close'));
      if (!box.image) {
        return h('li', { class: 'composer-file', title: box.filename },
          icon(box.read ? 'files' : 'spinner'), h('span', { class: 'composer-file-name' }, box.filename),
          h('span', { class: 'composer-file-size' }, copy.fileSize(box.size)), removeButton);
      }
      return h('li', { class: box.read ? 'composer-image' : 'composer-image is-reading', title: box.filename },
        box.read ? h('img', { src: imageDataUrl(box.read), alt: box.filename }) : icon('spinner'), removeButton);
    }));
    this.paint();
  }

  private hasMessage(): boolean {
    return Boolean(this.input.value.trim() || this.files.length);
  }

  private stops(): boolean {
    return this.state.running && !this.hasMessage();
  }

  private sendable(): boolean {
    return this.state.enabled && this.hasMessage() && this.files.every(box => box.read);
  }

  private paint(): void {
    const stops = this.stops();
    const label = stops ? copy.stop : copy.send;
    this.button.replaceChildren(icon(stops ? 'stop' : 'send'));
    this.button.setAttribute('aria-label', label);
    this.button.title = label;
    this.button.classList.toggle('is-stop', stops);
    this.button.disabled = stops ? false : !this.sendable();
    this.note.textContent = this.refusal;
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
    const { files } = this;
    const read = files.flatMap(box => (box.read ? [box.read] : []));
    this.input.value = '';
    this.files = [];
    this.refusal = '';
    this.fit();
    this.paintFiles();
    // A message that did not go out comes back to the box.
    if (await this.actions.send(text, read)) return;
    if (!this.input.value) {
      this.input.value = text;
      this.fit();
    }
    if (!this.files.length) this.files = files;
    this.paintFiles();
  }
}

/** The type a file is: an image by its MIME type, as a pasted one may have no name; any file by its extension. */
function fileType(file: File): string | undefined {
  if (Object.values(FILE_TYPES).includes(file.type) && file.type.startsWith('image/')) return file.type;
  const dot = file.name.lastIndexOf('.');
  return dot < 0 ? undefined : FILE_TYPES[file.name.slice(dot).toLowerCase()];
}

/** The files pasted: ``items`` first, as the web chat reads them, since Chromium puts a screenshot in ``files`` too. */
function clipboardFiles(data: DataTransfer): File[] {
  const items = [...data.items ?? []].filter(item => item.kind === 'file')
    .flatMap(item => item.getAsFile() ?? []);
  return items.length ? items : [...data.files ?? []];
}

function readFile(file: File, mimeType: string): Promise<OutgoingAttachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const timer = setTimeout(() => {
      reader.abort();
      reject(new Error(`reading ${file.name} took over ${READ_TIMEOUT_MS} ms`));
    }, READ_TIMEOUT_MS);
    reader.onload = () => {
      clearTimeout(timer);
      const url = String(reader.result);
      resolve({ filename: file.name || 'image', mimeType, base64Data: url.slice(url.indexOf(',') + 1) });
    };
    reader.onerror = () => {
      clearTimeout(timer);
      reject(reader.error);
    };
    reader.readAsDataURL(file);
  });
}
