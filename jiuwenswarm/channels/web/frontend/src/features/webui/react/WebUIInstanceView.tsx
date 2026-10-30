/**
 * 内部实例视图：独立订阅自己的实例快照，持有错误边界、显示确认与绑定当前帧的交互接口。
 * 内容更新只通知本实例，不触发其他实例或同范围其他实例视图。
 * React 重挂载、显示确认与失败反馈都按 localFrameId 区分每次有效渲染，
 * 避免 clear 后以相同 revision 重建时继承旧页面状态或旧错误。
 */

import { useEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react';
import type { WebUIInteractionIntent, WebUIInteractionResult } from '../contracts/interaction.ts';
import { WebUISchemaRenderer } from '../renderer/SchemaRenderer.tsx';
import type { WebUIRendererRegistry } from '../renderer/types.ts';
import type { WebUIRuntime } from '../runtime/types.ts';
import { WebUIInstanceErrorBoundary } from './WebUIInstanceErrorBoundary.tsx';

export interface WebUIInstanceViewProps {
  runtime: WebUIRuntime;
  registry: WebUIRendererRegistry;
  instanceKey: string;
}

export function WebUIInstanceView({ runtime, registry, instanceKey }: WebUIInstanceViewProps) {
  useSyncExternalStore(
    (listener) => runtime.subscribeInstance(instanceKey, listener),
    () => runtime.getInstanceVersion(instanceKey),
    () => runtime.getInstanceVersion(instanceKey),
  );
  const instance = runtime.getInstance(instanceKey);
  const frame = instance?.frame;
  const revision = frame?.revision ?? 0;
  const generation = instance?.localFrameId ?? 0;
  const connected = instance?.connected ?? false;
  const renderFailure =
    instance?.renderFailure?.localFrameId === generation ? instance.renderFailure.message : null;

  // 显示确认等快照更新不重建未变化的组件树，避免重复执行适配器。
  const renderedTree = useMemo(() => {
    if (!frame) {
      return null;
    }
    const context = { instanceKey, connected };
    const onIntent = async (intent: WebUIInteractionIntent): Promise<WebUIInteractionResult> => {
      try {
        return await runtime.dispatchIntent({ ...intent, instanceKey, frameRevision: frame.revision });
      } catch (error) {
        return { status: 'failed', error: error instanceof Error ? error.message : String(error) };
      }
    };
    return (
      <WebUISchemaRenderer
        tree={frame.tree}
        registry={registry}
        context={context}
        onIntent={onIntent}
        renderGeneration={generation}
      />
    );
  }, [runtime, registry, instanceKey, frame, connected, generation]);

  if (!instance) {
    return null;
  }

  if (!frame) {
    if (!instance.error) {
      return null;
    }
    return (
      <div className="webui-instance-error" data-testid="webui-instance-error" data-variant={instance.error.code} role="alert">
        {instance.error.message || instance.error.code}
      </div>
    );
  }

  return (
    <WebUIInstanceErrorBoundary
      key={generation}
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
      onError={(error) => runtime.reportRenderFailure(instanceKey, revision, error.message, generation)}
    >
      {renderedTree}
      <WebUIRenderConfirmation
        generation={generation}
        onDisplayed={() => runtime.reportDisplayed(instanceKey, revision, generation)}
      />
    </WebUIInstanceErrorBoundary>
  );
}

/** 在 React 提交后确认当前帧已显示；渲染失败时不会执行到这里。 */
function WebUIRenderConfirmation({ generation, onDisplayed }: { generation: number; onDisplayed: () => void }): ReactNode {
  useEffect(() => {
    onDisplayed();
  }, [generation]);
  return null;
}
