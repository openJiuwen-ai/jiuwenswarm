const { createMarketResourceMonitor } = require('./app_market_resources.cjs');

// App pages keep one WebContentsView while moving between their window and editor pane.
function createAppMarketWindows(BaseWindow, WebContentsView, getOwner, getIcon, metrics, locale) {
  const windows = new Map();
  const resources = createMarketResourceMonitor(windows, metrics, locale);
  let ownerCloseRegistered = false;
  const isWebUrl = value => {
    try { return ['http:', 'https:'].includes(new URL(value).protocol); } catch { return false; }
  };
  const show = entry => {
    const win = entry.window;
    if (entry.embedded || entry.hidden || win.isDestroyed()) return;
    if (win.isMinimized()) win.restore();
    win.show(); win.focus();
  };
  const loadEntry = async (entry, target, marketUrl = target, options) => {
    const navigation = ++entry.navigation;
    entry.loadingUrl = marketUrl;
    try {
      await entry.view.webContents.loadURL(target, options);
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
  const resizeWindow = entry => {
    if (entry.embedded || entry.window.isDestroyed()) return;
    const { width, height } = entry.window.getContentBounds();
    entry.view.setBounds({ x: 0, y: 0, width, height });
  };
  const present = (entry, embedded) => {
    if (entry.embedded !== embedded) {
      const owner = getOwner();
      const previous = entry.embedded ? owner : entry.window;
      previous?.contentView.removeChildView(entry.view);
      (embedded ? owner : entry.window).contentView.addChildView(entry.view);
      entry.embedded = embedded;
    }
    entry.hidden = false;
    entry.view.setVisible(!embedded);
    if (embedded) entry.window.hide();
    else resizeWindow(entry);
  };
  const open = async (appId, rawUrl, title, embedded = false) => {
    const url = new URL(rawUrl);
    if (typeof appId !== 'string' || !appId || !isWebUrl(url.href) || url.username || url.password
        || !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) throw new Error('Invalid application entry');
    const existing = windows.get(appId);
    if (existing && !existing.window.isDestroyed()) {
      present(existing, embedded === true);
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
    const win = new BaseWindow({
      parent: process.platform === 'win32' ? undefined : owner,
      title: title ? `${String(title)} · WorkSwarm` : 'WorkSwarm',
      icon: getIcon(), skipTaskbar: true,
      width: 1200, height: 800, show: false,
    });
    const view = new WebContentsView({ webPreferences: {
      partition: `persist:app-market-${encodeURIComponent(appId)}`,
      contextIsolation: true, nodeIntegration: false, sandbox: true,
    } });
    const contents = view.webContents;
    win.contentView.addChildView(view);
    const entry = { window: win, view, title: title ? `${String(title)} · WorkSwarm` : 'WorkSwarm', embedded: false, entryUrl: null, loadingUrl: null, navigation: 0, hidden: false };
    windows.set(appId, entry);
    resources.start();
    win.setMenuBarVisibility(false);
    win.on('resize', () => resizeWindow(entry));
    // With no taskbar button, the market's Open / restore action is the restore entry.
    win.on('minimize', () => { entry.hidden = true; win.hide(); });
    win.on('closed', () => {
      if (windows.get(appId) === entry) windows.delete(appId);
      resources.stopIfEmpty();
      const owner = getOwner();
      if (entry.embedded && owner && !owner.isDestroyed()) owner.contentView.removeChildView(view);
      if (!contents.isDestroyed()) contents.close({ waitForBeforeUnload: false });
    });
    present(entry, embedded === true);
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
  const setViewport = (appId, bounds, focus = false) => {
    const entry = windows.get(appId);
    const owner = getOwner();
    if (!entry || entry.window.isDestroyed() || !owner || owner.isDestroyed()) return false;
    if (!bounds || ![bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite)
        || bounds.width <= 0 || bounds.height <= 0) {
      if (entry.embedded) entry.view.setVisible(false);
      return false;
    }
    if (!entry.embedded) present(entry, true);
    const zoom = owner.webContents.getZoomFactor();
    const size = owner.getContentBounds();
    const x = Math.max(0, Math.min(Math.round(bounds.x * zoom), size.width));
    const y = Math.max(0, Math.min(Math.round(bounds.y * zoom), size.height));
    const width = Math.max(0, Math.min(Math.round(bounds.width * zoom), size.width - x));
    const height = Math.max(0, Math.min(Math.round(bounds.height * zoom), size.height - y));
    for (const other of windows.values()) if (other.embedded) other.view.setVisible(false);
    entry.view.setBounds({ x, y, width, height });
    entry.view.setVisible(width > 0 && height > 0);
    if (focus && width > 0 && height > 0) entry.view.webContents.focus();
    return true;
  };
  const reload = appId => {
    const entry = windows.get(appId);
    if (!entry || entry.window.isDestroyed()) return false;
    entry.view.webContents.reload();
    return true;
  };
  return { open, close, setViewport, reload, getResources: resources.read };
}

module.exports = { createAppMarketWindows };
