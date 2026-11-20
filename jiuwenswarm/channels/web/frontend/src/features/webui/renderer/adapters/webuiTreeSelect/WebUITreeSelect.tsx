import { useEffect, useMemo, useState } from 'react';
import { TreeSelect, type TreeSelectProps } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

type Selection = string | string[] | null;
interface TreeOption {
  key?: string; value: string; title: string; children?: TreeOption[];
  disabled?: boolean; disableCheckbox?: boolean; selectable?: boolean; checkable?: boolean;
}
type CheckedStrategy = 'SHOW_CHILD' | 'SHOW_PARENT' | 'SHOW_ALL';
const checkedStrategies: Record<CheckedStrategy, CheckedStrategy> = {
  SHOW_CHILD: TreeSelect.SHOW_CHILD,
  SHOW_PARENT: TreeSelect.SHOW_PARENT,
  SHOW_ALL: TreeSelect.SHOW_ALL,
};

/** Derive node keys from values so the plugin tree keeps stable identities without restating them. */
function prepareTree(items: TreeOption[]): TreeOption[] {
  return items.map((item) => ({
    ...item,
    key: item.key ?? item.value,
    children: item.children ? prepareTree(item.children) : undefined,
  }));
}

export function WebUITreeSelect({ node, context, emit, appearance }: WebUIAdapterProps) {
  const p = node.props as Pick<TreeSelectProps<Selection>, 'value' | 'defaultValue' | 'disabled' | 'placeholder'
    | 'allowClear' | 'multiple' | 'treeCheckable' | 'showSearch' | 'treeDefaultExpandAll' | 'treeDefaultExpandedKeys'
    | 'size' | 'status' | 'variant' | 'maxTagCount'> & {
      treeData: TreeOption[]; showCheckedStrategy: CheckedStrategy; ariaLabel?: string;
    };
  const treeData = useMemo(() => prepareTree(p.treeData), [p.treeData]);
  const [value, setValue] = useState<Selection>(
    p.value !== undefined ? p.value : p.defaultValue ?? (p.multiple ? [] : null),
  );
  useEffect(() => {
    if (p.value !== undefined) setValue(p.value);
  }, [p.value]);
  const disabled = p.disabled || !context.connected;
  return <TreeSelect<Selection>
    className={appearance?.className} style={appearance?.style}
    data-testid="webui-tree-select" aria-label={p.ariaLabel} treeData={treeData} value={value} disabled={disabled}
    placeholder={p.placeholder} allowClear={p.allowClear} multiple={p.multiple} treeCheckable={p.treeCheckable}
    showCheckedStrategy={checkedStrategies[p.showCheckedStrategy ?? 'SHOW_CHILD']} showSearch={p.showSearch}
    treeNodeFilterProp="title" treeDefaultExpandAll={p.treeDefaultExpandAll}
    treeDefaultExpandedKeys={p.treeDefaultExpandedKeys} size={p.size} status={p.status} variant={p.variant}
    maxTagCount={p.maxTagCount}
    onSelect={(next) => {
      if (!disabled && node.events['ui.select']) void emit('ui.select', { value: next });
    }}
    onChange={(next) => {
      if (disabled) return;
      const normalized = next ?? (p.multiple ? [] : null);
      setValue(normalized);
      if (node.events['ui.change']) void emit('ui.change', { value: normalized });
    }}
    onClear={() => {
      if (!disabled && node.events['ui.clear']) void emit('ui.clear', {});
    }}
  />;
}
