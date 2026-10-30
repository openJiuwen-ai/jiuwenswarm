/**
 * 测试用适配器：只用于渲染层测试，通过统一 emit 接口触发节点事件。
 */

import { useState } from 'react';
import { createRendererRegistry } from '../renderer/types.ts';
import type { WebUIAdapterProps } from '../renderer/types.ts';

let adapterRenderCount = 0;

/** 供测试断言实例隔离：每渲染一个节点加一。 */
export function demoAdapterRenderCount(): number {
  return adapterRenderCount;
}

export function resetDemoAdapterRenderCount(): void {
  adapterRenderCount = 0;
}

export function DemoComponentAdapter({ node, children, context, emit }: WebUIAdapterProps) {
  adapterRenderCount += 1;
  const [local, setLocal] = useState(0);
  const [lastStatus, setLastStatus] = useState('');
  const title = typeof node.props.title === 'string' ? node.props.title : '';
  const tone = typeof node.props.tone === 'string' ? node.props.tone : 'neutral';

  const send = async (): Promise<void> => {
    const result = await emit('ui.click', {});
    setLastStatus(result.status);
  };

  return (
    <section data-testid="webui-demo" data-variant={tone} data-instance={context.instanceKey}>
      <span data-testid="webui-demo-title">{title}</span>
      <span data-testid="webui-demo-local">{local}</span>
      <button type="button" data-testid="webui-demo-local-button" onClick={() => setLocal((value) => value + 1)}>
        local
      </button>
      {node.children.length > 0 ? <div data-testid="webui-demo-children">{children}</div> : null}
      {node.events['ui.click'] ? (
        <button type="button" data-testid="webui-demo-action" onClick={send}>
          {node.events['ui.click'].id}
        </button>
      ) : null}
      {lastStatus ? (
        <span data-testid="webui-demo-status" data-variant={lastStatus}>
          {lastStatus}
        </span>
      ) : null}
    </section>
  );
}

export const demoRendererRegistry = createRendererRegistry({ demo: DemoComponentAdapter });
