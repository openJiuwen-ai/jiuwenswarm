/**
 * Pure MCP Apps document helpers: parse a ui:// resource and build the
 * sandboxed iframe document with its Content-Security-Policy. No I/O, so it
 * can be unit tested under node (tests/mcpAppDocument.test.mjs).
 */

export const MCP_APP_MIME_TYPE = 'text/html;profile=mcp-app';

export interface McpAppCsp {
  connectDomains?: string[];
  resourceDomains?: string[];
  frameDomains?: string[];
  baseUriDomains?: string[];
}

export interface McpAppResource {
  html: string;
  csp?: McpAppCsp;
  permissions?: Record<string, unknown>;
  prefersBorder?: boolean;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function stringList(value: unknown): string[] | undefined {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : undefined;
}

/** Parse a raw ReadResourceResult into the app HTML and its UI metadata. */
export function parseAppResource(result: unknown, uri: string): McpAppResource {
  const rawContents = asRecord(result)?.contents;
  const contents = Array.isArray(rawContents) ? rawContents : [];
  const content = contents.map(asRecord).find((item) => item?.uri === uri) ?? asRecord(contents[0]);
  if (!content) throw new Error(`MCP App resource ${uri} is empty`);
  const mimeType = typeof content.mimeType === 'string' ? content.mimeType : '';
  if (mimeType && !mimeType.startsWith('text/html')) {
    throw new Error(`MCP App resource ${uri} has unsupported type ${mimeType}`);
  }
  let html = typeof content.text === 'string' ? content.text : '';
  if (!html && typeof content.blob === 'string') {
    const bytes = Uint8Array.from(atob(content.blob), (char) => char.charCodeAt(0));
    html = new TextDecoder().decode(bytes);
  }
  if (!html) throw new Error(`MCP App resource ${uri} has no HTML`);
  const ui = asRecord(asRecord(content._meta)?.ui);
  const csp = asRecord(ui?.csp);
  return {
    html,
    csp: csp
      ? {
          connectDomains: stringList(csp.connectDomains),
          resourceDomains: stringList(csp.resourceDomains),
          frameDomains: stringList(csp.frameDomains),
          baseUriDomains: stringList(csp.baseUriDomains),
        }
      : undefined,
    permissions: asRecord(ui?.permissions),
    prefersBorder: typeof ui?.prefersBorder === 'boolean' ? ui.prefersBorder : undefined,
  };
}

// Only plain origins (optionally with a path) and data:/blob: are accepted, so
// a server cannot smuggle keywords such as 'unsafe-eval' or extra directives.
const CSP_SOURCE = /^(https?:\/\/[a-z0-9.*-]+(:\d+)?(\/[^\s;,'"]*)?|data:|blob:)$/i;

function sources(domains: string[] | undefined): string {
  return (domains ?? []).filter((domain) => CSP_SOURCE.test(domain)).join(' ');
}

/** Content-Security-Policy for the app document, per the resource's _meta.ui.csp. */
export function buildAppCsp(csp: McpAppCsp | undefined): string {
  const resources = sources(csp?.resourceDomains);
  const connect = sources(csp?.connectDomains);
  const frames = sources(csp?.frameDomains);
  const bases = sources(csp?.baseUriDomains);
  // Script/worker allowances match the ext-apps reference host (basic-host):
  // bundled and WebGL apps (CesiumJS, Three.js) need eval, WebAssembly and
  // blob: workers. The iframe has an opaque origin and connect-src stays
  // limited to declared domains, so this does not widen network access.
  return [
    "default-src 'none'",
    `script-src 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' blob: data: ${resources}`.trim(),
    `worker-src blob: ${resources}`.trim(),
    `style-src 'unsafe-inline' blob: data: ${resources}`.trim(),
    `img-src data: blob: ${resources}`.trim(),
    `font-src data: blob: ${resources}`.trim(),
    `media-src data: blob: ${resources}`.trim(),
    `connect-src ${connect || "'none'"}`,
    `frame-src ${frames || "'none'"}`,
    `base-uri ${bases || "'none'"}`,
    "form-action 'none'",
    "object-src 'none'",
  ].join('; ');
}

// Forwards CSP violations, uncaught errors and console output to the host as
// MCP logging notifications, so failures inside the sandboxed iframe (whose
// console the host cannot otherwise see) show up in the host console.
function diagnosticsReporter(forwardInfo: boolean): string {
  const levels = forwardInfo
    ? "{error:'error',warn:'warning',log:'info',info:'info'}"
    : "{error:'error',warn:'warning'}";
  return (
    '<script>(function(){' +
    "function send(level,logger,data){try{parent.postMessage({jsonrpc:'2.0',method:'notifications/message'," +
    "params:{level:level,logger:logger,data:data}},'*')}catch(_){}}" +
    'function text(a){try{return a instanceof Error?String(a.stack||a):typeof a==="string"?a:JSON.stringify(a)}' +
    'catch(_){return String(a)}}' +
    "document.addEventListener('securitypolicyviolation',function(e){" +
    "send('warning','mcp-app-csp',{directive:e.effectiveDirective,blockedURI:e.blockedURI})});" +
    "window.addEventListener('error',function(e){send('error','mcp-app-error',text(e.error||e.message))});" +
    "window.addEventListener('unhandledrejection',function(e){send('error','mcp-app-error',text(e.reason))});" +
    `var levels=${levels};` +
    'Object.keys(levels).forEach(function(k){var orig=console[k];console[k]=function(){' +
    "send(levels[k],'mcp-app-console',Array.prototype.map.call(arguments,text).join(' ').slice(0,2000));" +
    'return orig.apply(console,arguments)}});' +
    '})();</script>'
  );
}

export interface BuildAppDocumentOptions {
  /** Also forward console.log/info (dev builds); warnings and errors always are. */
  forwardConsoleInfo?: boolean;
  /**
   * Inline the CSP as a <meta> tag (default). The sandbox proxy sets the CSP
   * as an HTTP header instead, which the written document inherits.
   */
  inlineCsp?: boolean;
}

/** Prepend a CSP <meta> (and diagnostics reporter) before any app script runs. */
export function buildAppDocument(resource: McpAppResource, options: BuildAppDocumentOptions = {}): string {
  const policy = buildAppCsp(resource.csp).replace(/"/g, '&quot;');
  const cspMeta = options.inlineCsp === false
    ? ''
    : `<meta http-equiv="Content-Security-Policy" content="${policy}">`;
  const meta = cspMeta + diagnosticsReporter(Boolean(options.forwardConsoleInfo));
  const html = resource.html;
  const head = /<head[^>]*>/i.exec(html);
  if (head) {
    const at = head.index + head[0].length;
    return `${html.slice(0, at)}${meta}${html.slice(at)}`;
  }
  return `<!doctype html><html><head>${meta}</head><body>${html}</body></html>`;
}
