const { randomUUID } = require('node:crypto');
const { createWindowTransitions, transitionOptions } = require('./application_transition.cjs');

const CHANNEL = 'desktop:presentation:';
const MAX_BYTES = 256 * 1024;

function boundedInteger(value, min, max) {
  return Number.isSafeInteger(value) && value >= min && value <= max;
}

function boundedJSON(value) {
  try {
    const json = JSON.stringify(value);
    return typeof json === 'string' && Buffer.byteLength(json) <= MAX_BYTES;
  } catch {
    return false;
  }
}

function clampBounds(bounds, area) {
  const width = Math.min(bounds.width, area.width);
  const height = Math.min(bounds.height, area.height);
  return {
    x: Math.max(area.x, Math.min(bounds.x, area.x + area.width - width)),
    y: Math.max(area.y, Math.min(bounds.y, area.y + area.height - height)),
    width,
    height,
  };
}

/** Own one transparent presentation window; the original renderer owns all conversation work. */
function createApplicationPresentation({ BrowserWindow, ipcMain, screen, getMainWindow, preload, readyTimeoutMs = 15000 }) {
  let current = null;
  const handlers = [];

  const alive = window => window && !window.isDestroyed();
  const main = () => getMainWindow();
  const topFrame = (event, window) => alive(window) && event.sender === window.webContents
    && event.senderFrame === window.webContents.mainFrame;
  const send = (window, name, value) => {
    if (alive(window)) window.webContents.send(CHANNEL + name, value);
  };
  function activate(entry) {
    clearTimeout(entry.timer);
    entry.window.show();
    entry.window.focus();
    main().hide();
    entry.resolve?.({ generation: entry.generation });
    entry.resolve = null;
    entry.reject = null;
  }
  const transitions = createWindowTransitions({ BrowserWindow, screen, current: () => current, owner: main, activate, close, timeoutMs: readyTimeoutMs });

  function close(reason = 'closed', restore = true) {
    const previous = current;
    if (!previous) return;
    current = null;
    clearTimeout(previous.timer);
    transitions.cancel(previous, reason);
    previous.reject?.(new Error(reason));
    if (alive(previous.window)) previous.window.destroy();
    const owner = main();
    if (restore && alive(owner)) {
      if (owner.isMinimized()) owner.restore();
      owner.show();
      owner.focus();
      if (previous.workspaceFullscreen && !owner.isFullScreen()) {
        if (previous.windowedBounds) owner.setBounds(previous.windowedBounds);
        if (previous.windowedMaximized) owner.maximize();
        owner.setFullScreen(true);
      }
    }
    send(owner, 'closed', { generation: previous.generation, key: previous.config.key, reason });
  }

  function register(name, role, handler) {
    const channel = CHANNEL + name;
    ipcMain.handle(channel, (event, ...args) => {
      const window = role === 'main' ? main() : current?.window;
      if (!topFrame(event, window)) throw new Error('Untrusted presentation IPC sender');
      return handler(...args);
    });
    handlers.push(channel);
  }

  register('open', 'main', async config => {
    if (!config || typeof config.key !== 'string' || !config.key || config.key.length > 800
      || typeof config.src !== 'string' || !/^\/extension-assets\/[^/]+\/[^/]+\//.test(config.src)
      || typeof config.resourceUrl !== 'string' || !/^https?:\/\//.test(config.resourceUrl)
      || typeof config.title !== 'string' || !config.title.trim() || config.title.length > 200
      || !boundedInteger(config.width, 160, 960) || !boundedInteger(config.height, 120, 960)
      || !boundedJSON(config)) throw new Error('Invalid application presentation');
    close('replaced', false);
    const owner = main();
    const area = screen.getDisplayMatching(owner.getBounds()).workArea;
    const bounds = clampBounds({
      x: area.x + area.width - config.width - 40,
      y: area.y + area.height - config.height - 40,
      width: config.width,
      height: config.height,
    }, area);
    const window = new BrowserWindow({
      ...bounds, title: config.title, frame: false, transparent: true, backgroundColor: '#00000000',
      show: false, alwaysOnTop: true, skipTaskbar: true, hasShadow: false,
      enableLargerThanScreen: true,
      resizable: false, minimizable: false, maximizable: false, fullscreenable: false,
      webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false },
    });
    const generation = randomUUID();
    let resolveReady;
    let rejectReady;
    const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
    const entry = {
      window, generation, config, resolve: resolveReady, reject: rejectReady, interactive: false, initialWorkArea: area,
      timer: setTimeout(() => { if (current === entry) close('ready_timeout'); }, readyTimeoutMs),
    };
    current = entry;
    window.setIgnoreMouseEvents(true, { forward: true });
    window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    window.webContents.on('will-navigate', event => event.preventDefault());
    window.webContents.on('render-process-gone', () => { if (current === entry) close('renderer_gone'); });
    window.webContents.on('did-fail-load', (_event, code, _description, _url, isMainFrame) => {
      if (isMainFrame && code !== -3 && current === entry) close('load_failed');
    });
    window.on('closed', () => { if (current === entry) close('closed'); });
    const url = new URL(owner.webContents.getURL());
    // The presentation renderer starts at the application entry, independent of the active chat route.
    if (url.protocol !== 'file:') url.pathname = '/';
    url.search = '';
    url.hash = '';
    url.searchParams.set('application-presentation', '1');
    void window.loadURL(url.href).catch(() => { if (current === entry) close('load_failed'); });
    return ready;
  });

  register('bootstrap', 'floating', () => {
    const entry = current;
    if (!entry.bootstrap) entry.bootstrap = (async () => {
      await transitions.prepareContext(entry);
      if (current !== entry) throw new Error('STALE_REQUEST');
      entry.initialWorkArea = screen.getDisplayMatching(entry.window.getBounds()).workArea;
      entry.window.setBounds(clampBounds(entry.window.getBounds(), entry.initialWorkArea));
      return { generation: entry.generation, ...entry.config, initialWorkArea: entry.initialWorkArea };
    })();
    return entry.bootstrap;
  });
  register('ready', 'floating', options => {
    const entry = current;
    if (!entry.resolve) return true;
    if (options !== undefined && !transitionOptions(options)) throw new Error('Invalid window transition');
    if (options !== undefined && !entry.config.state?.reducedMotion) return transitions.prepare(entry, 'compact', options);
    return transitions.compact(entry);
  });
  register('transition-start', 'floating', id => transitions.start(current, id));
  register('transition-finish', 'floating', id => transitions.finish(current, id));
  register('transition-workspace', 'main', async (key, options) => {
    const entry = current;
    if (!entry || entry.config.key !== key) throw new Error('STALE_REQUEST');
    if (!transitionOptions(options)) throw new Error('Invalid window transition');
    if (entry.config.state?.reducedMotion) return transitions.workspace(entry);
    const descriptor = await transitions.prepare(entry, 'workspace', options);
    const done = entry.transition.done;
    send(entry.window, 'transition', descriptor);
    return done;
  });
  register('failed', 'floating', () => {
    const entry = current;
    setImmediate(() => { if (current === entry) close('render_failed'); });
    return true;
  });
  register('update', 'main', (key, payload) => {
    if (!current || current.config.key !== key) return false;
    if (!boundedJSON(payload)) throw new Error('Presentation update is too large');
    current.config = { ...current.config, ...payload };
    send(current.window, 'update', payload);
    return true;
  });
  register('request', 'floating', request => {
    if (!boundedJSON(request)) throw new Error('Presentation request is too large');
    send(main(), 'request', { generation: current.generation, key: current.config.key, request });
    return true;
  });
  register('respond', 'main', (generation, result) => {
    if (!current || current.generation !== generation) return false;
    if (!boundedJSON(result)) throw new Error('Presentation result is too large');
    send(current.window, 'result', result);
    return true;
  });
  register('resize', 'main', (key, width, height) => {
    if (!current || current.config.key !== key) return false;
    if (!boundedInteger(width, 160, 960) || !boundedInteger(height, 120, 960)) throw new Error('Invalid presentation size');
    const entry = current;
    return transitions.settled(entry, () => {
      const bounds = current.window.getBounds();
      const area = screen.getDisplayMatching(bounds).workArea;
      current.window.setBounds(clampBounds({
        x: bounds.x + bounds.width - width, y: bounds.y + bounds.height - height, width, height,
      }, area));
      return true;
    });
  });
  register('move', 'main', (key, deltaX, deltaY) => {
    if (!current || current.config.key !== key) return false;
    if (!boundedInteger(deltaX, -4096, 4096) || !boundedInteger(deltaY, -4096, 4096)) throw new Error('Invalid presentation movement');
    const entry = current;
    return transitions.settled(entry, () => {
      const bounds = current.window.getBounds();
      const moved = { ...bounds, x: bounds.x + deltaX, y: bounds.y + deltaY };
      current.window.setBounds(clampBounds(moved, screen.getDisplayMatching(moved).workArea));
      return true;
    });
  });
  register('close', 'main', key => {
    if (!current || current.config.key !== key) return false;
    return transitions.workspace(current);
  });
  register('hit-test', 'main', (key, interactive) => {
    if (!current || current.config.key !== key) return false;
    if (typeof interactive !== 'boolean') throw new Error('Invalid presentation hit test');
    const entry = current;
    entry.interactive = interactive;
    return transitions.settled(entry, () => {
      current.window.setIgnoreMouseEvents(!interactive, { forward: true });
      return true;
    });
  });

  return {
    close,
    restore() {
      const entry = current;
      if (!entry) return false;
      if (entry.transition) return true;
      Promise.resolve(transitions.workspace(entry)).catch(error => {
        console.error('Workspace restoration failed', error);
        if (current === entry) close('transition_failed');
      });
      return true;
    },
    dispose() {
      close('shutdown', false);
      handlers.forEach(channel => ipcMain.removeHandler(channel));
    },
  };
}

module.exports = { createApplicationPresentation, clampBounds };
