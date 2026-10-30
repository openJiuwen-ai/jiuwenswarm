import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import type { WebUIStyleOverrides } from '../renderer/types.ts';
import type { WebUIMountHandle } from '../runtime/types.ts';
import { WebUIInstanceView } from './WebUIInstanceView.tsx';
import { useOptionalWebUIAppContext } from './WebUIProvider.tsx';

export interface WebUIPanelEntry {
  /** Opaque identity including session, slot, package and instance. */
  key: string;
  title: string;
}

const noopSubscribe = (): (() => void) => () => undefined;
const emptySnapshot = () => '';

/** Mount once for discovery; render and bind only the body selected by the host. */
export function useWebUIPanels({ slotId, sessionId, styles }: {
  slotId: string;
  sessionId: string | null;
  styles?: WebUIStyleOverrides;
}) {
  const context = useOptionalWebUIAppContext();
  const runtime = context?.runtime;
  const registry = context?.registry;
  const [handle, setHandle] = useState<WebUIMountHandle | null>(null);

  const store = useMemo(() => {
    if (!runtime || !sessionId) return null;
    return {
      subscribe(listener: () => void) {
        let unsubscribeInstances: (() => void)[] = [];
        const subscribeInstances = () => {
          unsubscribeInstances.forEach((unsubscribe) => unsubscribe());
          unsubscribeInstances = runtime.getScopeSnapshot(sessionId, slotId).instanceKeys
            .map((key) => runtime.subscribeInstance(key, listener));
        };
        const unsubscribeScope = runtime.subscribeScope(sessionId, slotId, () => {
          subscribeInstances();
          listener();
        });
        subscribeInstances();
        return () => {
          unsubscribeScope();
          unsubscribeInstances.forEach((unsubscribe) => unsubscribe());
        };
      },
      getSnapshot: () => JSON.stringify([
        runtime.getScopeVersion(sessionId, slotId),
        ...runtime.getScopeSnapshot(sessionId, slotId).instanceKeys.map((key) => [key, runtime.getInstanceVersion(key)]),
      ]),
    };
  }, [runtime, sessionId, slotId]);

  const version = useSyncExternalStore(
    store?.subscribe ?? noopSubscribe,
    store?.getSnapshot ?? emptySnapshot,
    store?.getSnapshot ?? emptySnapshot,
  );

  useEffect(() => {
    if (!runtime || !sessionId) return;
    const owner = runtime.mountSlot(sessionId, slotId);
    setHandle(owner);
    void owner.notifyReady();
    return () => owner.release();
  }, [runtime, sessionId, slotId]);

  const panels = useMemo<WebUIPanelEntry[]>(() => {
    if (!runtime || !sessionId || !handle || handle.sessionId !== sessionId || handle.slotId !== slotId) return [];
    const scope = runtime.getScopeSnapshot(sessionId, slotId);
    if (!scope.mounted) return [];
    return scope.instanceKeys.flatMap((key) => {
      const frame = runtime.getInstance(key)?.frame;
      const root = frame?.tree;
      // The protocol validates root types; discovery only consumes their shared title contract.
      if (!root || typeof root.props.title !== 'string') return [];
      return [{ key, title: root.props.title }];
    });
  }, [runtime, sessionId, slotId, handle, version]);

  const renderPanel = useCallback((key: string) => {
    if (!runtime || !registry || !handle || !panels.some((panel) => panel.key === key)) return null;
    return (
      <WebUIInstanceView
        key={`${handle.mountLeaseId}:${key}`}
        runtime={runtime}
        registry={registry}
        instanceKey={key}
        mountLeaseId={handle.mountLeaseId}
        styles={styles}
      />
    );
  }, [runtime, registry, handle, panels, styles]);

  return { panels, renderPanel };
}
