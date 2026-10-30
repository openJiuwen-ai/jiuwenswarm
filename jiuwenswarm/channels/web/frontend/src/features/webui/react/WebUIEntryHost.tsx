import { useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { WebUIContentActivity } from '../renderer/ContentActivity.tsx';
import type { WebUIRendererRegistry } from '../renderer/types.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import type { WebUIDispatchReceipt } from '../transport/types.ts';
import { WebUIHost } from './WebUIHost.tsx';
import { useOptionalWebUIAppContext } from './WebUIProvider.tsx';
import './WebUIEntryHost.css';

const ENTRY_SLOT_ID = 'application.entry';
const ENTRY_SESSION_ID = '';
const ENTRY_VISIT_STORAGE_KEY = 'workswarm.webui.entry.visited';
const noopSubscribe = (): (() => void) => () => undefined;
const noopVersion = (): number => 0;

export interface WebUIEntryHostProps {
  children?: ReactNode;
  runtime?: WebUIRuntime;
  registry?: WebUIRendererRegistry;
}

function getStorage(): Storage | undefined {
  try {
    return globalThis.localStorage ?? (globalThis as { window?: Window }).window?.localStorage;
  } catch {
    return undefined;
  }
}

function isFirstVisit(): boolean {
  try {
    return getStorage()?.getItem(ENTRY_VISIT_STORAGE_KEY) !== '1';
  } catch {
    return false;
  }
}

function markVisited(): void {
  try {
    getStorage()?.setItem(ENTRY_VISIT_STORAGE_KEY, '1');
  } catch {
    // Storage failures must not block normal workspace entry.
  }
}

export function WebUIEntryHost({
  children,
  runtime: runtimeOverride,
  registry: registryOverride,
}: WebUIEntryHostProps) {
  const { t } = useTranslation();
  const context = useOptionalWebUIAppContext();
  const runtime = runtimeOverride ?? context?.runtime;
  const registry = registryOverride ?? context?.registry;
  const [dismissed, setDismissed] = useState(false);
  const firstVisit = useMemo(() => isFirstVisit(), []);
  const mountOptions = useMemo(() => ({
    readyOnce: true,
    readyPayload: { reason: 'first_visit' },
    onReadySettled: (receipt: WebUIDispatchReceipt | null) => {
      if (firstVisit && receipt?.accepted) {
        markVisited();
      }
    },
  }), [firstVisit]);

  const scopeStore = useMemo(() => {
    if (!runtime) {
      return null;
    }
    return {
      subscribe: (listener: () => void) => runtime.subscribeScope(ENTRY_SESSION_ID, ENTRY_SLOT_ID, listener),
      getVersion: () => runtime.getScopeVersion(ENTRY_SESSION_ID, ENTRY_SLOT_ID),
    };
  }, [runtime]);

  const version = useSyncExternalStore(
    scopeStore ? scopeStore.subscribe : noopSubscribe,
    scopeStore ? scopeStore.getVersion : noopVersion,
    scopeStore ? scopeStore.getVersion : noopVersion,
  );

  const scope = useMemo(
    () => (runtime ? runtime.getScopeSnapshot(ENTRY_SESSION_ID, ENTRY_SLOT_ID) : null),
    [runtime, version],
  );
  const instances = useMemo(
    () => (runtime && scope ? scope.instanceKeys.map((key) => runtime.getInstance(key)).filter(Boolean) : []),
    [runtime, scope, version],
  );
  const latestInstance = useMemo(
    () => [...instances].sort((left, right) => (right?.localFrameId ?? 0) - (left?.localFrameId ?? 0))[0],
    [instances],
  );
  const hasFailedInstance = Boolean(
    latestInstance && (latestInstance.state === 'error' || Boolean(latestInstance.error || latestInstance.renderFailure)),
  );
  const hasActiveTree = Boolean(
    latestInstance && latestInstance.state === 'active' && latestInstance.frame && !latestInstance.error && !latestInstance.renderFailure,
  );

  useEffect(() => {
    if (scope?.closed || hasFailedInstance) {
      setDismissed(true);
    }
  }, [scope?.closed, hasFailedInstance]);

  const entryMounted = Boolean(runtime && registry && !dismissed && !scope?.closed && !hasFailedInstance);
  const entryOpen = Boolean(entryMounted && hasActiveTree);

  return (
    <>
      {entryMounted && (
        <dialog
          open={entryOpen}
          aria-modal={entryOpen ? 'true' : undefined}
          aria-label={t('webui.entryLabel', 'Welcome')}
          className="webui-entry-host"
          data-testid="webui-entry-host"
          onCancel={(event) => event.preventDefault()}
        >
          <WebUIHost
            slotId={ENTRY_SLOT_ID}
            sessionId={ENTRY_SESSION_ID}
            mountOptions={mountOptions}
            runtime={runtime}
            registry={registry}
            className="webui-entry-host__body"
            instanceClassName="webui-entry-host__instance"
          />
        </dialog>
      )}
      {children === undefined ? null : (
        <WebUIContentActivity active={!entryOpen}>{children}</WebUIContentActivity>
      )}
    </>
  );
}
