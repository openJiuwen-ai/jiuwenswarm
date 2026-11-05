/** Optional component-owned state retained across data-frame remounts. */
export interface WebUIComponentState<T> {
  getSnapshot(): T;
  subscribe(listener: () => void): () => void;
  set(value: T): void;
}

export interface WebUIComponentStateStore {
  retain<T>(nodeKey: string, component: string, initialize: () => T): WebUIComponentState<T>;
}

export function createComponentState<T>(initial: T): WebUIComponentState<T> {
  let value = initial;
  const listeners = new Set<() => void>();
  return {
    getSnapshot: () => value,
    subscribe(listener) {
      listeners.add(listener);
      return () => { listeners.delete(listener); };
    },
    set(next) {
      if (Object.is(value, next)) return;
      value = next;
      listeners.forEach((listener) => listener());
    },
  };
}

/** Owned by one mounted instance view, never shared with another consumer. */
export function createComponentStateStore(): WebUIComponentStateStore & {
  prune(nodes: ReadonlyMap<string, string>): void;
} {
  const entries = new Map<string, { component: string; state: unknown }>();
  return {
    retain<T>(nodeKey: string, component: string, initialize: () => T) {
      const entry = entries.get(nodeKey);
      if (entry?.component === component) return entry.state as WebUIComponentState<T>;
      const state = createComponentState(initialize());
      entries.set(nodeKey, { component, state });
      return state;
    },
    prune(nodes) {
      for (const [key, entry] of entries) {
        if (nodes.get(key) !== entry.component) entries.delete(key);
      }
    },
  };
}
