const { contextBridge, ipcRenderer } = require('electron');

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args);

const frontendOnly = (() => {
  try {
    return ipcRenderer.sendSync('desktop:is-frontend-only');
  } catch {
    return false;
  }
})();

const desktopApi = Object.freeze({
  isElectron: true,
  apiBase: frontendOnly ? 'http://127.0.0.1:19000' : '',
  wsBase: frontendOnly ? 'ws://127.0.0.1:19000' : '',
  minimizeWindow: () => invoke('desktop:minimize-window'),
  toggleFullscreenWindow: () => invoke('desktop:toggle-fullscreen-window'),
  closeWindow: () => invoke('desktop:close-window'),
  downloadFile: (url, filename) => invoke('desktop:download-file', url, filename),
  installUpdate: installerPath => invoke('desktop:install-update', installerPath),
  saveDataUrl: (dataUrl, filename) => invoke('desktop:save-data-url', dataUrl, filename),
  selectProjectDirectory: () => invoke('desktop:select-project-directory'),
  onLayoutInvalidated: callback => {
    const listener = () => callback();
    ipcRenderer.on('desktop:layout-invalidated', listener);
    return () => ipcRenderer.removeListener('desktop:layout-invalidated', listener);
  },
  browser: Object.freeze({
    // 所有调用都携带 sessionId：主进程为每个会话维护独立的浏览上下文。
    navigate: (url, sessionId) => invoke('browser:navigate', url, sessionId),
    goBack: sessionId => invoke('browser:go-back', sessionId),
    goForward: sessionId => invoke('browser:go-forward', sessionId),
    reload: sessionId => invoke('browser:reload', sessionId),
    stop: sessionId => invoke('browser:stop', sessionId),
    setBounds: (bounds, sessionId) => invoke('browser:set-bounds', bounds, sessionId),
    setVisible: (visible, sessionId) => invoke('browser:set-visible', visible, sessionId),
    getState: sessionId => invoke('browser:get-state', sessionId),
    onStateChanged: callback => {
      const listener = (_event, state) => callback(state);
      ipcRenderer.on('browser:state-changed', listener);
      return () => ipcRenderer.removeListener('browser:state-changed', listener);
    },
  }),
});

contextBridge.exposeInMainWorld('jiuwenDesktop', desktopApi);

// Keep the existing frontend desktop calls working while pywebview and Electron
// coexist. New desktop-only functionality should use window.jiuwenDesktop.
contextBridge.exposeInMainWorld('pywebview', {
  api: {
    minimize_window: desktopApi.minimizeWindow,
    toggle_fullscreen_window: desktopApi.toggleFullscreenWindow,
    close_window: desktopApi.closeWindow,
    download_file: desktopApi.downloadFile,
    install_update: desktopApi.installUpdate,
    save_data_url: desktopApi.saveDataUrl,
    select_project_directory: desktopApi.selectProjectDirectory,
  },
});
