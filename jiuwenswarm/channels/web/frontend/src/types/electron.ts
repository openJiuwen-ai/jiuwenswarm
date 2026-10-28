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

export type ElectronDownloadState = 'downloading' | 'paused' | 'completed' | 'cancelled' | 'interrupted';

export interface ElectronDownloadRecord {
  id: string;
  filename: string;
  url: string;
  /** 本地保存路径；完成（completed）后可用于打开文件 */
  path: string;
  receivedBytes: number;
  totalBytes: number;
  state: ElectronDownloadState;
  /** 系统文件类型图标（data URL，与资源管理器一致）；解析失败时缺省走通用 SVG */
  icon?: string;
  /** 开始时间（毫秒时间戳） */
  startTime: number;
  endTime: number;
  /** 仅 completed 态：文件已不在记录的保存路径（被移动/删除），面板显示"已删除" */
  fileMissing?: boolean;
}

export interface ElectronShareImageExportProgress {
  sessionId: string;
  phase: 'loading_snapshot' | 'rendering' | 'saving';
  progress: number | null;
}

/** 下载气泡锚点矩形：主窗口内容区坐标系（CSS px），来自 getBoundingClientRect */
export interface ElectronBubbleAnchorRect {
  left: number;
  top: number;
  right: number;
  bottom: number;
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
    listDownloads: () => Promise<ElectronDownloadRecord[]>;
    downloadAction: (downloadId: string, action: 'pause' | 'resume' | 'cancel') => Promise<boolean>;
    openDownload: (downloadId: string) => Promise<boolean>;
    /** 在系统文件管理器中显示已完成的下载文件 */
    showDownloadInFolder: (downloadId: string) => Promise<boolean>;
    /** 从下载记录列表中移除一条记录（仅限无活跃 DownloadItem 的记录） */
    removeDownload: (downloadId: string) => Promise<boolean>;
    openDownloadsFolder: () => Promise<boolean>;
    onDownloadsChanged: (callback: (downloads: ElectronDownloadRecord[]) => void) => () => void;
    /** Chrome 式下载气泡：锚定在下载按钮下方（CSS px，相对窗口内容区），悬浮于浏览器内容之上 */
    showDownloadBubble: (anchorRect: ElectronBubbleAnchorRect | null) => Promise<boolean>;
    hideDownloadBubble: () => Promise<boolean>;
    onDownloadBubbleChanged: (callback: (visible: boolean) => void) => () => void;
    /** Optional for older shells; hides pages without closing their MCP bindings. */
    setHeadless?: (headless: boolean) => Promise<boolean>;
    getState: (sessionId: string) => Promise<ElectronBrowserState>;
    listPanels: (sessionId: string) => Promise<ElectronBrowserState[]>;
    onPanelsChanged: (callback: (panels: ElectronBrowserState[]) => void) => () => void;
    onStateChanged: (callback: (state: ElectronBrowserState) => void) => () => void;
  };
}
