/**
 * 内部实例视图：独立订阅自己的实例快照，持有错误边界、显示确认与绑定当前帧的交互接口。
 * 内容更新只通知本实例，不触发其他实例或同范围其他实例视图。
 */

import { useEffect, useLayoutEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react';
import type { WebUINormalizedNode } from '../contracts/normalized.ts';
import { WebUISchemaRenderer } from '../renderer/SchemaRenderer.tsx';
import { createComponentStateStore } from '../renderer/componentState.ts';
import type { WebUIRendererRegistry, WebUIStyleOverrides } from '../renderer/types.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import { WebUIInstanceErrorBoundary } from './WebUIInstanceErrorBoundary.tsx';
import { browserClientCapabilities } from '../client/browserClient.ts';
import { requestPanelOpen } from '../client/panelNavigation.ts';
import { useClientPreference } from '../client/preferences.ts';
import type { WebUINormalizedRenderFrame } from '../contracts/normalized.ts';

export interface WebUIInstanceViewProps {
  runtime: WebUIRuntime;
  registry: WebUIRendererRegistry;
  instanceKey: string;
  mountLeaseId?: number;
  styles?: WebUIStyleOverrides;
  selected?: boolean;
}

export function WebUIInstanceView({
  runtime, registry, instanceKey, mountLeaseId, styles, selected,
}: WebUIInstanceViewProps) {
  useSyncExternalStore(
    (listener) => runtime.subscribeInstance(instanceKey, listener),
    () => runtime.getInstanceVersion(instanceKey),
    () => runtime.getInstanceVersion(instanceKey),
  );
  const instance = runtime.getInstance(instanceKey);
  const frame = instance?.frame;
  const clientFrameActive = useSyncExternalStore(
    browserClientCapabilities.subscribe,
    () => !frame?.activationGeneration || browserClientCapabilities.acceptsFrame({
      target: { package_id: frame.target.packageId }, activation_generation: frame.activationGeneration,
    }),
    () => !frame?.activationGeneration,
  );
  const hasFrame = !!frame;
  const componentState = useMemo(() => createComponentStateStore(),
    [runtime, instanceKey, mountLeaseId, instance?.connected, instance?.lifetimeId, hasFrame]);
  useLayoutEffect(() => {
    const nodes = new Map<string, string>();
    const visit = (node: WebUINormalizedNode) => {
      nodes.set(node.key, node.component);
      node.children.forEach(visit);
      Object.values(node.contentGroups ?? {}).forEach((group) => group.forEach(visit));
    };
    if (frame) visit(frame.tree);
    componentState.prune(nodes);
  }, [frame, componentState]);
  // Each valid frame rebuilds the tree unless the slot keeps it mounted for the instance's lifetime.
  const keepMounted = !!frame && runtime.getCatalog().getSlot(frame.target.slotId)?.rerender === 'keep-mounted';
  const mountKey = keepMounted ? `lifetime:${instance?.lifetimeId ?? ''}` : instance?.localFrameId ?? 0;
  // Display acknowledgements do not rebuild an unchanged component tree.
  const renderedTree = useMemo(() => {
    if (!frame || !instance) return null;
    return (
      <WebUISchemaRenderer
        tree={frame.tree}
        registry={registry}
        context={{
          instanceKey,
          sessionId: frame.target.sessionId,
          packageId: frame.target.packageId,
          connected: instance.connected,
          componentState,
          styles,
          selected,
          openPanel: (key) => {
            if (runtime.getInstance(instanceKey)?.lifetimeId === instance.lifetimeId) {
              requestPanelOpen(runtime, frame.target, key);
            }
          },
        }}
        renderGeneration={mountKey}
        bindNode={(nodeKey) => runtime.bindNode(instanceKey, instance.localFrameId, nodeKey, mountLeaseId)}
      />
    );
  }, [frame, registry, runtime, instanceKey, instance?.connected, instance?.localFrameId, mountLeaseId, componentState, styles,
    selected, mountKey]);
  if (!instance || !clientFrameActive) {
    return null;
  }

  if (!frame) {
    if (!instance.error) {
      return null;
    }
    return (
      <div
        className="webui-instance-error"
        data-testid="webui-instance-error"
        data-variant={instance.error.code}
        role="alert"
      >
        {instance.error.message || instance.error.code}
      </div>
    );
  }

  const revision = frame.revision;
  const generation = instance.localFrameId;
  const renderFailure = instance.renderFailure?.revision === revision ? instance.renderFailure.message : null;

  return (
    <WebUIInstanceErrorBoundary
      key={mountKey}
      resetKey={generation}
      fallback={
        renderFailure ? (
          <div
            className="webui-instance-error"
            data-testid="webui-instance-error"
            data-variant="render_failed"
            role="alert"
          >
            {renderFailure}
          </div>
        ) : null
      }
      onError={(error) => runtime.reportRenderFailure(instanceKey, revision, error.message, instance.localFrameId)}
    >
      <WebUIVisibleInstance frame={frame}>
        {renderedTree}
        <WebUIRenderConfirmation
          revision={generation}
          onDisplayed={() => runtime.reportDisplayed(instanceKey, revision, instance.localFrameId)}
        />
      </WebUIVisibleInstance>
    </WebUIInstanceErrorBoundary>
  );
}

function WebUIVisibleInstance({ frame, children }: { frame: WebUINormalizedRenderFrame; children: ReactNode }) {
  const visible = useClientPreference(frame.target.packageId, frame.visibleWhen);
  return visible ? children : null;
}

/** 在 React 提交后确认当前帧已显示；渲染失败时不会执行到这里。 */
function WebUIRenderConfirmation({ revision, onDisplayed }: { revision: number | string; onDisplayed: () => void }): ReactNode {
  useEffect(() => {
    onDisplayed();
  }, [revision]);
  return null;
}
