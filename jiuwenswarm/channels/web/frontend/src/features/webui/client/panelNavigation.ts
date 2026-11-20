import { webUIInstanceKey, type WebUINormalizedTarget } from '../contracts/normalized.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import { readInstanceVisibility } from './preferences.ts';

export interface PanelOpenRequest {
  id: number;
  sessionId: string;
  key: string;
}

function createNavigation() {
  let sequence = 0;
  let request: PanelOpenRequest | null = null;
  const listeners = new Set<() => void>();
  const changed = () => listeners.forEach((listener) => listener());
  return {
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getSnapshot: () => request,
    open(sessionId: string, key: string) {
      request = { id: ++sequence, sessionId, key };
      changed();
    },
    consume(id: number) {
      if (request?.id === id) {
        request = null;
        changed();
      }
    },
  };
}

const stores = new WeakMap<WebUIRuntime, ReturnType<typeof createNavigation>>();

export function panelNavigation(runtime: WebUIRuntime) {
  let store = stores.get(runtime);
  if (!store) {
    store = createNavigation();
    stores.set(runtime, store);
  }
  return store;
}

/** The caller supplies a trusted source target; the plugin can only name its own instance. */
export function requestPanelOpen(runtime: WebUIRuntime, source: WebUINormalizedTarget, instanceKey: string): boolean {
  if (
    !source.sessionId ||
    source.scope === 'application' ||
    !runtime.getScopeSnapshot(source.sessionId, source.slotId).mounted
  )
    return false;
  const sourceInstance = runtime.getInstance(webUIInstanceKey(source));
  const target = {
    sessionId: source.sessionId,
    slotId: 'conversation.panel.right',
    packageId: source.packageId,
    instanceKey,
  };
  const key = webUIInstanceKey(target);
  const instance = runtime.getInstance(key);
  if (
    !sourceInstance?.connected ||
    !sourceInstance.frame ||
    !instance?.connected ||
    !instance.frame ||
    !runtime.getScopeSnapshot(target.sessionId, target.slotId).mounted ||
    !['Panel', 'Iframe'].includes(instance.frame.tree.component)
  )
    return false;
  if (!readInstanceVisibility(sourceInstance.frame) || !readInstanceVisibility(instance.frame)) return false;
  panelNavigation(runtime).open(target.sessionId, key);
  return true;
}
