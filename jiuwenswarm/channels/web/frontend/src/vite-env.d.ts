/// <reference types="vite/client" />
/// <reference types="vite-plugin-svgr/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_WS_BASE?: string;
  readonly VITE_PLATFORM?: string;
  readonly VITE_WEB_TRANSPORT?: string;
  readonly VITE_TRANSPORT?: string;
  readonly VITE_GATEWAY_HTTP_BASE?: string;
  readonly VITE_WEB_HTTP_BASE?: string;
  readonly VITE_JIUWENSWARM_EDITION?: string;
  readonly VITE_LOGIN_AUTH_SIMULATE?: string;
  readonly VITE_API_PREFIX?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

type DesktopSaveResult = {
  ok: boolean;
  cancelled?: boolean;
};

type DesktopBlobSaveStartResult = DesktopSaveResult & {
  transfer_id?: string;
};

interface Window {
  __JIUWEN_LOGIN_AUTH_SIMULATE__?: string;
  __JIUWEN_LOGIN_ENTRY_URL__?: string;
  __JIUWEN_WEB_TRANSPORT__?: string;
  __JIUWENSWARM_EDITION__?: string;
  /** Set by desktop_app.py after the webview page loads. */
  __JIUWEN_DESKTOP__?: boolean;
  /** Set by desktop_app.py when OS file-drag accept handlers are injected. */
  __JIUWEN_DESKTOP_DND__?: boolean;
  jiuwenDesktop?: import('./types/electron').JiuwenElectronDesktopApi;
  pywebview?: {
    api?: {
      paste_clipboard?: () => Promise<void>;
      /** pywebview 桌面壳的隐式授权一键授权（对齐 Electron captureOAuthToken）：
       * 抓厂商文档页提取当前授权直链，开子窗截取 login_success 跳转里的 access_token；
       * 失败/用户关窗 resolve null。旧壳没有此方法，调用前需判空。 */
      capture_oauth_token?: (options: {
        docPageUrl: string;
        successUrlPrefix: string;
        title?: string;
      }) => Promise<{ accessToken?: string } | null> | { accessToken?: string } | null;
      open_external_url?: (url: string) => Promise<boolean> | boolean;
      open_app_link?: (url: string) => Promise<boolean> | boolean;
      download_file?: (url: string, filename: string) => Promise<DesktopSaveResult> | DesktopSaveResult;
      begin_blob_save?: (filename: string, mimeType: string, totalSize: number) => Promise<DesktopBlobSaveStartResult> | DesktopBlobSaveStartResult;
      append_blob_save?: (transferId: string, encodedChunk: string) => Promise<boolean> | boolean;
      finish_blob_save?: (transferId: string) => Promise<DesktopSaveResult> | DesktopSaveResult;
      abort_blob_save?: (transferId: string) => Promise<boolean> | boolean;
      install_update?: (path: string) => Promise<boolean> | boolean;
      save_data_url?: (dataUrl: string, filename: string) => Promise<DesktopSaveResult> | DesktopSaveResult;
      select_project_directory?: () => Promise<string | null> | string | null;
      select_local_files?: (
        allowMultiple?: boolean,
        initialDir?: string | null,
      ) => Promise<Array<Record<string, unknown>>> | Array<Record<string, unknown>>;
      select_local_file_path?: (
        initialPath?: string | null,
        title?: string | null,
      ) => Promise<string | null> | string | null;
      describe_local_files?: (
        paths: string[],
      ) => Promise<Array<Record<string, unknown>>> | Array<Record<string, unknown>>;
      get_clipboard_files?: () =>
        | Promise<Array<Record<string, unknown>>>
        | Array<Record<string, unknown>>;
      get_close_action?: () => Promise<'ask' | 'hide' | 'quit' | null> | 'ask' | 'hide' | 'quit' | null;
      set_close_action?: (action: 'ask' | 'hide' | 'quit') => Promise<boolean> | boolean;
    };
  };
  /** Durable ingest hook invoked by desktop_app.py run_js on native file drops. */
  __JIUWEN_INGEST_LOCAL_FILES__?: (detail: unknown) => void;
}

declare module 'virtual:login-auth-simulate-provider' {
  export const simulatedAuthProvider: import('./auth/types').EnterpriseAuthProvider | null;
}
