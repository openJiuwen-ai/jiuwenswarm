// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
const { randomUUID } = require('node:crypto');

const MAX_CHILDREN = 24;
const integer = (value, min, max) => Number.isSafeInteger(value) && value >= min && value <= max;

function validChildren(windows) {
  if (!Array.isArray(windows) || windows.length > MAX_CHILDREN) return false;
  try { if (Buffer.byteLength(JSON.stringify(windows)) > 250 * 1024) return false; } catch { return false; }
  const ids = new Set();
  return windows.every(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)
      || Object.keys(item).some(key => !['id', 'title', 'width', 'height', 'config', 'x', 'y'].includes(key))
      || typeof item.id !== 'string' || !/^[A-Za-z0-9._-]{1,80}$/.test(item.id) || ids.has(item.id)
      || typeof item.title !== 'string' || !item.title.trim() || Buffer.byteLength(item.title) > 200
      || !integer(item.width, 160, 960) || !integer(item.height, 120, 960)
      || !item.config || typeof item.config !== 'object' || Array.isArray(item.config)) return false;
    try { if (Buffer.byteLength(JSON.stringify(item.config)) > 16 * 1024) return false; } catch { return false; }
    if (('x' in item || 'y' in item) && (!integer(item.x, -100000, 100000) || !integer(item.y, -100000, 100000))) return false;
    ids.add(item.id);
    return true;
  });
}

/** Manage subordinate native surfaces without interpreting plugin business data. */
function createChildPresentations({ BrowserWindow, screen, parent, owner, preload, send, clampBounds, timeoutMs }) {
  const entries = new Map();
  const alive = window => window && !window.isDestroyed();
  const get = key => entries.get(key);
  const contains = entry => entries.get(entry.config.key) === entry && parent()?.generation === entry.parentGeneration;
  function close(entry, reason = 'closed') {
    if (entries.get(entry.config.key) !== entry) return;
    entries.delete(entry.config.key);
    clearTimeout(entry.timer);
    if (alive(entry.window)) entry.window.destroy();
    send(owner(), 'closed', { key: entry.config.key, generation: entry.generation, reason,
      parentKey: entry.parentKey, parentGeneration: entry.parentGeneration });
  }
  function sync(key, generation, windows) {
    const root = parent();
    if (!root || root.config.key !== key || root.generation !== generation || root.resolve) throw new Error('STALE_REQUEST');
    if (!validChildren(windows)) throw new Error('Invalid presentation children');
    const configurations = new Map(windows.map(item => {
      const config = { ...root.config.config, ...item.config };
      if (Buffer.byteLength(JSON.stringify(config)) > 16 * 1024) throw new Error('Child configuration is too large');
      return [item.id, config];
    }));
    const keys = new Set(windows.map(item => `${key}:child:${item.id}`));
    for (const entry of [...entries.values()]) if (!keys.has(entry.config.key)) close(entry, 'removed');
    const result = [];
    for (const item of windows) {
      const childKey = `${key}:child:${item.id}`;
      const childConfig = configurations.get(item.id);
      let entry = entries.get(childKey);
      if (entry) {
        if (JSON.stringify(entry.config.config) !== JSON.stringify(childConfig) || entry.config.title !== item.title) {
          entry.config = { ...entry.config, config: childConfig, title: item.title };
          entry.itemConfig = item.config;
          entry.window.setTitle?.(item.title);
          send(entry.window, 'update', { config: childConfig });
        }
      } else {
        const initial = { x: item.x ?? root.initialWorkArea.x + 40, y: item.y ?? root.initialWorkArea.y + 40,
          width: item.width, height: item.height };
        const area = screen.getDisplayMatching(initial).workArea;
        const window = new BrowserWindow({
          ...clampBounds(initial, area), title: item.title, frame: false, transparent: true, backgroundColor: '#00000000',
          show: false, alwaysOnTop: true, skipTaskbar: true, hasShadow: false,
          resizable: false, minimizable: false, maximizable: false, fullscreenable: false,
          webPreferences: { preload, contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false },
        });
        entry = { window, config: { ...root.config, key: childKey, title: item.title, width: item.width, height: item.height,
          config: childConfig }, itemConfig: item.config, generation: randomUUID(), parentKey: key, parentGeneration: generation,
          initialWorkArea: area, interactive: false, ready: false };
        entries.set(childKey, entry);
        const created = entry;
        created.timer = setTimeout(() => close(created, 'ready_timeout'), timeoutMs);
        window.setIgnoreMouseEvents(true, { forward: true });
        window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
        window.webContents.on('will-navigate', event => event.preventDefault());
        window.webContents.on('render-process-gone', () => close(created, 'renderer_gone'));
        window.webContents.on('did-fail-load', (_event, code, _description, _url, isMainFrame) => {
          if (isMainFrame && code !== -3) close(created, 'load_failed');
        });
        window.on('closed', () => close(created));
        const url = new URL(owner().webContents.getURL());
        if (url.protocol !== 'file:') url.pathname = '/';
        url.search = ''; url.hash = ''; url.searchParams.set('application-presentation', '1');
        void window.loadURL(url.href).catch(() => close(created, 'load_failed'));
      }
      result.push({ id: item.id, key: childKey, generation: entry.generation });
    }
    return { windows: result };
  }
  function floating(name, entry, ...args) {
    if (!contains(entry)) throw new Error('STALE_REQUEST');
    switch (name) {
      case 'bootstrap': return { ...entry.config, generation: entry.generation, initialWorkArea: entry.initialWorkArea, isChild: true };
      case 'ready':
        if (args[0] !== undefined) throw new Error('Child transitions are not supported');
        clearTimeout(entry.timer); entry.ready = true; entry.window.showInactive();
        parent()?.window.moveTop?.();
        return true;
      case 'failed': close(entry, 'render_failed'); return true;
      case 'request': {
        let size;
        try { size = Buffer.byteLength(JSON.stringify(args[0])); } catch { throw new Error('Invalid presentation request'); }
        if (size > 256 * 1024) throw new Error('Presentation request is too large');
        send(owner(), 'request', { key: entry.config.key, generation: entry.generation,
          parentKey: entry.parentKey, parentGeneration: entry.parentGeneration, request: args[0] });
        return true;
      }
      default: throw new Error('Child transitions are not supported');
    }
  }
  function move(entry, dx, dy) {
    if (!integer(dx, -4096, 4096) || !integer(dy, -4096, 4096)) throw new Error('Invalid presentation movement');
    const bounds = entry.window.getBounds();
    const moved = { ...bounds, x: bounds.x + dx, y: bounds.y + dy };
    const point = screen.getCursorScreenPoint?.();
    const pointerInside = point && point.x >= moved.x && point.x < moved.x + moved.width
      && point.y >= moved.y && point.y < moved.y + moved.height;
    const display = pointerInside && screen.getDisplayNearestPoint ? screen.getDisplayNearestPoint(point) : screen.getDisplayMatching(moved);
    entry.window.setBounds(clampBounds(moved, display.workArea));
    return true;
  }
  function resize(entry, width, height) {
    if (!integer(width, 160, 960) || !integer(height, 120, 960)) throw new Error('Invalid presentation size');
    const bounds = entry.window.getBounds();
    const area = screen.getDisplayMatching(bounds).workArea;
    entry.window.setBounds(clampBounds({ x: bounds.x + bounds.width - width, y: bounds.y + bounds.height - height, width, height }, area));
    return true;
  }
  return {
    get, sync, floating, move, resize,
    fromSender(event) { return [...entries.values()].find(entry => alive(entry.window) && event.sender === entry.window.webContents
      && event.senderFrame === entry.window.webContents.mainFrame); },
    respond(generation, result) {
      const entry = [...entries.values()].find(item => item.generation === generation);
      if (!entry) return false;
      send(entry.window, 'result', result); return true;
    },
    updateState(state) {
      for (const entry of entries.values()) { entry.config = { ...entry.config, state }; send(entry.window, 'update', { state }); }
    },
    updateConfig(baseConfig) {
      // Keep the per-child declaration overrides while refreshing the shared base,
      // so an update({config}) reaches open children just like the parent window.
      for (const entry of entries.values()) {
        let merged;
        try { merged = { ...baseConfig, ...entry.itemConfig }; } catch { continue; }
        if (JSON.stringify(entry.config.config) === JSON.stringify(merged)) continue;
        try { if (Buffer.byteLength(JSON.stringify(merged)) > 16 * 1024) continue; } catch { continue; }
        entry.config = { ...entry.config, config: merged };
        send(entry.window, 'update', { config: merged });
      }
    },
    clear() { for (const entry of [...entries.values()]) close(entry, 'parent_closed'); },
  };
}

module.exports = { createChildPresentations, validChildren, MAX_CHILDREN };
