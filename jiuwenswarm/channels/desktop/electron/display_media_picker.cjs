// getDisplayMedia support for the trusted renderer.
//
// Electron does not ship Chrome's screen/window picker: without a
// setDisplayMediaRequestHandler the page cannot obtain a display stream (the
// full-duplex "share screen or window" source). macOS 15+ can use the native
// system picker; elsewhere a small modal lists desktopCapturer sources.

const PICKER_PROTOCOL = 'jiuwenswarm-display-choice:';
const THUMBNAIL_SIZE = { width: 320, height: 180 };

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

function orderSources(sources) {
  const screens = sources.filter(source => source.id.startsWith('screen:'));
  const windows = sources.filter(source => !source.id.startsWith('screen:'));
  return [...screens, ...windows];
}

function pickerHtml(sources) {
  const items = sources.map((source, index) => {
    const thumbnail = source.thumbnail && !source.thumbnail.isEmpty()
      ? `<img src="data:image/jpeg;base64,${source.thumbnail.toJPEG(70).toString('base64')}" alt="">`
      : '<span class="empty"></span>';
    return `<a class="source" href="${PICKER_PROTOCOL}//select?index=${index}" title="${escapeHtml(source.name)}">`
      + `${thumbnail}<span class="name">${escapeHtml(source.name)}</span></a>`;
  }).join('');
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="color-scheme" content="light dark"><style>
    *{box-sizing:border-box}body{margin:0;padding:20px;font:13px/1.5 system-ui,sans-serif;color:CanvasText;background:Canvas}
    h1{margin:0 0 14px;font-size:15px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}
    .source{display:flex;flex-direction:column;gap:6px;padding:8px;border:1px solid ButtonBorder;border-radius:8px;color:inherit;text-decoration:none}
    .source:hover,.source:focus{outline:2px solid Highlight;outline-offset:-1px}
    img,.empty{display:block;width:100%;aspect-ratio:16/9;object-fit:contain;background:#0002;border-radius:4px}
    .name{overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
    .actions{display:flex;justify-content:flex-end;margin-top:16px}
    button{min-width:80px;padding:6px 16px;color:ButtonText;background:ButtonFace;border:1px solid ButtonBorder;border-radius:4px}
  </style></head><body><h1>选择要共享的屏幕或窗口</h1><div class="grid">${items}</div>
  <div class="actions"><button type="button" onclick="location.href='${PICKER_PROTOCOL}//cancel'">取消</button></div>
  </body></html>`;
}

function promptForSource({ BrowserWindow, parent, sources }) {
  if (!sources.length) return Promise.resolve(null);
  const promptWindow = new BrowserWindow({
    parent: parent || undefined,
    modal: Boolean(parent),
    show: false,
    width: 720,
    height: 520,
    minimizable: false,
    maximizable: false,
    autoHideMenuBar: true,
    title: '共享屏幕',
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  return new Promise(resolve => {
    let settled = false;
    const finish = source => {
      if (settled) return;
      settled = true;
      resolve(source);
      if (!promptWindow.isDestroyed()) promptWindow.destroy();
    };
    promptWindow.webContents.on('will-navigate', (event, targetUrl) => {
      const target = new URL(targetUrl);
      if (target.protocol !== PICKER_PROTOCOL) return;
      event.preventDefault();
      const index = Number.parseInt(target.searchParams.get('index') || '', 10);
      finish(target.hostname === 'select' && sources[index] ? sources[index] : null);
    });
    promptWindow.on('closed', () => finish(null));
    promptWindow.once('ready-to-show', () => promptWindow.show());
    void promptWindow.loadURL(`data:text/html;charset=UTF-8,${encodeURIComponent(pickerHtml(sources))}`)
      .catch(() => finish(null));
  });
}

function registerDisplayMediaHandler({ session, desktopCapturer, BrowserWindow, getParentWindow, platform = process.platform }) {
  let picking = false;
  session.setDisplayMediaRequestHandler((request, callback) => {
    // One picker at a time; a second request while it is open is denied.
    if (picking || !request.videoRequested) {
      callback(null);
      return;
    }
    picking = true;
    void (async () => {
      const sources = orderSources(await desktopCapturer.getSources({
        types: ['screen', 'window'],
        thumbnailSize: THUMBNAIL_SIZE,
      }));
      return promptForSource({ BrowserWindow, parent: getParentWindow(), sources });
    })().then(
      source => callback(source ? { video: source } : null),
      error => {
        console.error('[electron] display media request failed', error);
        callback(null);
      },
    ).finally(() => {
      picking = false;
    });
  }, { useSystemPicker: platform === 'darwin' });
}

module.exports = { registerDisplayMediaHandler, orderSources, pickerHtml, PICKER_PROTOCOL };
