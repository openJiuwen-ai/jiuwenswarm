/**
 * 全局渲染器：接收一棵已规范化的 schema 树，按节点 component 查注册表并递归渲染。
 * 缺失适配器时抛错，由实例视图的错误边界把整棵实例树降级为失败态，不做部分展示。
 * 每次合法 replace 通过 renderGeneration 整树重建，丢弃上一代的节点局部状态。
 */

import { Fragment, type ReactNode } from 'react';
import type { WebUIInteractionResult } from '../contracts/interaction.ts';
import type { WebUINormalizedNode } from '../contracts/normalized.ts';
import type { WebUIAdapterContext, WebUIIntentHandler, WebUIRendererRegistry } from './types.ts';

export interface WebUISchemaRendererProps {
  tree: WebUINormalizedNode;
  registry: WebUIRendererRegistry;
  context: WebUIAdapterContext;
  onIntent: WebUIIntentHandler;
  /** 合法 replace 的代次；变化时整棵实例子树重新挂载。 */
  renderGeneration: number | string;
}

export function WebUISchemaRenderer({
  tree,
  registry,
  context,
  onIntent,
  renderGeneration,
}: WebUISchemaRendererProps) {
  return <Fragment key={renderGeneration}>{renderNode(tree, registry, context, onIntent)}</Fragment>;
}

function renderNode(
  node: WebUINormalizedNode,
  registry: WebUIRendererRegistry,
  context: WebUIAdapterContext,
  onIntent: WebUIIntentHandler,
): ReactNode {
  const Adapter = registry.get(node.component);
  if (!Adapter) {
    // 整棵实例树失败，而不是继续渲染可交互子节点。
    throw new Error(`WebUI renderer adapter is missing for component: ${node.component}`);
  }

  const children = node.children.map((child) => renderNode(child, registry, context, onIntent));
  const action = node.action;
  const dispatch = async (payload?: Record<string, unknown>): Promise<WebUIInteractionResult> => {
    if (!action) {
      return { status: 'failed', error: 'node has no action' };
    }
    try {
      return await onIntent({ nodeKey: node.key, actionId: action.id, args: action.args, payload });
    } catch (error) {
      return { status: 'failed', error: errorMessage(error) };
    }
  };

  return (
    <Adapter key={node.key} node={node} context={context} dispatch={dispatch}>
      {children}
    </Adapter>
  );
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
