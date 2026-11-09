const { contextBridge, ipcRenderer } = require('electron');
const channel = 'desktop:presentation:';
const subscribe = (name, callback) => {
  const listener = (_event, payload) => callback(payload);
  ipcRenderer.on(channel + name, listener);
  return () => ipcRenderer.removeListener(channel + name, listener);
};

// This window has no filesystem, browser, authentication or general desktop operations.
contextBridge.exposeInMainWorld('jiuwenPresentation', Object.freeze({
  bootstrap: () => ipcRenderer.invoke(channel + 'bootstrap'),
  ready: options => ipcRenderer.invoke(channel + 'ready', options),
  startTransition: id => ipcRenderer.invoke(channel + 'transition-start', id),
  finishTransition: id => ipcRenderer.invoke(channel + 'transition-finish', id),
  onTransition: callback => subscribe('transition', callback),
  failed: () => ipcRenderer.invoke(channel + 'failed'),
  request: request => ipcRenderer.invoke(channel + 'request', request),
  onUpdate: callback => subscribe('update', callback),
  onResult: callback => subscribe('result', callback),
}));
