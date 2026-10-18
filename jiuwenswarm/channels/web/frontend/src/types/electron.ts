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

export interface ElectronShareImageExportProgress {
  sessionId: string;
  phase: 'loading_snapshot' | 'rendering' | 'saving';
  progress: number | null;
}

export interface JiuwenElectronDesktopApi {
  readonly isElectron: true;
  readonly apiBase?: string;
  readonly wsBase?: string;
  minimizeWindow: () => Promise<boolean>;
  toggleFullscreenWindow: () => Promise<boolean>;
  closeWindow: () => Promise<boolean>;
  getCloseAction: () => Promise<'ask' | 'hide' | 'quit' | null>;
  setCloseAction: (action: 'ask' | 'hide' | 'quit') => Promise<boolean>;
  /** 打开模态授权窗口截取隐式授权（implicit grant）跳转 URL 片段里的 access_token。
   * 授权直链由主进程实时抓取 docPageUrl（厂商文档页）提取——不硬编码，跟随官方密钥
   * 轮换。抓取失败、用户关闭窗口或校验失败时 resolve null，前端保持手动粘贴。
   * 旧版本 Electron 壳可能没有此方法，调用前需判空。 */
  captureOAuthToken?: (options: {
    docPageUrl: string;
    successUrlPrefix: string;
    title?: string;
  }) => Promise<{ accessToken: string } | null>;
  exportShareImage: (sessionId: string, locale: string) => Promise<boolean>;
  onShareImageExportProgress: (callback: (progress: ElectronShareImageExportProgress) => void) => () => void;
  downloadFile: (url: string, filename: string) => Promise<boolean | { ok: boolean; cancelled?: boolean }>;
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
  /** Optional for compatibility with older Electron shells; accepts native dropped Files. */
  describeDroppedFiles?: (files: File[]) => Promise<Array<Record<string, unknown>>>;
  getClipboardFiles: () => Promise<Array<Record<string, unknown>>>;
  pasteClipboard: () => Promise<void>;
  clearHuaweiSignIn?: () => Promise<number>;
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
