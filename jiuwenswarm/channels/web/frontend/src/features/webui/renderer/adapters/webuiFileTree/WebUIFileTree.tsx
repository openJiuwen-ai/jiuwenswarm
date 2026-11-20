import { useMemo } from 'react';
import type { WebUIAdapterProps } from '../../types.ts';
import { createComponentState } from '../../componentState.ts';
import { useContentActivity } from '../../ContentActivity.tsx';
import { FileTree, type FileTreeProps, type FileTreeSelection } from './FileTree.tsx';

export function WebUIFileTree({ node, context, emit, appearance }: WebUIAdapterProps) {
  const active = useContentActivity();
  const p = node.props as unknown as Omit<FileTreeProps, 'selection' | 'viewport' | 'onSelect' | 'onExpand'>;
  const state = useMemo(() => {
    const initialize = () => ({
      selection: createComponentState<FileTreeSelection>({
        selectedKey: p.selectedKey ?? null, expandedKeys: p.expandedKeys ?? [],
      }),
      viewport: { scrollTop: 0, width: 240 },
    });
    return context.componentState?.retain(node.key, node.component, initialize).getSnapshot() ?? initialize();
  }, [context.componentState, node.key, node.component]);
  return <FileTree {...p} selection={state.selection} viewport={state.viewport}
    disabled={p.disabled || !context.connected || !active}
    className={appearance?.className} style={appearance?.style}
    onSelect={(key) => emit('ui.select', { key })} onExpand={(data) => emit('ui.change', data)} />;
}
