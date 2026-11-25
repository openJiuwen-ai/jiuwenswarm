import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import type { WebUIStyleOverrides } from '../renderer/types.ts';
import type { WebUIMountHandle } from '../runtime/types.ts';
import { WebUIInstanceView } from './WebUIInstanceView.tsx';
import { useOptionalWebUIAppContext, type WebUIAppContextValue } from './WebUIProvider.tsx';
import { useTranslation } from 'react-i18next';
import type { LocalizedText } from '../catalog/components/localizedText.ts';
import { resolveLocalizedText } from '../renderer/useLocalizedText.ts';
import { panelNavigation } from '../client/panelNavigation.ts';
import { preferenceVersion, readInstanceVisibility, subscribePreferences } from '../client/preferences.ts';

export interface WebUIPanelEntry {
  /** Opaque identity including session, slot, package and instance. */
  key: string;
  /** Opaque identity of the contribution, stable when another conversation mounts the same instance. */
  panelId: string;
  title: string;
  /** Optional whitelisted icon name from the root Panel / Iframe `icon` prop. */
  icon?: string;
}

const noopSubscribe = (): (() => void) => () => undefined;
const emptySnapshot = () => '';
const noOpenRequest = () => null;
const noPanels: readonly WebUIPanelEntry[] = Object.freeze([]);

interface ScopeDiscovery {
  visible: WebUIPanelEntry[];
  hidden: WebUIPanelEntry[];
}

type WebUIRuntime = NonNullable<WebUIAppContextValue['runtime']>;
type ScopeSnapshot = ReturnType<WebUIRuntime['getScopeSnapshot']>;

/**
 * Split the scope instances into visible panels and panels hidden only by the instance-visibility
 * client preference. Hidden panels are still mounted contributions: a preference round-trip must
 * restore their tabs, so the tab host needs them as a separate list.
 */
function discoverScopePanels(
  runtime: Pick<WebUIRuntime, 'getInstance'>,
  scope: Pick<ScopeSnapshot, 'instanceKeys'>,
  scopeLanguage: string,
): ScopeDiscovery {
  const visible: WebUIPanelEntry[] = [];
  const hidden: WebUIPanelEntry[] = [];
  for (const key of scope.instanceKeys) {
    const instance = runtime.getInstance(key);
    const frame = instance?.frame;
    const root = frame?.tree;
    // The protocol validates root types; discovery only consumes their shared title/icon contract.
    if (!instance || !frame || !root || root.props.title === undefined) continue;
    let preferenceVisible = true;
    try {
      preferenceVisible = readInstanceVisibility(frame);
    } catch {
      /* The instance error boundary presents storage read failures. */
    }
    const { packageId, instanceKey } = instance.target;
    const icon = typeof root.props.icon === 'string' ? root.props.icon : undefined;
    const entry: WebUIPanelEntry = {
      key,
      panelId: `${packageId}|${instanceKey}`,
      title: resolveLocalizedText(root.props.title as LocalizedText, scopeLanguage) ?? '',
      icon,
    };
    if (preferenceVisible) visible.push(entry);
    else hidden.push(entry);
  }
  return { visible, hidden };
}

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

  const discovery = useMemo(() => {
    if (!runtime || !sessionId || !handle || handle.sessionId !== sessionId || handle.slotId !== slotId) {
      return { visible: noPanels, hidden: noPanels };
    }
    const scope = runtime.getScopeSnapshot(sessionId, slotId);
    if (!scope.mounted) return { visible: noPanels, hidden: noPanels };
    return discoverScopePanels(runtime, scope, language);
    // `preferences` re-renders the host when a client preference changes so this memo re-reads
    // `readInstanceVisibility`, which reads `window.localStorage` directly.
  }, [runtime, sessionId, slotId, handle, version, language, preferences]);

  const panels = discovery.visible;
  const hiddenPanels = discovery.hidden;

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

  return { panels, hiddenPanels, renderPanel, openRequest, consumeOpenRequest };
}
