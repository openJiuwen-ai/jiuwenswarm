const { contextBridge, ipcRenderer, webUtils } = require('electron');

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
  getCloseAction: () => invoke('desktop:get-close-action'),
  setCloseAction: action => invoke('desktop:set-close-action', action),
  openExternalUrl: url => invoke('desktop:open-external-url', url),
  captureOAuthToken: options => invoke('desktop:capture-oauth-token', options),
  exportShareImage: (sessionId, locale) => invoke('desktop:export-share-image', sessionId, locale),
  onShareImageExportProgress: callback => {
    const listener = (_event, progress) => callback(progress);
    ipcRenderer.on('desktop:share-image-export-progress', listener);
    return () => ipcRenderer.removeListener('desktop:share-image-export-progress', listener);
  },
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
  describeDroppedFiles: files => {
    // File objects cannot cross IPC. Resolve their native paths in the preload.
    const paths = Array.from(files, file => webUtils.getPathForFile(file));
    if (!paths.length || paths.some(filePath => !filePath)) {
      return Promise.resolve([]);
    }
    return invoke('desktop:describe-local-files', [...new Set(paths)]);
  },
  getClipboardFiles: () => invoke('desktop:get-clipboard-files'),
  pasteClipboard: () => invoke('desktop:paste-clipboard'),
  clearHuaweiSignIn: () => invoke('auth:clear-huawei-sign-in'),
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
    listDownloads: () => invoke('browser:list-downloads'),
    downloadAction: (downloadId, action) => invoke('browser:download-action', downloadId, action),
    openDownload: downloadId => invoke('browser:open-download', downloadId),
    showDownloadInFolder: downloadId => invoke('browser:show-download-in-folder', downloadId),
    removeDownload: downloadId => invoke('browser:remove-download', downloadId),
    openDownloadsFolder: () => invoke('browser:open-downloads-folder'),
    onDownloadsChanged: callback => {
      const listener = (_event, downloads) => callback(downloads);
      ipcRenderer.on('browser:downloads-changed', listener);
      return () => ipcRenderer.removeListener('browser:downloads-changed', listener);
    },
    // Chrome 式下载气泡：锚定在工具栏下载按钮下方，原生子视图悬浮于浏览器内容之上。
    showDownloadBubble: anchorRect => invoke('browser:show-download-bubble', anchorRect),
    hideDownloadBubble: () => invoke('browser:hide-download-bubble'),
    onDownloadBubbleChanged: callback => {
      const listener = (_event, visible) => callback(visible);
      ipcRenderer.on('browser:download-bubble-changed', listener);
      return () => ipcRenderer.removeListener('browser:download-bubble-changed', listener);
    },
    setHeadless: headless => invoke('browser:set-headless', headless),
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
    get_close_action: desktopApi.getCloseAction,
    set_close_action: desktopApi.setCloseAction,
    open_external_url: desktopApi.openExternalUrl,
    capture_oauth_token: desktopApi.captureOAuthToken,
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
    paste_clipboard: desktopApi.pasteClipboard,
  },
});
