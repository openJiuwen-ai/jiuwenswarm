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
  openExternalUrl: url => invoke('desktop:open-external-url', url),
  downloadFile: (url, filename) => invoke('desktop:download-file', url, filename),
  installUpdate: installerPath => invoke('desktop:install-update', installerPath),
  saveDataUrl: (dataUrl, filename) => invoke('desktop:save-data-url', dataUrl, filename),
  beginBlobSave: (filename, mimeType, totalSize) => invoke('desktop:begin-blob-save', filename, mimeType, totalSize),
  appendBlobSave: (transferId, encodedChunk) => invoke('desktop:append-blob-save', transferId, encodedChunk),
  finishBlobSave: transferId => invoke('desktop:finish-blob-save', transferId),
  abortBlobSave: transferId => invoke('desktop:abort-blob-save', transferId),
  selectProjectDirectory: () => invoke('desktop:select-project-directory'),
  selectLocalFiles: (allowMultiple, initialDir) => invoke('desktop:select-local-files', allowMultiple, initialDir),
  selectLocalFilePath: (initialPath, title) => invoke('desktop:select-local-file-path', initialPath, title),
  describeLocalFiles: paths => invoke('desktop:describe-local-files', paths),
  getClipboardFiles: () => invoke('desktop:get-clipboard-files'),
  onLayoutInvalidated: callback => {
    const listener = () => callback();
    ipcRenderer.on('desktop:layout-invalidated', listener);
    return () => ipcRenderer.removeListener('desktop:layout-invalidated', listener);
  },
  browser: Object.freeze({
    // Operations target a panel ID; listing is scoped to the conversation ID.
    navigate: (url, sessionId) => invoke('browser:navigate', url, sessionId),
    goBack: sessionId => invoke('browser:go-back', sessionId),
    goForward: sessionId => invoke('browser:go-forward', sessionId),
    reload: sessionId => invoke('browser:reload', sessionId),
    stop: sessionId => invoke('browser:stop', sessionId),
    setBounds: (bounds, sessionId) => invoke('browser:set-bounds', bounds, sessionId),
    setVisible: (visible, sessionId, focus) => invoke('browser:set-visible', visible, sessionId, focus !== false),
    getState: sessionId => invoke('browser:get-state', sessionId),
    listPanels: sessionId => invoke('browser:list-panels', sessionId),
    onPanelsChanged: callback => {
      const listener = (_event, panels) => callback(panels);
      ipcRenderer.on('browser:panels-changed', listener);
      return () => ipcRenderer.removeListener('browser:panels-changed', listener);
    },
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
    open_external_url: desktopApi.openExternalUrl,
    download_file: desktopApi.downloadFile,
    install_update: desktopApi.installUpdate,
    save_data_url: desktopApi.saveDataUrl,
    begin_blob_save: desktopApi.beginBlobSave,
    append_blob_save: desktopApi.appendBlobSave,
    finish_blob_save: desktopApi.finishBlobSave,
    abort_blob_save: desktopApi.abortBlobSave,
    select_project_directory: desktopApi.selectProjectDirectory,
    select_local_files: desktopApi.selectLocalFiles,
    select_local_file_path: desktopApi.selectLocalFilePath,
    describe_local_files: desktopApi.describeLocalFiles,
    get_clipboard_files: desktopApi.getClipboardFiles,
  },
});
