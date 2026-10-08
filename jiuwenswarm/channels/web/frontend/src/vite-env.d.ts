/// <reference types="vite/client" />
/// <reference types="vite-plugin-svgr/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
  readonly VITE_WS_BASE?: string;
  readonly VITE_WEB_TRANSPORT?: string;
  readonly VITE_TRANSPORT?: string;
  readonly VITE_GATEWAY_HTTP_BASE?: string;
  readonly VITE_WEB_HTTP_BASE?: string;
  readonly VITE_JIUWENSWARM_EDITION?: string;
  readonly VITE_LOGIN_AUTH_SIMULATE?: string;
  readonly VITE_BASE_PATH?: string;
  readonly VITE_API_PREFIX?: string;
  readonly VITE_WELCOME_TITLE_ZH?: string;
  readonly VITE_WELCOME_TITLE_EN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

type DesktopSaveResult = {
  ok: boolean;
  cancelled?: boolean;
};

interface Window {
  /** Set by desktop_app.py after the webview page loads. */
  __JIUWEN_DESKTOP__?: boolean;
  /** Set by desktop_app.py when OS file-drag accept handlers are injected. */
  __JIUWEN_DESKTOP_DND__?: boolean;
  pywebview?: {
    api?: {
      download_file?: (url: string, filename: string) => Promise<DesktopSaveResult> | DesktopSaveResult;
      install_update?: (path: string) => Promise<boolean> | boolean;
      save_data_url?: (dataUrl: string, filename: string) => Promise<DesktopSaveResult> | DesktopSaveResult;
      select_project_directory?: () => Promise<string | null> | string | null;
      select_local_files?: (
        allowMultiple?: boolean,
        initialDir?: string | null,
      ) => Promise<Array<Record<string, unknown>>> | Array<Record<string, unknown>>;
      describe_local_files?: (
        paths: string[],
      ) => Promise<Array<Record<string, unknown>>> | Array<Record<string, unknown>>;
      get_clipboard_files?: () =>
        | Promise<Array<Record<string, unknown>>>
        | Array<Record<string, unknown>>;
    };
  };
  /** Durable ingest hook invoked by desktop_app.py run_js on native file drops. */
  __JIUWEN_INGEST_LOCAL_FILES__?: (detail: unknown) => void;
  /** Edition injected by the User Web server (mirrors JIUWENSWARM_EDITION). */
  __JIUWENSWARM_EDITION__?: string;
  /** Login simulation switch injected by the User Web server. */
  __JIUWEN_LOGIN_AUTH_SIMULATE__?: boolean | string;
  /** Web transport mode injected by the User Web server (websocket | http). */
  __JIUWEN_WEB_TRANSPORT__?: string;
  /** Workspace quota feature flag injected at serve time (WORKSPACE_QUOTA_ENABLED). */
  __WORKSPACE_QUOTA_ENABLED__?: boolean | string;
}

declare module 'virtual:login-auth-simulate-provider' {
  import type { EnterpriseAuthProvider } from './auth/types';
  export const simulatedAuthProvider: EnterpriseAuthProvider | null;
}
