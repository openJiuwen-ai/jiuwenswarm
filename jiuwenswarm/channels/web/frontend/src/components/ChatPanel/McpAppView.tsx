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
 * calls are proxied to the MCP server through the backend and always need user
 * approval: tool annotations such as readOnlyHint are untrusted server hints.
 * "Always allow" is remembered per app view and tool only.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  AppBridge,
  PostMessageTransport,
  buildAllowAttribute,
} from '@modelcontextprotocol/ext-apps/app-bridge';
import type { McpAppResult } from '../../types/message';
import {
  appToolClaimsReadOnly,
  buildAppDocument,
  callAppTool,
  loadAppResource,
  loadSandboxUrl,
  openAppLink,
  type McpAppResource,
} from '../../features/mcpApps/mcpAppHost';
import {
  beginMcpAppMessageRequest,
  dispatchMcpAppMessage,
  normalizeMcpAppMessage,
  registerMcpAppInstance,
  updateMcpAppContext,
  type McpAppInstanceIdentity,
} from '../../features/mcpApps/mcpAppContext';
import './McpAppView.css';

const HOST_INFO = { name: 'WorkSwarm', version: '0.2.5' };
const MIN_HEIGHT = 120;
const MAX_HEIGHT = 900;
const PROXY_SANDBOX = 'allow-scripts allow-same-origin allow-forms';
const SRCDOC_SANDBOX = 'allow-scripts allow-forms';

type ApprovalDecision = 'deny' | 'once' | 'always';

interface PendingApproval {
  tool: string;
  args: Record<string, unknown>;
  claimsReadOnly: boolean;
  resolve: (decision: ApprovalDecision) => void;
}

interface PendingMessage {
  text: string;
  resolve: (approved: boolean) => void;
  release: () => void;
}

function colorMode(): 'light' | 'dark' {
  return document.documentElement.dataset.colorMode === 'dark' ? 'dark' : 'light';
}

export function McpAppView({
  app,
  sessionId,
  appInstanceId,
}: {
  app: McpAppResult;
  sessionId: string | null;
  appInstanceId: string;
}) {
  const { t } = useTranslation();
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [resource, setResource] = useState<McpAppResource | null>(null);
  // undefined while resolving; null means "no proxy, use the srcdoc fallback".
  const [sandboxUrl, setSandboxUrl] = useState<string | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [height, setHeight] = useState(360);
  const [approval, setApproval] = useState<PendingApproval | null>(null);
  const [pendingMessage, setPendingMessage] = useState<PendingMessage | null>(null);
  const pendingMessageRef = useRef<PendingMessage | null>(null);
  const approvalResolveRef = useRef<((approved: ApprovalDecision) => void) | null>(null);
  // Latest tool input/result, read when the app initializes. Store updates can
  // hand us an equal-but-new object; that must not remount the app.
  const appRef = useRef(app);
  appRef.current = app;
  // Tools the user chose to always allow for this app view (not persisted).
  const alwaysAllowedRef = useRef<Set<string>>(new Set());
  const identity = useMemo<McpAppInstanceIdentity | null>(() => sessionId ? ({
    sessionId,
    appInstanceId,
    server: app.server,
    tool: app.tool,
    resourceUri: app.resourceUri,
  }) : null, [sessionId, appInstanceId, app.server, app.tool, app.resourceUri]);

  useEffect(() => identity ? registerMcpAppInstance(identity) : undefined, [identity]);

  useEffect(() => () => {
    const pending = pendingMessageRef.current;
    if (pending) {
      pending.release();
      pending.resolve(false);
      pendingMessageRef.current = null;
    }
  }, []);

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
    let sandboxReadyTimer: ReturnType<typeof setTimeout> | null = null;
    const documentOptions = { forwardConsoleInfo: import.meta.env.DEV, inlineCsp: !sandboxUrl };

    const bridge = new AppBridge(
      null,
      HOST_INFO,
      {
        openLinks: {},
        serverTools: {},
        message: { text: {} },
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
      if (sandboxReadyTimer) {
        clearTimeout(sandboxReadyTimer);
        sandboxReadyTimer = null;
      }
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
      if (!identity) throw new Error('The MCP App view is not interactive.');
      const result = updateMcpAppContext(identity, params);
      if (result.error) throw new Error(result.error);
      return {};
    };
    bridge.onmessage = async (params) => {
      if (!identity) return { isError: true };
      const normalized = normalizeMcpAppMessage(params);
      if (!normalized.value) return { isError: true, error: normalized.error };
      const request = beginMcpAppMessageRequest(identity);
      if (!request.value) return { isError: true, error: request.error };
      const approved = await new Promise<boolean>((resolve) => {
        const pending = { text: normalized.value!, resolve, release: request.value! };
        pendingMessageRef.current = pending;
        setPendingMessage(pending);
      });
      const pending = pendingMessageRef.current;
      pending?.release();
      pendingMessageRef.current = null;
      setPendingMessage(null);
      if (!approved) return { isError: true };
      try {
        const sent = await dispatchMcpAppMessage(identity.sessionId, normalized.value);
        return sent ? {} : { isError: true };
      } catch {
        return { isError: true };
      }
    };
    bridge.onopenlink = async ({ url }) => {
      const opened = await openAppLink(url);
      return opened ? {} : { isError: true };
    };
    bridge.oncalltool = async ({ name, arguments: args }) => {
      if (!alwaysAllowedRef.current.has(name)) {
        const claimsReadOnly = await appToolClaimsReadOnly(app.server, name);
        const decision = await new Promise<ApprovalDecision>((resolve) => {
          approvalResolveRef.current = resolve;
          setApproval({ tool: name, args: args ?? {}, claimsReadOnly, resolve });
        });
        approvalResolveRef.current = null;
        setApproval(null);
        if (decision === 'always') alwaysAllowedRef.current.add(name);
        if (decision === 'deny') {
          return {
            content: [{ type: 'text', text: `The user declined to run ${name}.` }],
            isError: true,
          };
        }
      }
      try {
        const result = await callAppTool(app.server, name, args);
        return result as Awaited<ReturnType<NonNullable<AppBridge['oncalltool']>>>;
      } catch (err: unknown) {
        // webRequest 在 ok=false / 断连时 reject：rejection 逃逸进 bridge
        // 会让 App 端 tools/call 悬挂——转成显式 isError 结果。
        const message = err instanceof Error ? err.message : String(err);
        return { content: [{ type: 'text', text: message }], isError: true };
      }
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

    // CR-3：代理页加载失败/被拒（HOST_PATTERN 拒绝、瞬时故障）时 bridge
    // 可能永远不 ready——超时回退 srcdoc，避免 App 永久空白无提示。
    if (sandboxUrl) {
      sandboxReadyTimer = setTimeout(() => {
        if (disposed) return;
        console.warn('[mcp-app] sandbox proxy not ready within 10s, falling back to srcdoc');
        setSandboxUrl(null);
      }, 10000);
    }

    return () => {
      disposed = true;
      if (sandboxReadyTimer) {
        clearTimeout(sandboxReadyTimer);
        sandboxReadyTimer = null;
      }
      // 卸载兜底：审批 Promise resolve(false)、待确认消息释放——避免
      // 审批期间卸载导致 Promise 悬挂（bridge 侧一直等到自身超时）。
      approvalResolveRef.current?.('deny');
      approvalResolveRef.current = null;
      pendingMessageRef.current?.release();
      pendingMessageRef.current = null;
      setPendingMessage(null);
      setApproval(null);
      void bridge.teardownResource({}).catch(() => undefined).finally(() => bridge.close());
    };
  }, [resource, sandboxUrl, app.server, app.tool, identity]);

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
            {approval.claimsReadOnly ? (
              <div className="mcp-app-view__approval-hint" data-testid="chat-panel-mcp-app-view-approval-read-only-hint">
                {t('mcpApp.readOnlyHint')}
              </div>
            ) : null}
          </div>
          <div className="mcp-app-view__approval-actions">
            <button
              type="button"
              data-testid="chat-panel-mcp-app-view-approval-deny-btn"
              onClick={() => approval.resolve('deny')}
            >
              {t('mcpApp.deny')}
            </button>
            <button
              type="button"
              data-testid="chat-panel-mcp-app-view-approval-allow-always-btn"
              onClick={() => approval.resolve('always')}
            >
              {t('mcpApp.allowAlways')}
            </button>
            <button
              type="button"
              className="is-primary"
              data-testid="chat-panel-mcp-app-view-approval-allow-btn"
              onClick={() => approval.resolve('once')}
            >
              {t('mcpApp.allow')}
            </button>
          </div>
        </div>
      ) : null}
      {pendingMessage ? (
        <div
          className="mcp-app-view__approval mcp-app-view__message-confirmation"
          role="alertdialog"
          data-testid="chat-panel-mcp-app-view-message-confirmation"
        >
          <div className="mcp-app-view__approval-text" data-testid="chat-panel-mcp-app-view-message-text">
            <strong>{t('mcpApp.messagePrompt', { app: `${app.server} · ${app.tool}` })}</strong>
            <pre>{pendingMessage.text}</pre>
          </div>
          <div className="mcp-app-view__approval-actions">
            <button
              type="button"
              data-testid="chat-panel-mcp-app-view-message-cancel-btn"
              onClick={() => pendingMessage.resolve(false)}
            >
              {t('mcpApp.cancelMessage')}
            </button>
            <button
              type="button"
              className="is-primary"
              data-testid="chat-panel-mcp-app-view-message-send-btn"
              onClick={() => pendingMessage.resolve(true)}
            >
              {t('mcpApp.sendMessage')}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
