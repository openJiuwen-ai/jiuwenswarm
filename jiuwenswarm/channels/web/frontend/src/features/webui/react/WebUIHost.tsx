/**
 * 槽位宿主：订阅所属挂载区的实例列表，每个实例交给独立的内部实例视图。
 * 实例增删通过范围订阅处理，内容更新由实例视图自己订阅；更新 A 不执行 B 的适配器。
 */

import { useEffect, useMemo, useState, useSyncExternalStore } from 'react';
import type { WebUIRendererRegistry } from '../renderer/types.ts';
import type { WebUIMountHandle, WebUIRuntime } from '../runtime/types.ts';
import { WebUIInstanceView } from './WebUIInstanceView.tsx';
import { useOptionalWebUIAppContext } from './WebUIProvider.tsx';

const noopSubscribe = (): (() => void) => () => undefined;
const noopVersion = (): number => 0;

export interface WebUIHostProps {
  slotId: string;
  sessionId: string | null;
  /** 消息级槽位使用：只消费该 instanceKey 的实例，并以其身份上报 ready。 */
  instanceKey?: string;
  packageId?: string;
  className?: string;
  /** 测试或特殊宿主的注入点；生产环境由 WebUIProvider 提供。 */
  runtime?: WebUIRuntime;
  registry?: WebUIRendererRegistry;
}

interface WebUIHostScopeStore {
  subscribe: (listener: () => void) => () => void;
  getVersion: () => number;
}

export function WebUIHost({
  slotId,
  sessionId,
  instanceKey,
  packageId,
  className,
  runtime: runtimeOverride,
  registry: registryOverride,
}: WebUIHostProps) {
  const context = useOptionalWebUIAppContext();
  const runtime = runtimeOverride ?? context?.runtime;
  const registry = registryOverride ?? context?.registry;
  const [mountHandle, setMountHandle] = useState<WebUIMountHandle | null>(null);

  const scopeStore = useMemo<WebUIHostScopeStore | null>(() => {
    if (!runtime || !sessionId) {
      return null;
    }
    return {
      subscribe: (listener: () => void) => runtime.subscribeScope(sessionId, slotId, listener),
      getVersion: () => runtime.getScopeVersion(sessionId, slotId),
    };
  }, [runtime, sessionId, slotId]);

  const version = useSyncExternalStore(
    scopeStore ? scopeStore.subscribe : noopSubscribe,
    scopeStore ? scopeStore.getVersion : noopVersion,
    scopeStore ? scopeStore.getVersion : noopVersion,
  );

  useEffect(() => {
    if (!runtime || !sessionId) {
      return undefined;
    }
    const handle = runtime.mountSlot(sessionId, slotId, { instanceKey, packageId });
    setMountHandle(handle);
    void handle.notifyReady();
    return () => handle.release();
  }, [runtime, sessionId, slotId, instanceKey, packageId]);

  const scope = useMemo(
    () => (runtime && sessionId ? runtime.getScopeSnapshot(sessionId, slotId) : null),
    [runtime, sessionId, slotId, version],
  );

  if (!runtime || !registry || !sessionId || !scope || !scope.mounted) {
    return null;
  }

  let instanceKeys = scope.instanceKeys.filter((key) => {
    const target = runtime.getInstance(key)?.target;
    return target && (instanceKey === undefined || target.instanceKey === instanceKey)
      && (packageId === undefined || target.packageId === packageId);
  });
  if (runtime.getCatalog().getSlot(slotId)?.display === 'single') {
    instanceKeys = [...instanceKeys].sort((left, right) =>
      (runtime.getInstance(right)?.localFrameId ?? 0) - (runtime.getInstance(left)?.localFrameId ?? 0),
    ).slice(0, 1);
  }

  if (instanceKeys.length === 0) {
    if (instanceKey !== undefined) {
      return null;
    }
    return (
      <div
        className={`webui-host webui-host-waiting ${className ?? ''}`.trim()}
        data-testid="webui-host-waiting"
        aria-busy="true"
      />
    );
  }

  return (
    <div
      className={`webui-host flex flex-col gap-2 ${className ?? ''}`.trim()}
      data-testid="webui-host"
      data-variant={slotId}
      data-connected={scope.connected ? 'true' : 'false'}
    >
      {instanceKeys.map((key) => (
        <div
          key={key}
          className="webui-host-instance"
          data-testid="webui-host-instance"
          data-variant={key}
        >
          <WebUIInstanceView
            key={mountHandle?.mountLeaseId}
            runtime={runtime}
            registry={registry}
            instanceKey={key}
            mountLeaseId={mountHandle?.mountLeaseId}
          />
        </div>
      ))}
    </div>
  );
}
