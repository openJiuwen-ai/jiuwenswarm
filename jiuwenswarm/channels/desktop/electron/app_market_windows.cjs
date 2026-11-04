// App windows do not depend on conversation browser panels or headless mode.
function createAppMarketWindows(BrowserWindow, getOwner, getIcon) {
  const windows = new Map();
  let ownerCloseRegistered = false;
  const isWebUrl = value => {
    try { return ['http:', 'https:'].includes(new URL(value).protocol); } catch { return false; }
  };
  const show = entry => {
    const win = entry.window;
    if (entry.hidden || win.isDestroyed()) return;
    if (win.isMinimized()) win.restore();
    win.show(); win.focus();
  };
  const loadEntry = async (entry, target, marketUrl = target, options) => {
    const navigation = ++entry.navigation;
    entry.loadingUrl = marketUrl;
    try {
      await entry.window.loadURL(target, options);
      if (navigation !== entry.navigation || entry.window.isDestroyed()) return false;
      entry.entryUrl = marketUrl;
      entry.loadingUrl = null;
      return true;
    } catch (error) {
      if (navigation !== entry.navigation || entry.window.isDestroyed()) return false;
      entry.loadingUrl = null;
      if (!entry.entryUrl) entry.window.destroy();
      throw error;
    }
  };
  const open = async (appId, rawUrl, title) => {
    const url = new URL(rawUrl);
    if (typeof appId !== 'string' || !appId || !isWebUrl(url.href) || url.username || url.password
        || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) throw new Error('Invalid application entry');
    const existing = windows.get(appId);
    if (existing && !existing.window.isDestroyed()) {
      existing.hidden = false;
      if ((existing.loadingUrl || existing.entryUrl) !== url.href && !await loadEntry(existing, url.href)) return true;
      show(existing);
      return true;
    }
    const owner = getOwner();
    if (!owner || owner.isDestroyed()) throw new Error('Application host unavailable');
    if (!ownerCloseRegistered) {
      owner.once('closed', () => {
        for (const entry of [...windows.values()]) if (!entry.window.isDestroyed()) entry.window.destroy();
      });
      ownerCloseRegistered = true;
    }
    const win = new BrowserWindow({
      parent: process.platform === 'win32' ? undefined : owner,
      title: title ? `${String(title)} · WorkSwarm` : 'WorkSwarm',
      icon: getIcon(), skipTaskbar: true,
      width: 1200, height: 800, show: false,
      webPreferences: {
        partition: `persist:app-market-${encodeURIComponent(appId)}`,
        contextIsolation: true, nodeIntegration: false, sandbox: true,
      },
    });
    const entry = { window: win, entryUrl: null, loadingUrl: null, navigation: 0, hidden: false };
    windows.set(appId, entry);
    win.setMenuBarVisibility(false);
    win.on('page-title-updated', event => event.preventDefault());
    // With no taskbar button, the market's Open / restore action is the restore entry.
    win.on('minimize', () => { entry.hidden = true; win.hide(); });
    win.on('closed', () => { if (windows.get(appId) === entry) windows.delete(appId); });
    const contents = win.webContents;
    contents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    contents.session.setPermissionCheckHandler(() => false);
    let nativeNavigation = 0;
    contents.on('will-navigate', (event, target) => {
      if (!isWebUrl(target)) { event.preventDefault(); return; }
      nativeNavigation = ++entry.navigation;
      entry.entryUrl = entry.loadingUrl || entry.entryUrl || url.href;
      entry.loadingUrl = null;
    });
    contents.on('did-stop-loading', () => {
      if (nativeNavigation === entry.navigation && !win.isDestroyed()) {
        nativeNavigation = 0;
        show(entry);
      }
    });
    contents.on('will-redirect', (event, target) => { if (!isWebUrl(target)) event.preventDefault(); });
    contents.setWindowOpenHandler(({ url: target, referrer, postBody }) => {
      if (isWebUrl(target)) {
        const options = { httpReferrer: referrer };
        if (postBody) {
          options.postData = postBody.data;
          options.extraHeaders = `Content-Type: ${postBody.contentType}${postBody.boundary ? `; boundary=${postBody.boundary}` : ''}`;
        }
        void loadEntry(entry, target, entry.loadingUrl || entry.entryUrl || url.href, options)
          .then(current => { if (current) show(entry); }).catch(() => undefined);
      }
      return { action: 'deny' };
    });
    if (await loadEntry(entry, url.href)) show(entry);
    return true;
  };
  const close = appId => {
    if (typeof appId !== 'string' || !appId) throw new Error('Invalid application ID');
    const win = windows.get(appId)?.window;
    if (win && !win.isDestroyed()) win.destroy();
    return true;
  };
  return { open, close };
}

module.exports = { createAppMarketWindows };
