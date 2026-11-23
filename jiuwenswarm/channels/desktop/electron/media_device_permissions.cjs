function httpOrigin(value) {
  if (typeof value !== 'string' || !URL.canParse(value)) return null;
  const url = new URL(value);
  return url.protocol === 'https:' || url.protocol === 'http:' ? url.origin : null;
}

function matchingMediaFrames(top, frameNames, origin, requestingUrl) {
  const matches = [];
  for (const frame of top.framesInSubtree) {
    if (frame.detached || frame.origin !== origin || frame.url !== requestingUrl || !frame.parent) continue;

    let container = frame;
    while (container.parent && container.parent.frameTreeNodeId !== top.frameTreeNodeId) {
      container = container.parent;
    }
    const name = frameNames.get(container.frameTreeNodeId);
    // Electron supplies an address, so every possible requester must have a qualifying container.
    if (!name || container.origin !== origin) return [];
    matches.push({ id: frame.frameTreeNodeId, token: frame.frameToken, name });
  }
  return matches.sort((first, second) => first.id - second.id);
}

function delegatesMedia(names, features, origin) {
  return names.every(name => {
    const elements = document.getElementsByName(name);
    const iframe = elements[0];
    return elements.length === 1 && iframe instanceof HTMLIFrameElement
      && features.every(feature => iframe.featurePolicy?.allowsFeature(feature, origin) === true);
  });
}

function devicePrompt(origin, mediaTypes, locale, signal) {
  const chinese = locale.toLowerCase().startsWith('zh');
  const labels = chinese ? { video: '摄像头', audio: '麦克风' } : { video: 'camera', audio: 'microphone' };
  const devices = ['video', 'audio'].filter(type => mediaTypes.includes(type)).map(type => labels[type]);
  return {
    type: 'question',
    title: chinese ? '设备权限' : 'Device access',
    message: chinese
      ? `允许此网页使用${devices.join('和')}吗？`
      : `Allow this website to use your ${devices.join(' and ')}?`,
    detail: origin,
    buttons: chinese ? ['拒绝', '允许本次'] : ['Deny', 'Allow once'],
    defaultId: 0,
    cancelId: 0,
    signal,
  };
}

function registerMediaDevicePermissions({ window, app, dialog, getFrontendUrl }) {
  const owner = window.webContents;
  const session = owner.session;
  const frameNames = new Map();
  let pending;
  const isChildMediaRequest = (contents, permission, details) =>
    contents === owner && permission === 'media' && details.isMainFrame !== true;
  const isHostOrigin = origin => origin !== null && !owner.isDestroyed() && origin === httpOrigin(owner.getURL());
  const cancelPrompt = () => pending?.controller.abort();

  owner.on('frame-created', (_event, { frame }) => {
    if (!frame?.parent || frame.parent.frameTreeNodeId !== owner.mainFrame.frameTreeNodeId) return;
    const id = frame.frameTreeNodeId;
    if (frameNames.has(id)) return;

    const liveIds = new Set(owner.mainFrame.frames.map(child => child.frameTreeNodeId));
    for (const knownId of frameNames.keys()) {
      if (!liveIds.has(knownId)) frameNames.delete(knownId);
    }
    // Capture the embedding element's name before the guest can change window.name.
    frameNames.set(id, frame.url === '' && frame.name.startsWith('workswarm-media-') ? frame.name : '');
  });

  owner.on('did-start-navigation', event => {
    if (event.isMainFrame && !event.isSameDocument) frameNames.clear();
    if (!pending || event.isSameDocument) return;
    if (event.isMainFrame || httpOrigin(event.frame?.origin) === pending.origin) {
      cancelPrompt();
    }
  });
  owner.on('render-process-gone', cancelPrompt);
  owner.once('destroyed', cancelPrompt);

  session.setPermissionCheckHandler((contents, permission, requestingOrigin, details) => {
    if (isChildMediaRequest(contents, permission, details)) {
      return isHostOrigin(httpOrigin(details.securityOrigin ?? requestingOrigin));
    }
    // Installing a session handler replaces Electron's default check.
    return permission !== 'deprecated-sync-clipboard-read';
  });

  async function confirm(origin, mediaTypes, requestingUrl) {
    const controller = new AbortController();
    pending = { origin, controller };
    try {
      const hostOrigin = httpOrigin(getFrontendUrl());
      if (!hostOrigin || !isHostOrigin(hostOrigin)) return false;

      const top = owner.mainFrame;
      const topToken = top.frameToken;
      const frames = matchingMediaFrames(top, frameNames, origin, requestingUrl);
      if (frames.length === 0) return false;

      const names = [...new Set(frames.map(frame => frame.name))];
      const features = mediaTypes.map(type => type === 'audio' ? 'microphone' : 'camera');
      const parameters = JSON.stringify([names, features, origin]);
      const checkSignal = AbortSignal.any([controller.signal, AbortSignal.timeout(2000)]);
      const delegated = await Promise.race([
        owner.executeJavaScriptInIsolatedWorld(1001, [{
          code: `(${delegatesMedia.toString()})(...${parameters})`,
        }]),
        new Promise(resolve => checkSignal.addEventListener('abort', () => resolve(false), { once: true })),
      ]);
      const sameRequest = () => !controller.signal.aborted && !window.isDestroyed()
        && owner.mainFrame.frameToken === topToken
        && JSON.stringify(matchingMediaFrames(owner.mainFrame, frameNames, origin, requestingUrl))
          === JSON.stringify(frames);
      if (delegated !== true || !sameRequest()) return false;

      const { response } = await dialog.showMessageBox(
        window,
        devicePrompt(origin, mediaTypes, app.getLocale(), controller.signal),
      );
      return response === 1 && sameRequest();
    } catch {
      return false;
    } finally {
      pending = undefined;
    }
  }

  session.setPermissionRequestHandler((contents, permission, reply, details) => {
    if (!isChildMediaRequest(contents, permission, details)) {
      // Keep Electron's default request policy for other permissions and windows.
      const locationDisabled = permission === 'geolocation' && process.platform === 'darwin'
        && app.commandLine.hasSwitch('disable-geolocation');
      reply(!locationDisabled);
      return;
    }
    const origin = httpOrigin(details.securityOrigin);
    if (isHostOrigin(origin)) {
      reply(true);
      return;
    }
    const types = details.mediaTypes;
    if (!origin || !Array.isArray(types) || !types.length || types.some(type => type !== 'video' && type !== 'audio')) {
      reply(false);
      return;
    }
    if (pending || window.isDestroyed()) {
      reply(false);
      return;
    }
    void confirm(origin, types, details.requestingUrl).then(reply);
  });
}

module.exports = { registerMediaDevicePermissions };
