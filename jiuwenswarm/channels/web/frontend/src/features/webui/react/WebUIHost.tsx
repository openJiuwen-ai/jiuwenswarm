/**
 * 槽位宿主：订阅所属挂载区的实例列表，每个实例交给独立的内部实例视图。
 * 实例增删通过范围订阅处理，内容更新由实例视图自己订阅；更新 A 不执行 B 的适配器。
 */

import { useEffect, useMemo, useState, useSyncExternalStore, type CSSProperties } from 'react';
import {
  preferenceVersion,
  readInstanceVisibility,
  subscribePreferences,
} from '../client/preferences.ts';
import type { WebUIRendererRegistry, WebUIStyleOverrides } from '../renderer/types.ts';
import type { WebUIMountHandle, WebUIMountOptions, WebUIRuntime } from '../runtime/types.ts';
import type { WebUINormalizedTarget } from '../contracts/normalized.ts';
import { WebUIInstanceView } from './WebUIInstanceView.tsx';
import { useOptionalWebUIAppContext } from './WebUIProvider.tsx';
import { useWebUISlotContext } from './WebUISlotContext.ts';

const noopSubscribe = (): (() => void) => () => undefined;
const noopVersion = (): number => 0;

export interface WebUIHostProps {
  slotId: string;
  sessionId?: string | null;
  scope?: 'session' | 'application';
  /** 消息级槽位使用：只消费该 instanceKey 的实例，并以其身份上报 ready。 */
  instanceKey?: string;
  packageId?: string;
  mountOptions?: WebUIMountOptions;
  layout?: 'row' | 'column';
  onActivate?: (target: WebUINormalizedTarget) => void;
  className?: string;
  style?: CSSProperties;
  instanceClassName?: string;
  /** Component styles supplied by the mounting UI, separate from plugin schema props. */
  styles?: WebUIStyleOverrides;
  selectedTarget?: WebUINormalizedTarget | null;
  /**
   * 新帧到达时保留已挂载的组件，只更新属性。默认每个合法帧整树重建，使 defaultValue 等非受控属性生效；
   * 只给组件自行按身份管理状态的宿主使用，例如应用背景的 MediaSurface 按 src 保留正在运行的页面（Gateway S_07）。
   */
  keepMountedAcrossFrames?: boolean;
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
  sessionId: sessionOverride,
  scope: scopeOverride,
  instanceKey,
  packageId,
  mountOptions,
  layout = 'column',
  onActivate,
  className,
  style,
  instanceClassName,
  styles,
  selectedTarget,
  keepMountedAcrossFrames,
  runtime: runtimeOverride,
  registry: registryOverride,
}: WebUIHostProps) {
  const context = useOptionalWebUIAppContext();
  const viewContext = useWebUISlotContext();
  const runtime = runtimeOverride ?? context?.runtime;
  const registry = registryOverride ?? context?.registry;
  const resolvedScope =
    scopeOverride ?? mountOptions?.scope ?? runtime?.getCatalog().getSlot(slotId)?.scope ?? 'session';
  const isApplicationSlot = resolvedScope === 'application';
  const sessionId = sessionOverride === undefined ? (isApplicationSlot ? '' : viewContext.sessionId) : sessionOverride;
  const hasTarget = isApplicationSlot ? sessionId === '' : !!sessionId;
  const [mountHandle, setMountHandle] = useState<WebUIMountHandle | null>(null);
  useSyncExternalStore(subscribePreferences, preferenceVersion, preferenceVersion);

  const scopeStore = useMemo<WebUIHostScopeStore | null>(() => {
    if (!runtime || !hasTarget || sessionId === null) {
      return null;
    }
    return {
      subscribe: (listener: () => void) => runtime.subscribeScope(sessionId, slotId, listener),
      getVersion: () => runtime.getScopeVersion(sessionId, slotId),
    };
  }, [runtime, sessionId, slotId, hasTarget]);

  const version = useSyncExternalStore(
    scopeStore ? scopeStore.subscribe : noopSubscribe,
    scopeStore ? scopeStore.getVersion : noopVersion,
    scopeStore ? scopeStore.getVersion : noopVersion,
  );

  useEffect(() => {
    if (!runtime || !hasTarget || sessionId === null) {
      return undefined;
    }
    const handle = runtime.mountSlot(sessionId, slotId, {
      ...mountOptions,
      instanceKey,
      packageId,
      scope: resolvedScope,
    });
    setMountHandle(handle);
    void handle.notifyReady();
    return () => handle.release();
  }, [runtime, sessionId, slotId, instanceKey, packageId, hasTarget, mountOptions, resolvedScope]);

  const scope = useMemo(
    () => (runtime && hasTarget && sessionId !== null ? runtime.getScopeSnapshot(sessionId, slotId) : null),
    [runtime, sessionId, slotId, version, hasTarget],
  );

  if (!runtime || !registry || !hasTarget || !scope || !scope.mounted) {
    return null;
  }

  let instanceKeys = scope.instanceKeys.filter((key) => {
    const instance = runtime.getInstance(key);
    const target = instance?.target;
    if (instance?.frame) {
      try {
        if (!readInstanceVisibility(instance.frame)) return false;
      } catch {
        /* Let the instance error boundary present a storage read failure. */
      }
    }
    return target && (instanceKey === undefined || target.instanceKey === instanceKey)
      && (packageId === undefined || target.packageId === packageId);
  });
  const slotRule = runtime.getCatalog().getSlot(slotId);
  if (slotRule?.display === 'single') {
    instanceKeys = [...instanceKeys].sort((left, right) =>
      (runtime.getInstance(right)?.localFrameId ?? 0) - (runtime.getInstance(left)?.localFrameId ?? 0),
    ).slice(0, 1);
  } else if (slotRule?.maxVisible !== undefined && instanceKeys.length > slotRule.maxVisible) {
    // list 槽位超出 maxVisible 时保留先挂载的实例，后到的贡献不挤占输入区。
    instanceKeys = instanceKeys.slice(0, slotRule.maxVisible);
  }

  if (instanceKeys.length === 0) {
    return null;
  }

  return (
    <div
      className={`webui-host flex ${layout === 'row' ? 'flex-row items-center' : 'flex-col'} gap-2 ${className ?? ''}`.trim()}
      style={style}
      data-testid="webui-host"
      data-variant={slotId}
      data-connected={scope.connected ? 'true' : 'false'}
    >
      {instanceKeys.map((key) => (
        <div
          key={key}
          className={`webui-host-instance ${instanceClassName ?? ''}`.trim()}
          data-testid="webui-host-instance"
          data-variant={key}
          onClick={onActivate ? (event) => {
            if (event.defaultPrevented) return;
            const control = event.target instanceof Element ? event.target.closest('button, [role="button"]') : null;
            if (!control || control.hasAttribute('disabled') || control.getAttribute('aria-disabled') === 'true') return;
            const target = runtime.getInstance(key)?.target;
            if (target) onActivate(target);
          } : undefined}
        >
          <WebUIInstanceView
            key={mountHandle?.mountLeaseId}
            runtime={runtime}
            registry={registry}
            instanceKey={key}
            mountLeaseId={mountHandle?.mountLeaseId}
            styles={styles}
            keepMountedAcrossFrames={keepMountedAcrossFrames}
            selected={!!selectedTarget && selectedTarget.sessionId === sessionId
              && selectedTarget.packageId === runtime.getInstance(key)?.target.packageId
              && selectedTarget.instanceKey === runtime.getInstance(key)?.target.instanceKey}
          />
        </div>
      ))}
    </div>
  );
}
