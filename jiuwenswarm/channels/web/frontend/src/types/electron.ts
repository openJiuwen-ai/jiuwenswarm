export interface ElectronBrowserState {
  /** 状态所属的浏览器会话；渲染层据此过滤非本会话的广播 */
  sessionId: string;
  url: string;
  title: string;
  loading: boolean;
  canGoBack: boolean;
  canGoForward: boolean;
}

export interface ElectronBrowserBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface JiuwenElectronDesktopApi {
  readonly isElectron: true;
  readonly apiBase?: string;
  readonly wsBase?: string;
  minimizeWindow: () => Promise<boolean>;
  toggleFullscreenWindow: () => Promise<boolean>;
  closeWindow: () => Promise<boolean>;
  downloadFile: (url: string, filename: string) => Promise<boolean>;
  installUpdate: (installerPath: string) => Promise<boolean>;
  saveDataUrl: (dataUrl: string, filename: string) => Promise<{ ok: boolean; cancelled?: boolean }>;
  selectProjectDirectory: () => Promise<string | null>;
  onLayoutInvalidated: (callback: () => void) => () => void;
  browser: {
    navigate: (url: string, sessionId: string) => Promise<ElectronBrowserState>;
    goBack: (sessionId: string) => Promise<ElectronBrowserState>;
    goForward: (sessionId: string) => Promise<ElectronBrowserState>;
    reload: (sessionId: string) => Promise<ElectronBrowserState>;
    stop: (sessionId: string) => Promise<ElectronBrowserState>;
    setBounds: (bounds: ElectronBrowserBounds, sessionId: string) => Promise<ElectronBrowserBounds>;
    setVisible: (visible: boolean, sessionId: string) => Promise<boolean>;
    getState: (sessionId: string) => Promise<ElectronBrowserState>;
    onStateChanged: (callback: (state: ElectronBrowserState) => void) => () => void;
  };
}
