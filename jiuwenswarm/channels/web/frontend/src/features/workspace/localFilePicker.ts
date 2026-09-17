export type LocalFilePick = {
  path: string;
  filename: string;
  size: number;
  mime_type: string;
  kind: 'image' | 'document';
  base64?: string;
  error?: string;
};

export type LocalFilePickResult =
  | { ok: true; files: LocalFilePick[] }
  | { ok: false; reason: 'unsupported' | 'cancelled' | 'failed'; message?: string };

export const DESKTOP_LOCAL_FILES_EVENT = 'jiuwen-desktop-local-files';
export const DESKTOP_READY_EVENT = 'jiuwen-desktop-ready';
export const DESKTOP_FILE_DRAG_EVENT = 'jiuwen-desktop-file-drag';

export type DesktopLocalFilesEventDetail = {
  source?: 'drop' | 'paste' | string;
  files?: unknown;
  clientX?: number;
  clientY?: number;
  /** Set by desktop_app.py when the event comes from the native drop bridge. */
  trusted?: boolean;
  /** Unique id so the ingest bridge + CustomEvent paths do not double-ingest. */
  dropId?: string;
};

export type DesktopLocalFilesConsumer = (
  detail: DesktopLocalFilesEventDetail,
  files: LocalFilePick[],
) => void;

type DesktopWindowMarker = Window & {
  __JIUWEN_DESKTOP__?: boolean;
  __JIUWEN_DESKTOP_DND__?: boolean;
  /** Frontend override that forces dropEffect=copy so OS file drags are accepted. */
  __JIUWEN_DESKTOP_DND_COPY_OVERRIDE__?: boolean;
  __JIUWEN_DROP_QUEUE__?: DesktopLocalFilesEventDetail[];
};

/** Survives ChatPanel mount/unmount — Python run_js must always find a function. */
let desktopLocalFilesConsumer: DesktopLocalFilesConsumer | null = null;
let durableBridgeInstalled = false;

function getDropQueue(): DesktopLocalFilesEventDetail[] {
  const marker = window as DesktopWindowMarker;
  if (!Array.isArray(marker.__JIUWEN_DROP_QUEUE__)) {
    marker.__JIUWEN_DROP_QUEUE__ = [];
  }
  return marker.__JIUWEN_DROP_QUEUE__;
}

/**
 * Install a durable window.__JIUWEN_INGEST_LOCAL_FILES__ that never gets deleted
 * by React effect cleanup. ChatPanel registers a consumer to drain/receive drops.
 */
export function installDesktopLocalFilesBridge(): void {
  if (typeof window === 'undefined' || durableBridgeInstalled) return;
  durableBridgeInstalled = true;

  window.__JIUWEN_INGEST_LOCAL_FILES__ = (raw) => {
    const detail = (raw || {}) as DesktopLocalFilesEventDetail;
    const files = normalizePicks(detail.files);
    // No pywebview API call here: this function runs inside Python-side run_js,
    // and a concurrent JS->Python call deadlocks the WebView2 UI thread.
    if (desktopLocalFilesConsumer) {
      desktopLocalFilesConsumer(detail, files);
    } else {
      getDropQueue().push(detail);
    }
    window.dispatchEvent(new CustomEvent(DESKTOP_LOCAL_FILES_EVENT, { detail }));
  };
}

/** Register ChatPanel (or other UI) as the active consumer; drains queued drops. */
export function registerDesktopLocalFilesConsumer(
  consumer: DesktopLocalFilesConsumer,
): () => void {
  installDesktopLocalFilesBridge();
  desktopLocalFilesConsumer = consumer;
  const queue = getDropQueue();
  const queued = queue.splice(0, queue.length);
  for (const detail of queued) {
    consumer(detail, normalizePicks(detail.files));
  }
  return () => {
    if (desktopLocalFilesConsumer === consumer) {
      desktopLocalFilesConsumer = null;
    }
    // Keep window.__JIUWEN_INGEST_LOCAL_FILES__ — Python must always find it.
  };
}

/** Desktop webview shell marker and/or pywebview bridge (not browser / whl). */
export function isDesktopShell(): boolean {
  if (typeof window === 'undefined') return false;
  const marker = window as DesktopWindowMarker;
  if (marker.__JIUWEN_DESKTOP__ === true || marker.__JIUWEN_DESKTOP_DND__ === true) return true;
  if (typeof window.pywebview?.api?.select_local_files === 'function') return true;
  return 'pywebview' in window;
}

function dataTransferHasFiles(dt: DataTransfer | null): boolean {
  if (!dt?.types) return false;
  try {
    return Array.from(dt.types).includes('Files');
  } catch {
    return false;
  }
}

/**
 * Install window-level file-drag accept handlers inside the desktop webview.
 * Must run in the page itself (not only via Python evaluate_js) so a frontend
 * hot-update can fix the forbidden cursor without waiting for a full exe rebuild.
 * Window bubble listeners run after React and override dropEffect='none'.
 * OS file drags into Chromium/WebView2 must use dropEffect='copy':
 * ``none`` shows the forbidden cursor, and ``move`` is often rejected by the
 * browser for Explorer file drags (also ends up as the forbidden cursor).
 */
export function installDesktopFileDragAccept(): boolean {
  if (typeof window === 'undefined') return false;
  if (!isDesktopShell()) return false;
  installDesktopLocalFilesBridge();
  const marker = window as DesktopWindowMarker;
  marker.__JIUWEN_DESKTOP__ = true;

  // Bubble-phase override always wins over React paths that set dropEffect='none'
  // (forbidden cursor). Safe to install even when DND handlers already exist.
  if (!marker.__JIUWEN_DESKTOP_DND_COPY_OVERRIDE__) {
    marker.__JIUWEN_DESKTOP_DND_COPY_OVERRIDE__ = true;
    const forceCopy = (event: DragEvent) => {
      if (!dataTransferHasFiles(event.dataTransfer)) return;
      event.preventDefault();
      try {
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
      } catch {
        // ignore
      }
    };
    window.addEventListener('dragenter', forceCopy, false);
    window.addEventListener('dragover', forceCopy, false);
  }

  if (marker.__JIUWEN_DESKTOP_DND__ === true) return true;
  marker.__JIUWEN_DESKTOP_DND__ = true;

  const accept = (event: DragEvent) => {
    if (!dataTransferHasFiles(event.dataTransfer)) return;
    event.preventDefault();
    try {
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    } catch {
      // ignore
    }
    window.dispatchEvent(
      new CustomEvent(DESKTOP_FILE_DRAG_EVENT, { detail: { active: true } }),
    );
  };
  const endDrag = () => {
    window.dispatchEvent(
      new CustomEvent(DESKTOP_FILE_DRAG_EVENT, { detail: { active: false } }),
    );
  };

  window.addEventListener('dragenter', accept, true);
  window.addEventListener('dragover', accept, true);
  window.addEventListener('dragenter', accept, false);
  window.addEventListener('dragover', accept, false);
  window.addEventListener(
    'drop',
    (event: DragEvent) => {
      if (!dataTransferHasFiles(event.dataTransfer)) return;
      event.preventDefault();
      endDrag();
    },
    true,
  );
  return true;
}

const FILE_PICKER_TIMEOUT_MS = 10 * 60 * 1000;
const LAST_DIR_STORAGE_KEY = 'jiuwenswarm:last-file-picker-dir';

function getLocalFilePickerApi() {
  if (typeof window === 'undefined') return undefined;
  return window.pywebview?.api?.select_local_files;
}

/** True when the native pywebview file picker bridge is available. */
export function isDesktopLocalFilePicker(): boolean {
  return typeof getLocalFilePickerApi() === 'function';
}

export function isLocalFilePickerSupported(): boolean {
  // 桌面端走 pywebview；浏览器 / whl 走后端 path.select_files（对齐 path.select_directory）
  return isDesktopLocalFilePicker() || typeof window !== 'undefined';
}

function parentDirectoryOf(path: string): string | undefined {
  const trimmed = path.trim().replace(/[\\/]+$/, '');
  if (!trimmed) return undefined;
  const idx = Math.max(trimmed.lastIndexOf('\\'), trimmed.lastIndexOf('/'));
  if (idx <= 0) return undefined;
  return trimmed.slice(0, idx);
}

function readLastPickerDir(): string | undefined {
  if (typeof window === 'undefined') return undefined;
  try {
    const raw = window.localStorage.getItem(LAST_DIR_STORAGE_KEY)?.trim();
    return raw || undefined;
  } catch {
    return undefined;
  }
}

function rememberLastPickerDir(files: LocalFilePick[]): void {
  const dir = parentDirectoryOf(files[0]?.path || '');
  if (!dir || typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(LAST_DIR_STORAGE_KEY, dir);
  } catch {
    // private mode / quota — ignore
  }
}

function normalizePick(raw: unknown): LocalFilePick | null {
  if (!raw || typeof raw !== 'object') return null;
  const item = raw as Record<string, unknown>;
  const path = typeof item.path === 'string' ? item.path.trim() : '';
  const filename = typeof item.filename === 'string' ? item.filename.trim() : '';
  if (!path || !filename) return null;
  const kind = item.kind === 'image' ? 'image' : 'document';
  const size = typeof item.size === 'number' && Number.isFinite(item.size) ? item.size : 0;
  const mimeType =
    typeof item.mime_type === 'string' && item.mime_type.trim()
      ? item.mime_type.trim()
      : 'application/octet-stream';
  const base64 = typeof item.base64 === 'string' && item.base64 ? item.base64 : undefined;
  const error = typeof item.error === 'string' && item.error ? item.error : undefined;
  return {
    path,
    filename,
    size,
    mime_type: mimeType,
    kind,
    ...(base64 ? { base64 } : {}),
    ...(error ? { error } : {}),
  };
}

export function normalizePicks(rawFiles: unknown): LocalFilePick[] {
  if (!Array.isArray(rawFiles)) return [];
  return rawFiles
    .map((item) => normalizePick(item))
    .filter((item): item is LocalFilePick => item !== null);
}

async function selectLocalFilesViaBackend(
  allowMultiple: boolean,
  initialDir?: string,
): Promise<LocalFilePickResult> {
  try {
    const { webRequest } = await import('../../services/webClient');
    const payload = await webRequest<{
      files?: Array<Record<string, unknown>>;
      cancelled?: boolean;
    }>(
      'path.select_files',
      {
        allow_multiple: allowMultiple,
        ...(initialDir ? { initial_dir: initialDir } : {}),
      },
      { timeoutMs: FILE_PICKER_TIMEOUT_MS },
    );
    if (payload?.cancelled) {
      return { ok: false, reason: 'cancelled' };
    }
    const files = normalizePicks(payload?.files);
    if (!files.length) {
      return { ok: false, reason: 'cancelled' };
    }
    rememberLastPickerDir(files);
    return { ok: true, files };
  } catch (error) {
    const code =
      error && typeof error === 'object' && 'code' in error
        ? String((error as { code?: unknown }).code || '')
        : '';
    if (code === 'UNSUPPORTED') {
      return {
        ok: false,
        reason: 'unsupported',
        message: error instanceof Error ? error.message : String(error),
      };
    }
    if (code === 'METHOD_NOT_FOUND') {
      return {
        ok: false,
        reason: 'unsupported',
        message: '后端未注册 path.select_files，请重启 jiuwenswarm-start 后再试',
      };
    }
    return {
      ok: false,
      reason: 'failed',
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

export async function selectLocalFiles(
  allowMultiple = true,
): Promise<LocalFilePickResult> {
  const initialDir = readLastPickerDir();
  const pickFiles = getLocalFilePickerApi();
  if (typeof pickFiles === 'function') {
    try {
      const selected = initialDir
        ? await pickFiles(allowMultiple, initialDir)
        : await pickFiles(allowMultiple);
      const files = normalizePicks(selected);
      if (!files.length) {
        return { ok: false, reason: 'cancelled' };
      }
      rememberLastPickerDir(files);
      return { ok: true, files };
    } catch (error) {
      return {
        ok: false,
        reason: 'failed',
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }

  return selectLocalFilesViaBackend(allowMultiple, initialDir);
}

export async function describeLocalFiles(paths: string[]): Promise<LocalFilePick[]> {
  if (!isDesktopLocalFilePicker() || !paths.length) return [];
  const api = window.pywebview?.api?.describe_local_files;
  if (typeof api !== 'function') return [];
  try {
    return normalizePicks(await api(paths));
  } catch {
    return [];
  }
}

export async function getClipboardFilePicks(): Promise<LocalFilePick[]> {
  if (!isDesktopLocalFilePicker()) return [];
  const api = window.pywebview?.api?.get_clipboard_files;
  if (typeof api !== 'function') return [];
  try {
    return normalizePicks(await api());
  } catch {
    return [];
  }
}
