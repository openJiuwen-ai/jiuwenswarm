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

function createShareImageExporter({ BrowserWindow, getFrontendUrl, getCookies, uniqueDownloadPath, showItemInFolder }) {
  const active = new Map();
  // A separate StoragePartition cannot share a renderer with the chat window.
  // Exports are serialized so this one in-memory partition has bounded lifetime data.
  const partition = `share-image-export-${randomUUID()}`;
  let queue = Promise.resolve();

  async function render(sessionId, locale) {
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
    let downloadItem = null;
    let downloadFinished = false;
    let downloadHandler;
    contents.setWindowOpenHandler(() => ({ action: 'deny' }));
    contents.on('render-process-gone', () => { failure = new Error('share_export_renderer_exited'); });
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
      await withTimeout(window.loadURL(url.href), 60_000, 'share_export_runner_load_timeout');
      const deadline = Date.now() + 15 * 60 * 1000;
      let idleDeadline = deadline;
      const initDeadline = Date.now() + 15_000;
      let heartbeat = null;
      let state;
      for (;;) {
        if (failure) throw failure;
        if (contents.isDestroyed()) throw new Error('share_export_renderer_closed');
        state = await withTimeout(
          contents.executeJavaScript('window.__SHARE_IMAGE_EXPORT_STATE || null'),
          Math.max(1, deadline - Date.now()), 'share_export_render_timeout',
        );
        if (state?.status === 'error') throw new Error(state.error || 'share_export_render_failed');
        if (state?.status === 'ready') break;
        if (!state && Date.now() >= initDeadline) throw new Error('share_export_runner_not_initialized');
        if (typeof state?.heartbeat === 'number' && state.heartbeat !== heartbeat) {
          heartbeat = state.heartbeat;
          idleDeadline = Date.now() + 15 * 60 * 1000;
        }
        if (Date.now() >= deadline) throw new Error('share_export_render_timeout');
        if (Date.now() >= idleDeadline) throw new Error('share_export_render_stalled');
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      if (!/^[A-Za-z0-9._-]+\.(png|zip)$/i.test(state.filename || '')) {
        throw new Error('share_export_result_filename_invalid');
      }
      const targetPath = await uniqueDownloadPath(state.filename);
      temporaryPath = path.join(path.dirname(targetPath), `.${path.basename(targetPath)}.${randomUUID()}.part`);
      const completion = new Promise((resolve, reject) => {
        downloadHandler = (event, item, sender) => {
          if (sender !== contents || item.getFilename() !== state.filename) {
            event.preventDefault();
            reject(new Error('share_export_download_invalid'));
            return;
          }
          downloadItem = item;
          item.setSavePath(temporaryPath);
          item.once('done', (_event, result) => {
            downloadFinished = true;
            if (result === 'completed') resolve();
            else reject(new Error(`share_export_download_${result}`));
          });
        };
        session.once('will-download', downloadHandler);
      });
      await withTimeout(Promise.all([
        completion,
        contents.executeJavaScript('window.__DOWNLOAD_SHARE_IMAGE__()'),
      ]), 60_000, 'share_export_download_timeout');
      await fs.rename(temporaryPath, targetPath);
      temporaryPath = null;
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
      }
    }
  }

  return (sessionId, locale) => {
    if (typeof sessionId !== 'string' || !sessionId.trim() || typeof locale !== 'string') {
      return Promise.reject(new Error('invalid_share_export_request'));
    }
    if (!active.has(sessionId)) {
      const start = () => render(sessionId, locale);
      const task = queue.then(start, start).finally(() => active.delete(sessionId));
      queue = task;
      active.set(sessionId, task);
    }
    return active.get(sessionId);
  };
}

module.exports = { createShareImageExporter };
