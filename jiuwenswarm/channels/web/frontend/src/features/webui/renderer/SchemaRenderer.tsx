/**
 * 全局渲染器：接收一棵已规范化的 schema 树，按节点 component 查注册表并递归渲染。
 * 缺失适配器时抛错，由实例视图的错误边界把整棵实例树降级为失败态，不做部分展示。
 * 每次合法 replace 通过 renderGeneration 整树重建；组件可显式借助实例状态端口保留自己的交互值。
 */

import { Fragment, useLayoutEffect, useMemo, useRef, type ReactNode } from 'react';
import type { WebUIInteractionResult } from '../contracts/interaction.ts';
import type { WebUINormalizedNode } from '../contracts/normalized.ts';
import type { WebUIAdapterContext, WebUIRendererRegistry, WebUINodeBinder } from './types.ts';
import type { WebUINodeBinding } from '../runtime/types.ts';
import { useContentActivity } from './ContentActivity.tsx';
import { WebUIFormsProvider } from './adapters/webuiForm/WebUIForm.tsx';
import type { WebUIAdapterProps } from './types.ts';

export interface WebUISchemaRendererProps {
  tree: WebUINormalizedNode;
  registry: WebUIRendererRegistry;
  context: WebUIAdapterContext;
  /** 合法 replace 的代次；变化时整棵实例子树重新挂载。 */
  renderGeneration: number | string;
  bindNode?: WebUINodeBinder;
}

export function WebUISchemaRenderer({
  tree,
  registry,
  context,
  renderGeneration,
  bindNode,
}: WebUISchemaRendererProps) {
  return <Fragment key={renderGeneration}><WebUIFormsProvider><RenderNode node={tree} registry={registry} context={context} bindNode={bindNode} /></WebUIFormsProvider></Fragment>;
}

function RenderNode({ node, registry, context, bindNode, ...fieldControl }: {
  node: WebUINormalizedNode;
  registry: WebUIRendererRegistry;
  context: WebUIAdapterContext;
  bindNode?: WebUINodeBinder;
} & NonNullable<WebUIAdapterProps['fieldControl']>): ReactNode {
  const active = useContentActivity();
  const binding = useMemo<{ current: WebUINodeBinding | null }>(() => ({ current: null }),
    [node, active, context.connected]);
  const binder = useRef(bindNode);
  binder.current = bindNode;
  useLayoutEffect(() => {
    if (!active || !context.connected) return;
    const current = binder.current?.(node.key) ?? null;
    binding.current = current;
    return () => { current?.release(); binding.current = null; };
  }, [node, active, context.connected, binding]);
  const Adapter = registry.get(node.component);
  if (!Adapter) {
    // 整棵实例树失败，而不是继续渲染可交互子节点。
    throw new Error(`WebUI renderer adapter is missing for component: ${node.component}`);
  }

  const renderChild = (child: WebUINormalizedNode) => <RenderNode key={child.key} node={child} registry={registry} context={context} bindNode={bindNode} />;
  const children = node.children.map(renderChild);
  const contentGroups = Object.fromEntries(Object.entries(node.contentGroups ?? {}).map(([key, group]) => [key, group.map(renderChild)]));
  const emit = async (event: string, data: Record<string, unknown>): Promise<WebUIInteractionResult> => {
    if (!binding.current) return { status: 'inactive' };
    try { return await binding.current.emit(event, data); }
    catch (error) { return { status: 'failed', error: errorMessage(error) }; }
  };

  return (
    <Adapter key={node.key} node={node} context={context} fieldControl={fieldControl} appearance={context.styles?.[node.component]} emit={emit} contentGroups={contentGroups}>
      {children}
    </Adapter>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
