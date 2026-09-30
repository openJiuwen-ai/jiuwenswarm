import { useMemo, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import EntityAddIcon from '../../assets/agent-management/add.svg?react';
import EntityRemoveIcon from '../../assets/agent-management/remove.svg?react';
import { PickerDrawer, PickerListRegion, PageCard, SelectedCount } from '../ui';
import type { PickerListStatus } from '../ui';

export interface PickerItem {
  id: string;
  name: string;
  description: string;
  iconUrl?: string;
}

interface ConnectorPickerDrawerTab<S extends string> {
  value: S;
  label: ReactNode;
}

interface ConnectorPickerDrawerProps<T extends PickerItem, S extends string = string> {
  title: ReactNode;
  /** testid 基名：派生 -selected-count/-search/-tabs/-tab-{value}/-item 及列表区各态 */
  testId: string;
  status: PickerListStatus;
  items: T[];
  getItemKey: (item: T) => string;
  initialSelectedIds: string[];
  onClose: () => void;
  onConfirm: (ids: string[]) => void;
  onRetry?: () => void;
  /** 页签配置；提供即渲染页签，页签状态由组件内部管理 */
  tabs?: {
    ariaLabel: string;
    items: ConnectorPickerDrawerTab<S>[];
  };
  /** 页签维度的过滤（返回 false 剔除）；搜索词过滤由组件内置 */
  filterItem?: (item: T, sourceTab: S) => boolean;
  /** 自定义卡片；缺省渲染"点击勾选 + 角标图标"的通用卡片 */
  renderItem?: (item: T, selection: { selected: boolean; toggle: (id: string) => void }) => ReactNode;
  searchPlaceholder?: string;
  loadingMessage?: string;
  errorMessage?: string;
  emptyMessage?: string;
  retryLabel?: string;
  loadMoreLabel?: string;
  /** 描述为空时的兜底文案；缺省 connectorMarket.common.noDescription */
  fallbackDescription?: string;
}

/** 连接器（MCP）选择抽屉：搜索、可选页签、选中草稿、底部计数、三态与分页内聚，
    卡片默认为通用勾选卡，也可经 renderItem 注入（如带安装/连接按钮的专家编辑卡）。
    与 SkillPickerDrawer 共用 PickerDrawer + PickerListRegion 底座。 */
export function ConnectorPickerDrawer<T extends PickerItem, S extends string = string>({
  title,
  testId,
  status,
  items,
  getItemKey,
  initialSelectedIds,
  onClose,
  onConfirm,
  onRetry,
  tabs,
  filterItem,
  renderItem,
  searchPlaceholder,
  loadingMessage,
  errorMessage,
  emptyMessage,
  retryLabel,
  loadMoreLabel,
  fallbackDescription,
}: ConnectorPickerDrawerProps<T, S>) {
  const { t } = useTranslation();
  const [sourceTab, setSourceTab] = useState<S | undefined>(tabs?.items[0]?.value);
  const [query, setQuery] = useState('');
  const [selectedIds, setSelectedIds] = useState<string[]>(initialSelectedIds);

  const visibleItems = useMemo(
    () =>
      items.filter((item) => {
        if (tabs && sourceTab !== undefined && filterItem && !filterItem(item, sourceTab)) return false;
        const q = query.trim().toLocaleLowerCase();
        if (!q) return true;
        return `${item.id} ${item.name} ${item.description}`.toLocaleLowerCase().includes(q);
      }),
    [items, tabs, sourceTab, filterItem, query],
  );

  function toggle(id: string) {
    setSelectedIds((current) => (current.includes(id) ? current.filter((item) => item !== id) : [...current, id]));
  }

  const defaultRenderItem = (item: T, selection: { selected: boolean; toggle: (id: string) => void }) => (
    <PageCard
      testId={`${testId}-item`}
      variant={item.id}
      avatar={{ name: item.name, iconUrl: item.iconUrl }}
      title={item.name}
      description={item.description || fallbackDescription || t('connectorMarket.common.noDescription')}
      selected={selection.selected}
      onClick={() => selection.toggle(item.id)}
      actionSlot={
        selection.selected ? (
          <EntityRemoveIcon className="shrink-0 text-[color:var(--color-chat-accent)]" />
        ) : (
          <EntityAddIcon className="shrink-0 text-text-muted" />
        )
      }
    />
  );
  const renderItemFn = renderItem ?? defaultRenderItem;

  return (
    <PickerDrawer
      title={title}
      onClose={onClose}
      onConfirm={() => onConfirm(selectedIds)}
      testId={testId}
      footerLeading={<SelectedCount count={selectedIds.length} testId={`${testId}-selected-count`} />}
      search={query}
      onSearchChange={setQuery}
      searchPlaceholder={searchPlaceholder ?? t('connectorMarket.common.search')}
      tabs={
        tabs
          ? {
              value: sourceTab as S,
              onChange: setSourceTab,
              ariaLabel: tabs.ariaLabel,
              items: tabs.items,
            }
          : undefined
      }
    >
      <PickerListRegion
        status={status}
        items={visibleItems}
        getItemKey={getItemKey}
        testId={testId}
        loadingMessage={loadingMessage ?? t('common.loading')}
        errorMessage={errorMessage ?? t('connectorMarket.common.loadFailed')}
        emptyMessage={emptyMessage ?? t('connectorMarket.common.noResult')}
        retryLabel={retryLabel ?? t('common.retry')}
        onRetry={onRetry}
        loadMoreLabel={loadMoreLabel ?? t('connectorMarket.loadMore')}
        renderItem={(item) =>
          renderItemFn(item, {
            selected: selectedIds.includes(getItemKey(item)),
            toggle,
          })
        }
      />
    </PickerDrawer>
  );
}
