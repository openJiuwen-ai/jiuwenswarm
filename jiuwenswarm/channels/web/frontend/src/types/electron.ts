export interface ElectronBrowserState {
  panelId: string;
  memberId: string;
  label: string;
  busy: boolean;
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
  beginBlobSave: (
    filename: string,
    mimeType: string,
    totalSize: number,
  ) => Promise<{ ok: boolean; cancelled?: boolean; transfer_id?: string }>;
  appendBlobSave: (transferId: string, encodedChunk: string) => Promise<boolean>;
  finishBlobSave: (transferId: string) => Promise<{ ok: boolean; cancelled?: boolean }>;
  abortBlobSave: (transferId: string) => Promise<boolean>;
  selectProjectDirectory: () => Promise<string | null>;
  selectLocalFiles: (
    allowMultiple?: boolean,
    initialDir?: string | null,
  ) => Promise<Array<Record<string, unknown>>>;
  selectLocalFilePath: (
    initialPath?: string | null,
    title?: string | null,
  ) => Promise<string | null>;
  describeLocalFiles: (
    paths: string[],
  ) => Promise<Array<Record<string, unknown>>>;
  getClipboardFiles: () => Promise<Array<Record<string, unknown>>>;
  onLayoutInvalidated: (callback: () => void) => () => void;
  browser: {
    navigate: (url: string, sessionId: string) => Promise<ElectronBrowserState>;
    goBack: (sessionId: string) => Promise<ElectronBrowserState>;
    goForward: (sessionId: string) => Promise<ElectronBrowserState>;
    reload: (sessionId: string) => Promise<ElectronBrowserState>;
    stop: (sessionId: string) => Promise<ElectronBrowserState>;
    setBounds: (bounds: ElectronBrowserBounds, sessionId: string) => Promise<ElectronBrowserBounds>;
    /** focus=false：恢复显示时不抢主窗口焦点（模态弹窗关闭后的恢复路径） */
    setVisible: (visible: boolean, sessionId: string, focus?: boolean) => Promise<boolean>;
    getState: (sessionId: string) => Promise<ElectronBrowserState>;
    listPanels: (sessionId: string) => Promise<ElectronBrowserState[]>;
    onPanelsChanged: (callback: (panels: ElectronBrowserState[]) => void) => () => void;
    onStateChanged: (callback: (state: ElectronBrowserState) => void) => () => void;
  };
}
