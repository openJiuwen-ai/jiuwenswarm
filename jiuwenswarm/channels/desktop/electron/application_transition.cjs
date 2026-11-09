const { randomUUID } = require('node:crypto');

function transitionOptions(value) {
  return !!value && Object.keys(value).join(',') === 'durationMs'
    && Number.isSafeInteger(value.durationMs) && value.durationMs >= 200 && value.durationMs <= 2000;
}

function transitionBounds(workspace, surface) {
  const x = Math.min(workspace.x, surface.x), y = Math.min(workspace.y, surface.y);
  const width = Math.max(workspace.x + workspace.width, surface.x + surface.width) - x;
  const height = Math.max(workspace.y + workspace.height, surface.y + surface.height) - y;
  const local = bounds => ({ x: bounds.x - x, y: bounds.y - y, width: bounds.width, height: bounds.height });
  return { stage: { x, y, width, height }, workspace: local(workspace), surface: local(surface) };
}

/** Coordinate native surfaces without owning any resource artwork or animation. */
function createWindowTransitions({ BrowserWindow, screen, current, owner, activate, close, timeoutMs = 15000, platform = process.platform }) {
  function cancel(entry, reason) {
    const transition = entry?.transition;
    if (!transition) return;
    entry.transition = null;
    clearTimeout(transition.timer);
    if (transition.cover && !transition.cover.isDestroyed()) transition.cover.destroy();
    for (const operation of [...transition.pending, ...transition.applied]) operation.reject(new Error(reason));
    transition.reject(new Error(reason));
  }

  function begin(entry, direction) {
    if (entry !== current() || entry.transition) throw new Error('STALE_REQUEST');
    let resolve, reject;
    const done = new Promise((yes, no) => { resolve = yes; reject = no; });
    // Preparation may fail before a caller starts awaiting the completion receipt.
    void done.catch(() => {});
    const transition = { id: randomUUID(), direction, done, resolve, reject, started: false, pending: [], applied: [] };
    entry.transition = transition;
    transition.timer = setTimeout(() => { if (current() === entry) close('transition_timeout'); }, timeoutMs);
    return transition;
  }

  function active(entry, id) {
    const transition = entry?.transition;
    if (entry !== current() || !transition || transition.id !== id) throw new Error('STALE_REQUEST');
    return transition;
  }

  function rememberFullscreen(entry) {
    if (owner().isFullScreen()) {
      entry.workspaceFullscreen = true;
      entry.fullscreenBounds = owner().getContentBounds();
    }
  }

  async function fullscreen(entry, enabled) {
    const window = owner(), transition = entry.transition;
    if (window.isFullScreen() === enabled) return;
    const event = enabled ? 'enter-full-screen' : 'leave-full-screen';
    let completed;
    const changed = new Promise(resolve => { completed = resolve; window.once(event, completed); });
    try {
      window.setFullScreen(enabled);
      await Promise.race([changed, transition.done.then(() => { throw new Error('STALE_REQUEST'); })]);
      active(entry, transition.id);
    } finally { window.removeListener(event, completed); }
  }

  async function leaveFullscreen(entry) {
    await fullscreen(entry, false);
    entry.windowedBounds = owner().getBounds();
    entry.windowedMaximized = owner().isMaximized();
  }

  async function restoreWorkspace(entry) {
    const id = entry.transition.id;
    if (entry.workspaceFullscreen && entry.windowedBounds) {
      owner().setBounds(entry.windowedBounds);
      if (entry.windowedMaximized) owner().maximize();
    }
    owner().showInactive();
    if (entry.workspaceFullscreen) {
      owner().show(); owner().focus();
      await fullscreen(entry, true);
    }
    // Wait for the restored application compositor before dropping the covering frame.
    await painted(entry, owner());
    active(entry, id);
  }

  async function painted(entry, window) {
    const transition = entry.transition, contents = window.webContents;
    const image = await contents.capturePage(undefined, { stayHidden: true });
    active(entry, transition.id);
    if (image.isEmpty()) throw new Error('Window frame is empty');
    const frame = new Promise(resolve => contents.beginFrameSubscription(false, resolve));
    try {
      contents.invalidate();
      await Promise.race([frame, transition.done.then(() => { throw new Error('STALE_REQUEST'); })]);
      active(entry, transition.id);
    } finally { if (!window.isDestroyed()) contents.endFrameSubscription(); }
  }

  async function renderHidden(entry, window) {
    const id = entry.transition.id;
    const image = await window.webContents.capturePage();
    active(entry, id);
    if (image.isEmpty()) throw new Error('Window frame is empty');
  }

  function prepareSurface(entry, transition) {
    const cover = new BrowserWindow({
      ...transition.stage, title: entry.config.title, frame: false, transparent: true, backgroundColor: '#00000000',
      show: false, alwaysOnTop: true, skipTaskbar: true, hasShadow: false, enableLargerThanScreen: true,
      resizable: false, minimizable: false, maximizable: false, fullscreenable: false,
      webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false },
    });
    cover.setIgnoreMouseEvents(true, { forward: true });
    if (platform === 'darwin' && entry.workspaceFullscreen) {
      cover.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
    }
    transition.cover = cover;
    transition.coverReady = cover.loadURL('data:text/html,' + encodeURIComponent('<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'"><style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:transparent}img{display:block;width:100%;height:100%}</style><img alt="">'));
    void transition.coverReady.catch(() => {});
  }

  async function holdFrame(entry, transition, image, bounds) {
    const cover = transition.cover;
    if (image.isEmpty()) throw new Error('Window handoff frame is empty');
    await transition.coverReady;
    active(entry, transition.id);
    if (platform !== 'linux') cover.setOpacity(0);
    else cover.hide();
    cover.setBounds(bounds);
    await cover.webContents.executeJavaScript(`(async () => { const image = document.querySelector('img'); image.src = ${JSON.stringify(image.toDataURL())}; await image.decode(); })()`);
    active(entry, transition.id);
    if (platform === 'linux') await renderHidden(entry, cover);
    cover.showInactive();
    await painted(entry, cover);
    if (platform !== 'linux') cover.setOpacity(1);
    cover.moveTop();
    // An opacity-zero frame is prepared, but it does not cover the source yet.
    await painted(entry, cover);
  }

  async function prepareContext(entry) {
    if (!owner().isFullScreen()) return;
    const transition = begin(entry, 'compact');
    rememberFullscreen(entry);
    Object.assign(transition, transitionBounds(entry.fullscreenBounds, entry.window.getBounds()));
    try {
      const image = await owner().webContents.capturePage(undefined, { stayHidden: true });
      active(entry, transition.id);
      prepareSurface(entry, transition);
      await holdFrame(entry, transition, image, entry.fullscreenBounds);
      await leaveFullscreen(entry);
      owner().setContentBounds(entry.fullscreenBounds);
      await painted(entry, owner());
      transition.sourceImage = image;
      transition.contextPrepared = true;
    } catch (error) {
      if (current() === entry && entry.transition === transition) close('transition_failed');
      throw error;
    }
  }

  async function reveal(entry, bounds) {
    const id = entry.transition.id, window = entry.window;
    const transparentPreparation = platform !== 'linux';
    if (transparentPreparation) window.setOpacity(0);
    else window.hide();
    window.setBounds(bounds);
    if (!transparentPreparation) await renderHidden(entry, window);
    window.showInactive();
    await painted(entry, window);
    active(entry, id);
    if (transparentPreparation) window.setOpacity(1);
    await painted(entry, window);
  }

  async function settleSurface(entry, transition) {
    // A native resize and its renderer layout are not one atomic operation. Keep the
    // submitted endpoint on a separate surface until the final window has painted.
    const cover = transition.cover;
    try {
      const image = await entry.window.webContents.capturePage(undefined, { stayHidden: true });
      active(entry, transition.id);
      await holdFrame(entry, transition, image, transition.stage);
      entry.window.setBounds(transition.finalBounds);
      do {
        for (const operation of transition.pending.splice(0)) {
          try { operation.value = operation.action(); transition.applied.push(operation); }
          catch (error) { operation.reject(error); }
        }
        await painted(entry, entry.window);
      } while (transition.pending.length);
      if (transition.allWorkspaces !== undefined) {
        entry.window.setVisibleOnAllWorkspaces(transition.allWorkspaces, { skipTransformProcessType: true });
      }
      entry.window.setIgnoreMouseEvents(!entry.interactive, { forward: true });
      complete(entry, transition);
    } finally {
      if (!cover.isDestroyed()) cover.destroy();
      transition.cover = null;
    }
  }

  function complete(entry, transition) {
    active(entry, transition.id);
    if (transition.cover && !transition.cover.isDestroyed()) transition.cover.destroy();
    transition.cover = null;
    entry.transition = null;
    clearTimeout(transition.timer);
    for (const operation of transition.applied) operation.resolve(operation.value);
    for (const operation of transition.pending) operation.resolve(false);
    transition.resolve(true);
  }

  async function prepare(entry, direction, options) {
    if (!transitionOptions(options)) throw new Error('Invalid window transition');
    const transition = direction === 'compact' && entry.transition?.contextPrepared
      ? active(entry, entry.transition.id) : begin(entry, direction);
    if (direction === 'compact') rememberFullscreen(entry);
    const workspace = direction === 'workspace' && entry.workspaceFullscreen ? entry.fullscreenBounds : owner().getContentBounds();
    const surface = entry.window.getBounds(), geometry = transitionBounds(workspace, surface);
    Object.assign(transition, { finalBounds: surface, ...geometry });
    try {
      const image = transition.sourceImage || await owner().webContents.capturePage(undefined, { stayHidden: true });
      active(entry, transition.id);
      if (image.isEmpty()) throw new Error('Window capture is empty');
      if (!transition.contextPrepared) {
        prepareSurface(entry, transition);
        const source = direction === 'compact' ? owner() : entry.window;
        const sourceImage = direction === 'compact' ? image : await source.webContents.capturePage(undefined, { stayHidden: true });
        active(entry, transition.id);
        await holdFrame(entry, transition, sourceImage, source.getContentBounds());
      }
      // The plugin may now change its DOM without exposing those preparation steps.
      if (direction === 'workspace') entry.window.hide();
      return { id: transition.id, direction, durationMs: options.durationMs, image: image.toDataURL(),
        viewport: { width: geometry.stage.width, height: geometry.stage.height },
        workArea: screen.getDisplayMatching(surface).workArea,
        workspace: geometry.workspace, surface: geometry.surface };
    } catch (error) {
      if (current() === entry && entry.transition === transition) close('transition_failed');
      throw error;
    }
  }

  async function start(entry, id) {
    const transition = active(entry, id);
    if (transition.started) throw new Error('STALE_REQUEST');
    transition.started = true;
    entry.window.setIgnoreMouseEvents(true, { forward: true });
    if (platform === 'darwin' && entry.workspaceFullscreen) {
      transition.allWorkspaces = entry.window.isVisibleOnAllWorkspaces();
      entry.window.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true });
    }
    await reveal(entry, transition.stage);
    if (transition.direction === 'compact' && entry.workspaceFullscreen && !transition.contextPrepared) {
      entry.window.show(); entry.window.focus();
      await leaveFullscreen(entry);
      // Keep the rendered viewport at its full size while it is still covered and
      // visible. Resizing a hidden macOS surface can suspend capture indefinitely.
      owner().setContentBounds(entry.fullscreenBounds);
      await painted(entry, owner());
    }
    active(entry, id);
    activate(entry);
    transition.cover.hide();
    return true;
  }

  async function finish(entry, id) {
    const transition = active(entry, id);
    if (!transition.started) throw new Error('STALE_REQUEST');
    if (transition.direction === 'workspace') {
      await restoreWorkspace(entry);
      complete(entry, transition);
      close('workspace');
    } else {
      await settleSurface(entry, transition);
    }
    return true;
  }

  function compact(entry) {
    if (entry.transition?.contextPrepared) {
      const transition = entry.transition;
      return reveal(entry, entry.window.getBounds()).then(() => {
        active(entry, transition.id); activate(entry); complete(entry, transition); return true;
      });
    }
    if (!owner().isFullScreen()) { activate(entry); return true; }
    const transition = begin(entry, 'compact'); rememberFullscreen(entry);
    return leaveFullscreen(entry).then(() => {
      active(entry, transition.id); activate(entry); complete(entry, transition); return true;
    });
  }

  function workspace(entry) {
    if (!entry.workspaceFullscreen) { close('workspace'); return true; }
    const transition = begin(entry, 'workspace');
    return restoreWorkspace(entry).then(() => {
      complete(entry, transition); close('workspace'); return true;
    });
  }

  function settled(entry, action) {
    if (!entry.transition) return action();
    return new Promise((resolve, reject) => entry.transition.pending.push({ action, resolve, reject }));
  }

  return { prepareContext, prepare, start, finish, cancel, settled, compact, workspace };
}

module.exports = { createWindowTransitions, transitionBounds, transitionOptions };
