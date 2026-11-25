import type {
  ApplicationSurfaceConfig, PresentationGeometry, PresentationNativeApi, PresentationWindowApi,
  PresentationWorkArea,
} from './protocol.ts';

export interface OverlaySnapshot {
  generation: string;
  title: string;
  bounds: PresentationWorkArea;
  ready: boolean;
  frameApi: PresentationWindowApi;
}

/** Limit the surface rectangle to the application viewport, independently of its iframe viewport. */
export function fitOverlayBounds(bounds: PresentationWorkArea, area: PresentationWorkArea): PresentationWorkArea {
  const width = Math.min(bounds.width, area.width);
  const height = Math.min(bounds.height, area.height);
  return {
    x: Math.max(area.x, Math.min(bounds.x, area.x + area.width - width)),
    y: Math.max(area.y, Math.min(bounds.y, area.y + area.height - height)),
    width, height,
  };
}

/** Own one sandboxed in-app surface and adapt the existing presentation transport. */
export function createPresentationOverlay(viewport: () => PresentationWorkArea, readyTimeoutMs = 15000) {
  type Update = Parameters<Parameters<PresentationWindowApi['onUpdate']>[0]>[0];
  type Result = Parameters<Parameters<PresentationWindowApi['onResult']>[0]>[0];
  type Entry = {
    config: ApplicationSurfaceConfig;
    generation: string;
    bounds: PresentationWorkArea;
    initialWorkArea: PresentationWorkArea;
    ready: boolean;
    frameApi: PresentationWindowApi;
    updates: Set<(payload: Update) => void>;
    results: Set<(result: Result) => void>;
    resolve: (value: { generation: string }) => void;
    reject: (error: Error) => void;
    timer: ReturnType<typeof setTimeout>;
  };
  let nextGeneration = 0;
  let current: Entry | null = null;
  let snapshot: OverlaySnapshot | null = null;
  const observers = new Set<() => void>();
  const requests = new Set<Parameters<PresentationNativeApi['onRequest']>[0]>();
  const closed = new Set<Parameters<PresentationNativeApi['onClosed']>[0]>();
  const publish = () => {
    snapshot = current ? { generation: current.generation, title: current.config.title,
      bounds: current.bounds, ready: current.ready, frameApi: current.frameApi } : null;
    observers.forEach((listener) => listener());
  };
  const geometry = (entry: Entry): PresentationGeometry => ({
    mode: 'overlay', bounds: entry.bounds, workArea: viewport(),
  });
  const assertCurrent = (entry: Entry) => {
    if (current !== entry) throw new Error('STALE_REQUEST');
  };
  const close = (reason: string) => {
    const previous = current;
    if (!previous) return false;
    current = null;
    clearTimeout(previous.timer);
    previous.reject(new Error(reason));
    previous.updates.clear();
    previous.results.clear();
    publish();
    closed.forEach((listener) => listener({ generation: previous.generation, key: previous.config.key, reason }));
    return true;
  };
  const place = (entry: Entry, bounds: PresentationWorkArea) => {
    assertCurrent(entry);
    entry.bounds = fitOverlayBounds(bounds, viewport());
    publish();
    entry.updates.forEach((listener) => listener({ host: geometry(entry) }));
    return true;
  };
  const unsupported = async (): Promise<never> => { throw new Error('UNSUPPORTED_ACTION'); };
  const api: PresentationNativeApi = {
    open(config) {
      close('replaced');
      const area = viewport();
      let resolve!: Entry['resolve'];
      let reject!: Entry['reject'];
      const ready = new Promise<{ generation: string }>((done, fail) => { resolve = done; reject = fail; });
      const entry: Entry = {
        config, generation: `overlay-${++nextGeneration}`,
        bounds: fitOverlayBounds({ x: area.x + area.width - config.width - 24,
          y: area.y + area.height - config.height - 72, width: config.width, height: config.height }, area),
        initialWorkArea: area, ready: false, updates: new Set(), results: new Set(), resolve, reject,
        timer: setTimeout(() => { if (current === entry) close('ready_timeout'); }, readyTimeoutMs),
        frameApi: {
          async bootstrap() {
            assertCurrent(entry);
            return { ...entry.config, generation: entry.generation,
              initialWorkArea: entry.initialWorkArea, host: geometry(entry) };
          },
          async ready(options) {
            assertCurrent(entry);
            if (options !== undefined) throw new Error('UNSUPPORTED_ACTION');
            if (!entry.ready) {
              clearTimeout(entry.timer);
              entry.ready = true;
              publish();
              entry.resolve({ generation: entry.generation });
            }
            return true;
          },
          startTransition: unsupported, finishTransition: unsupported,
          onTransition: () => () => {},
          async failed() { if (current === entry) close('render_failed'); return true; },
          async request(request) {
            assertCurrent(entry);
            requests.forEach((listener) => listener({ key: config.key, generation: entry.generation, request }));
            return true;
          },
          onUpdate(listener) { entry.updates.add(listener); return () => { entry.updates.delete(listener); }; },
          onResult(listener) { entry.results.add(listener); return () => { entry.results.delete(listener); }; },
        },
      };
      current = entry;
      publish();
      return ready;
    },
    async update(key, payload) {
      if (!current || current.config.key !== key) return false;
      current.config = { ...current.config, ...payload };
      current.updates.forEach((listener) => listener(payload));
      return true;
    },
    async close(key) { return current?.config.key === key ? close('closed') : false; },
    transitionWorkspace: unsupported,
    async respond(generation, result) {
      if (!current || current.generation !== generation) return false;
      current.results.forEach((listener) => listener(result));
      return true;
    },
    async resize(key, width, height) {
      if (!current || current.config.key !== key) return false;
      const bounds = current.bounds;
      return place(current, { x: bounds.x + bounds.width - width, y: bounds.y + bounds.height - height, width, height });
    },
    async move(key, deltaX, deltaY) {
      if (!current || current.config.key !== key) return false;
      return place(current, { ...current.bounds, x: current.bounds.x + deltaX, y: current.bounds.y + deltaY });
    },
    hitTest: unsupported,
    onRequest(listener) { requests.add(listener); return () => { requests.delete(listener); }; },
    onClosed(listener) { closed.add(listener); return () => { closed.delete(listener); }; },
  };
  return {
    api,
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) { observers.add(listener); return () => { observers.delete(listener); }; },
    reflow() { if (current) place(current, current.bounds); },
    close: () => { close('closed'); },
    dispose() { close('shutdown'); observers.clear(); requests.clear(); closed.clear(); },
  };
}
