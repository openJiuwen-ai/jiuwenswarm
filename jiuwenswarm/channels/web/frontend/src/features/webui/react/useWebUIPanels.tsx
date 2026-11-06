import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import type { WebUIStyleOverrides } from '../renderer/types.ts';
import type { WebUIMountHandle } from '../runtime/types.ts';
import { WebUIInstanceView } from './WebUIInstanceView.tsx';
import { useOptionalWebUIAppContext } from './WebUIProvider.tsx';
import { useTranslation } from 'react-i18next';
import type { LocalizedText } from '../catalog/components/localizedText.ts';
import { resolveLocalizedText } from '../renderer/useLocalizedText.ts';
import { panelNavigation } from '../client/panelNavigation.ts';
import { preferenceVersion, readInstanceVisibility, subscribePreferences } from '../client/preferences.ts';

export interface WebUIPanelEntry {
  /** Opaque identity including session, slot, package and instance. */
  key: string;
  title: string;
}

const noopSubscribe = (): (() => void) => () => undefined;
const emptySnapshot = () => '';
const noOpenRequest = () => null;

/** Mount once for discovery; render and bind only the body selected by the host. */
export function useWebUIPanels({ slotId, sessionId, styles }: {
  slotId: string;
  sessionId: string | null;
  styles?: WebUIStyleOverrides;
}) {
  const context = useOptionalWebUIAppContext();
  const runtime = context?.runtime;
  const registry = context?.registry;
  const { i18n } = useTranslation();
  const language = i18n.resolvedLanguage ?? i18n.language;
  const navigation = useMemo(() => (runtime ? panelNavigation(runtime) : null), [runtime]);
  const openRequest = useSyncExternalStore(
    navigation?.subscribe ?? noopSubscribe,
    navigation?.getSnapshot ?? noOpenRequest,
    noOpenRequest,
  );
  const consumeOpenRequest = useCallback((id: number) => navigation?.consume(id), [navigation]);
  const [handle, setHandle] = useState<WebUIMountHandle | null>(null);
  const preferences = useSyncExternalStore(subscribePreferences, preferenceVersion, preferenceVersion);

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
      if (!frame || !root || root.props.title === undefined) return [];
      try {
        if (!readInstanceVisibility(frame)) return [];
      } catch {
        /* The instance error boundary presents storage read failures. */
      }
      return [{ key, title: resolveLocalizedText(root.props.title as LocalizedText, language) ?? '' }];
    });
  }, [runtime, sessionId, slotId, handle, version, language, preferences]);

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

  return { panels, renderPanel, openRequest, consumeOpenRequest };
}
