import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import EntityAddIcon from '../../assets/agent-management/add.svg?react';
import EntityRemoveIcon from '../../assets/agent-management/remove.svg?react';
import { PickerDrawer, PickerListRegion, PageCard, SelectedCount } from '../ui';

export interface PickerItem {
  id: string;
  name: string;
  description: string;
  iconUrl?: string;
}

interface PickerModalProps {
  title: string;
  items: PickerItem[];
  initialSelectedIds: string[];
  loading?: boolean;
  onCancel: () => void;
  onConfirm: (ids: string[]) => void;
}

export function PickerModal({ title, items, initialSelectedIds, loading, onCancel, onConfirm }: PickerModalProps) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string[]>(initialSelectedIds);
  const [query, setQuery] = useState('');

  const visible = useMemo(
    () =>
      items.filter((item) => {
        const q = query.trim().toLowerCase();
        if (!q) return true;
        return item.name.toLowerCase().includes(q) || item.description.toLowerCase().includes(q);
      }),
    [items, query],
  );

  function toggle(id: string) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  return (
    <PickerDrawer
      title={title}
      onClose={onCancel}
      onConfirm={() => onConfirm(selected)}
      testId="connector-market-picker"
      footerLeading={<SelectedCount count={selected.length} testId="connector-market-picker-selected-count" />}
      search={query}
      onSearchChange={setQuery}
      searchPlaceholder={t('connectorMarket.common.search')}
    >
      <PickerListRegion
        status={loading ? 'loading' : 'success'}
        items={visible}
        getItemKey={(item) => item.id}
        loadingMessage={t('common.loading')}
        emptyMessage={t('connectorMarket.common.noResult')}
        loadingTestId="connector-market-picker-loading"
        emptyTestId="connector-market-picker-empty"
        listTestId="connector-market-picker-list"
        renderItem={(item) => (
          <PageCard
            testId="connector-market-picker-item"
            variant={item.id}
            avatar={{ name: item.name, iconUrl: item.iconUrl }}
            title={item.name}
            description={item.description}
            selected={selected.includes(item.id)}
            onClick={() => toggle(item.id)}
            actionSlot={
              selected.includes(item.id) ? (
                <EntityRemoveIcon className="shrink-0 text-[color:var(--color-chat-accent)]" />
              ) : (
                <EntityAddIcon className="shrink-0 text-text-muted" />
              )
            }
          />
        )}
      />
    </PickerDrawer>
  );
}
