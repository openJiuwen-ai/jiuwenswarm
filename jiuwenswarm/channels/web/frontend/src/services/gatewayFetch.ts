import { browserClientCapabilities } from '../features/webui/client/browserClient.ts';
import type { ClientCapabilityHost } from '../features/webui/client/createClientCapabilityHost.ts';

/** Explicit Gateway transport; external URLs retain native fetch behavior. */
export function createGatewayFetch(host: ClientCapabilityHost, fetcher: typeof fetch = (...args) => fetch(...args)): typeof fetch {
  return async (input, init) => {
    if (!host.hasDeclarations() || !host.isGateway(input)) return fetcher(input, init);
    host.refresh();
    if (host.isChanging()) throw new DOMException('Request context changed', 'AbortError');
    const epoch = host.getEpoch();
    const contextSignal = host.getSignal();
    const original = input instanceof Request ? input : null;
    const signal = init && 'signal' in init ? init.signal : original?.signal;
    const controller = new AbortController();
    let abortBody: (() => void) | undefined;
    const abort = () => controller.abort();
    contextSignal.addEventListener('abort', abort, { once: true });
    signal?.addEventListener('abort', abort, { once: true });
    const release = () => {
      contextSignal.removeEventListener('abort', abort);
      signal?.removeEventListener('abort', abort);
      if (abortBody) controller.signal.removeEventListener('abort', abortBody);
    };
    if (contextSignal.aborted || signal?.aborted) abort();
    const check = () => {
      if (epoch !== host.getEpoch() || controller.signal.aborted) throw new DOMException('Request context changed', 'AbortError');
    };
    try {
      const originalHeaders = new Headers(init?.headers ?? original?.headers);
      const headers = host.bindHTTP(input, originalHeaders);
      const injected = [...headers].some(([name, value]) => originalHeaders.get(name) !== value);
      const response = await fetcher(input, {
        ...init,
        headers,
        ...(injected ? { redirect: (init?.redirect ?? original?.redirect) === 'manual' ? 'manual' : 'error' } : {}),
        signal: controller.signal,
      });
      check();
      if (!response.body) { release(); return response; }
      const reader = response.body.getReader();
      const body = new ReadableStream<Uint8Array>({
        start(destination) {
          abortBody = () => {
            const error = new DOMException('Request context changed', 'AbortError');
            destination.error(error);
            release();
            void reader.cancel(error).catch(() => undefined);
          };
          controller.signal.addEventListener('abort', abortBody, { once: true });
          if (controller.signal.aborted) abortBody();
        },
        async pull(destination) {
          try {
            check();
            const next = await reader.read();
            check();
            if (next.done) { release(); destination.close(); }
            else destination.enqueue(next.value);
          } catch (error) { release(); destination.error(error); await reader.cancel().catch(() => undefined); }
        },
        async cancel(reason) { release(); await reader.cancel(reason); },
      });
      return new ContextResponse(response, body, check);
    } catch (error) { release(); throw error; }
  };
}

/** Preserve native response metadata while checking delayed body consumption. */
class ContextResponse extends Response {
  constructor(private source: Response, body: ReadableStream<Uint8Array> | null, private check: () => void) {
    super(body, { status: source.status, statusText: source.statusText, headers: source.headers });
  }
  get url() { return this.source.url; }
  get headers() { return this.source.headers; }
  get redirected() { return this.source.redirected; }
  get type() { return this.source.type; }
  private current<T>(result: Promise<T>): Promise<T> { return result.then((value) => { this.check(); return value; }); }
  json(): Promise<unknown> { return this.current(super.json()); }
  text(): Promise<string> { return this.current(super.text()); }
  blob(): Promise<Blob> { return this.current(super.blob()); }
  arrayBuffer(): Promise<ArrayBuffer> { return this.current(super.arrayBuffer()); }
  formData(): Promise<FormData> { return this.current(super.formData()); }
  clone(): Response { return new ContextResponse(this.source, super.clone().body, this.check); }
}

export const gatewayFetch = createGatewayFetch(browserClientCapabilities);
