/**
 * Renders an MCP App (io.modelcontextprotocol/ui) for one tool result.
 *
 * Preferred path (spec's double-iframe architecture): the outer iframe loads
 * the backend's sandbox proxy page from a different origin than this UI; the
 * proxy writes the app HTML into an inner iframe with document.write (so the
 * app has a real http:// document — CesiumJS and similar libraries break under
 * about:srcdoc) and relays messages. The CSP is sent by the proxy as an HTTP
 * header. Fallback when the proxy is unavailable: a srcdoc iframe sandboxed
 * without allow-same-origin, with the CSP inlined as a <meta> tag.
 *
 * AppBridge speaks the MCP Apps protocol over postMessage. App-initiated tool
 * calls are proxied to the MCP server through the backend; calls to tools not
 * marked read-only need user approval.
 */
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AppBridge,
  PostMessageTransport,
  buildAllowAttribute,
} from '@modelcontextprotocol/ext-apps/app-bridge';
import type { McpAppResult } from '../../types/message';
import {
  appToolNeedsApproval,
  buildAppDocument,
  callAppTool,
  loadAppResource,
  loadSandboxUrl,
  openAppLink,
  type McpAppResource,
} from '../../features/mcpApps/mcpAppHost';
import './McpAppView.css';

const HOST_INFO = { name: 'WorkSwarm', version: '0.2.5' };
const MIN_HEIGHT = 120;
const MAX_HEIGHT = 900;
const PROXY_SANDBOX = 'allow-scripts allow-same-origin allow-forms';
const SRCDOC_SANDBOX = 'allow-scripts allow-forms';

interface PendingApproval {
  tool: string;
  args: Record<string, unknown>;
  resolve: (approved: boolean) => void;
}

function colorMode(): 'light' | 'dark' {
  return document.documentElement.dataset.colorMode === 'dark' ? 'dark' : 'light';
}

export function McpAppView({ app }: { app: McpAppResult }) {
  const { t } = useTranslation();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [resource, setResource] = useState<McpAppResource | null>(null);
  // undefined while resolving; null means "no proxy, use the srcdoc fallback".
  const [sandboxUrl, setSandboxUrl] = useState<string | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [height, setHeight] = useState(360);
  const [approval, setApproval] = useState<PendingApproval | null>(null);
  // Latest tool input/result, read when the app initializes. Store updates can
  // hand us an equal-but-new object; that must not remount the app.
  const appRef = useRef(app);
  appRef.current = app;
  // Latest ui/update-model-context payload from the app. Kept per view; not
  // yet forwarded to the agent's context.
  const modelContextRef = useRef<unknown>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    Promise.all([loadAppResource(app.server, app.resourceUri), loadSandboxUrl()])
      .then(([loaded, url]) => {
        if (cancelled) return;
        setSandboxUrl(url);
        setResource(loaded);
      })
      .catch((err: unknown) => { if (!cancelled) setError(err instanceof Error ? err.message : String(err)); });
    return () => { cancelled = true; };
  }, [app.server, app.resourceUri]);

  const useProxy = typeof sandboxUrl === 'string';

  useEffect(() => {
    const iframe = iframeRef.current;
    const target = iframe?.contentWindow;
    if (!resource || sandboxUrl === undefined || !iframe || !target) return;
    let disposed = false;
    const documentOptions = { forwardConsoleInfo: import.meta.env.DEV, inlineCsp: !sandboxUrl };

    const bridge = new AppBridge(
      null,
      HOST_INFO,
      {
        openLinks: {},
        serverTools: {},
        updateModelContext: { text: {} },
        sandbox: { csp: resource.csp },
      },
      {
        hostContext: {
          theme: colorMode(),
          displayMode: 'inline',
          availableDisplayModes: ['inline'],
          platform: 'desktop',
          locale: navigator.language,
          timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
          containerDimensions: { maxHeight: MAX_HEIGHT, width: iframe.clientWidth || undefined },
          toolInfo: { tool: { name: app.tool, inputSchema: { type: 'object' } } },
        },
      },
    );

    bridge.onsandboxready = () => {
      void bridge.sendSandboxResourceReady({
        html: buildAppDocument(resource, documentOptions),
        csp: resource.csp,
        permissions: resource.permissions,
      });
    };
    bridge.oninitialized = () => {
      const { arguments: args, toolResult } = appRef.current;
      void bridge.sendToolInput({ arguments: args });
      if (toolResult) {
        void bridge.sendToolResult(toolResult as Parameters<AppBridge['sendToolResult']>[0]);
      }
    };
    bridge.onloggingmessage = ({ level, logger, data }) => {
      console.warn(`[mcp-app ${app.server}/${app.tool}] ${logger ?? 'log'} (${level}) ${JSON.stringify(data)}`);
    };
    bridge.onsizechange = ({ height: next }) => {
      if (typeof next === 'number' && next > 0) {
        setHeight(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.ceil(next))));
      }
    };
    bridge.onupdatemodelcontext = async (params) => {
      modelContextRef.current = params;
      return {};
    };
    bridge.onopenlink = async ({ url }) => {
      const opened = await openAppLink(url);
      return opened ? {} : { isError: true };
    };
    bridge.oncalltool = async ({ name, arguments: args }) => {
      if (await appToolNeedsApproval(app.server, name)) {
        const approved = await new Promise<boolean>((resolve) => {
          setApproval({ tool: name, args: args ?? {}, resolve });
        });
        setApproval(null);
        if (!approved) {
          return {
            content: [{ type: 'text', text: `The user declined to run ${name}.` }],
            isError: true,
          };
        }
      }
      const result = await callAppTool(app.server, name, args);
      return result as Awaited<ReturnType<NonNullable<AppBridge['oncalltool']>>>;
    };

    void bridge
      .connect(new PostMessageTransport(target, target))
      .then(() => {
        // Load only after the bridge listens (for sandbox-proxy-ready or ui/initialize).
        if (disposed) return;
        if (sandboxUrl) {
          const url = new URL(sandboxUrl);
          url.searchParams.set('csp', JSON.stringify(resource.csp ?? {}));
          iframe.src = url.href;
        } else {
          iframe.srcdoc = buildAppDocument(resource, documentOptions);
        }
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));

    return () => {
      disposed = true;
      void bridge.teardownResource({}).catch(() => undefined).finally(() => bridge.close());
    };
  }, [resource, sandboxUrl, app.server, app.tool]);

  if (error) {
    return (
      <div className="mcp-app-view mcp-app-view--error" data-testid="chat-panel-mcp-app-view-error">
        {t('mcpApp.loadFailed')}: {error}
      </div>
    );
  }

  return (
    <div
      className="mcp-app-view"
      data-testid="chat-panel-mcp-app-view"
      data-variant={`${app.server}/${app.tool}`}
      data-prefers-border={resource?.prefersBorder === false ? 'false' : 'true'}
    >
      {!resource ? (
        <div className="mcp-app-view__loading" data-testid="chat-panel-mcp-app-view-loading">
          {t('mcpApp.loading')}
        </div>
      ) : null}
      {sandboxUrl !== undefined ? (
        <iframe
          ref={iframeRef}
          key={useProxy ? 'proxy' : 'srcdoc'}
          title={`${app.server} · ${app.tool}`}
          className="mcp-app-view__frame"
          data-testid="chat-panel-mcp-app-view-frame"
          sandbox={useProxy ? PROXY_SANDBOX : SRCDOC_SANDBOX}
          allow={buildAllowAttribute(resource?.permissions)}
          referrerPolicy={useProxy ? 'origin' : 'no-referrer'}
          style={{ height: resource ? height : 0 }}
        />
      ) : null}
      {approval ? (
        <div className="mcp-app-view__approval" role="alertdialog" data-testid="chat-panel-mcp-app-view-approval">
          <div className="mcp-app-view__approval-text" data-testid="chat-panel-mcp-app-view-approval-text">
            {t('mcpApp.approvalPrompt')} <code>{approval.tool}</code>
            <pre>{JSON.stringify(approval.args, null, 2)}</pre>
          </div>
          <div className="mcp-app-view__approval-actions">
            <button
              type="button"
              data-testid="chat-panel-mcp-app-view-approval-deny-btn"
              onClick={() => approval.resolve(false)}
            >
              {t('mcpApp.deny')}
            </button>
            <button
              type="button"
              className="is-primary"
              data-testid="chat-panel-mcp-app-view-approval-allow-btn"
              onClick={() => approval.resolve(true)}
            >
              {t('mcpApp.allow')}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
