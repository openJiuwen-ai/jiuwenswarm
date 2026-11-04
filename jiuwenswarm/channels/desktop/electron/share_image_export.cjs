const { randomUUID } = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');

async function withTimeout(task, milliseconds, error) {
  let timer;
  try {
    return await Promise.race([
      task,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(error)), milliseconds); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

function createShareImageExporter({ BrowserWindow, getFrontendUrl, getCookies, getPreferredDownloadPath, showItemInFolder, onProgress = () => {} }) {
  const active = new Map();
  async function reserveDownloadPath(filename) {
    const preferred = getPreferredDownloadPath(filename);
    const extension = path.extname(preferred);
    const stem = preferred.slice(0, -extension.length);
    for (let counter = 0; ; counter++) {
      const candidate = counter === 0 ? preferred : `${stem} (${counter})${extension}`;
      try {
        const handle = await fs.open(candidate, 'wx');
        await handle.close();
        return candidate;
      } catch (error) {
        if (error.code !== 'EEXIST') throw error;
      }
    }
  }
  async function render(sessionId, locale) {
    // Each session export owns its partition so concurrent jobs cannot share
    // renderers, credentials or cleanup with the chat or another export.
    const partition = `share-image-export-${randomUUID()}`;
    let previousProgress = '';
    const report = (phase, progress) => {
      const value = Number.isInteger(progress) && progress >= 0 && progress <= 100 ? progress : null;
      const key = `${phase}:${value}`;
      if (key === previousProgress) return;
      previousProgress = key;
      onProgress({ sessionId, phase, progress: value });
    };
    report('loading_snapshot');
    const origin = new URL(getFrontendUrl()).origin;
    const url = new URL('/share-export-runner', origin);
    url.searchParams.set('session_id', sessionId);
    url.searchParams.set('locale', locale);
    const window = new BrowserWindow({
      show: false, width: 900, height: 900,
      webPreferences: {
        partition, contextIsolation: true, nodeIntegration: false, sandbox: true,
        backgroundThrottling: false,
      },
    });
    const contents = window.webContents;
    const session = contents.session;
    let failure = null;
    let temporaryPath = null;
    let reservedPath = null;
    let downloadItem = null;
    let downloadFinished = false;
    let downloadHandler;
    contents.setWindowOpenHandler(() => ({ action: 'deny' }));
    const pendingRendererTasks = new Set();
    const stopRenderer = error => {
      if (!failure) failure = new Error(error);
      for (const reject of pendingRendererTasks) reject(failure);
    };
    contents.once('render-process-gone', () => stopRenderer('share_export_renderer_exited'));
    contents.once('destroyed', () => stopRenderer('share_export_renderer_closed'));
    const waitForRenderer = async task => {
      if (failure) throw failure;
      if (contents.isDestroyed()) throw new Error('share_export_renderer_closed');
      let rejectStopped;
      const stopped = new Promise((_, reject) => { rejectStopped = reject; });
      pendingRendererTasks.add(rejectStopped);
      try {
        return await Promise.race([task(), stopped]);
      } finally {
        pendingRendererTasks.delete(rejectStopped);
      }
    };
    contents.on('will-navigate', (event, target) => {
      if (target !== url.href) event.preventDefault();
    });
    try {
      for (const cookie of await getCookies(origin)) {
        await session.cookies.set({
          url: origin, name: cookie.name, value: cookie.value,
          httpOnly: cookie.httpOnly, secure: cookie.secure, sameSite: cookie.sameSite,
        });
      }
      await withTimeout(waitForRenderer(() => window.loadURL(url.href)), 60_000, 'share_export_runner_load_timeout');
      const initDeadline = Date.now() + 15_000;
      let initialized = false;
      let state;
      for (;;) {
        const readState = waitForRenderer(() => contents.executeJavaScript('window.__SHARE_IMAGE_EXPORT_STATE || null'));
        state = initialized ? await readState : await withTimeout(
          readState, Math.max(1, initDeadline - Date.now()), 'share_export_runner_not_initialized',
        );
        if (state) initialized = true;
        if (state?.status === 'error') throw new Error(state.error || 'share_export_render_failed');
        if (state?.status === 'ready') break;
        if (state?.status === 'loading_snapshot' || state?.status === 'rendering') {
          report(state.status, state.progress);
        }
        if (!initialized && Date.now() >= initDeadline) throw new Error('share_export_runner_not_initialized');
        await waitForRenderer(() => new Promise(resolve => setTimeout(resolve, 250)));
      }
      if (!/^[A-Za-z0-9._-]+\.(png|zip)$/i.test(state.filename || '')) {
        throw new Error('share_export_result_filename_invalid');
      }
      report('saving', 100);
      const targetPath = await reserveDownloadPath(state.filename);
      reservedPath = targetPath;
      temporaryPath = path.join(path.dirname(targetPath), `.${path.basename(targetPath)}.${randomUUID()}.part`);
      let resolveStarted;
      let rejectStarted;
      const started = new Promise((resolve, reject) => { resolveStarted = resolve; rejectStarted = reject; });
      const completion = new Promise((resolve, reject) => {
        downloadHandler = (event, item, sender) => {
          if (sender !== contents || item.getFilename() !== state.filename) {
            event.preventDefault();
            const error = new Error('share_export_download_invalid');
            rejectStarted(error);
            reject(error);
            return;
          }
          downloadItem = item;
          item.setSavePath(temporaryPath);
          item.once('done', (_event, result) => {
            downloadFinished = true;
            if (result === 'completed') resolve();
            else reject(new Error(`share_export_download_${result}`));
          });
          resolveStarted();
        };
        session.once('will-download', downloadHandler);
      });
      await Promise.all([
        waitForRenderer(() => completion),
        withTimeout(waitForRenderer(() => started), 60_000, 'share_export_download_timeout'),
        waitForRenderer(() => contents.executeJavaScript('window.__DOWNLOAD_SHARE_IMAGE__()')),
      ]);
      await fs.rename(temporaryPath, targetPath);
      temporaryPath = null;
      reservedPath = null;
      showItemInFolder(targetPath);
      return true;
    } finally {
      if (downloadHandler) session.removeListener('will-download', downloadHandler);
      if (downloadItem && !downloadFinished) downloadItem.cancel();
      if (!window.isDestroyed()) window.destroy();
      try {
        await session.clearStorageData();
      } finally {
        if (temporaryPath) await fs.rm(temporaryPath, { force: true });
        if (reservedPath) await fs.rm(reservedPath, { force: true });
      }
    }
  }

  return (sessionId, locale) => {
    if (typeof sessionId !== 'string' || !sessionId.trim() || typeof locale !== 'string') {
      return Promise.reject(new Error('invalid_share_export_request'));
    }
    if (!active.has(sessionId)) {
      const task = Promise.resolve().then(() => render(sessionId, locale)).finally(() => active.delete(sessionId));
      active.set(sessionId, task);
    }
    return active.get(sessionId);
  };
}

module.exports = { createShareImageExporter };
