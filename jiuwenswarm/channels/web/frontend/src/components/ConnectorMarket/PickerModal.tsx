import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Search, Plus, Check } from 'lucide-react';
import { FormDrawer, PageCard } from '../ui';

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

  const visible = items.filter((item) => {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return item.name.toLowerCase().includes(q) || item.description.toLowerCase().includes(q);
  });

  function toggle(id: string) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  return (
    <FormDrawer
      title={title}
      onClose={onCancel}
      onConfirm={() => onConfirm(selected)}
      testId="connector-market-picker"
      width={900}
    >
      <div className="relative mb-4 shrink-0">
        <Search
          size={14}
          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[color:var(--color-text-placeholder)]"
        />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t('connectorMarket.common.search')}
          className="h-8 w-full rounded-lg border border-border bg-bg pl-8 pr-3 text-[12px] leading-[18px] text-text outline-none focus:border-border-hover"
          data-testid="connector-market-picker-search"
        />
      </div>

      {loading ? (
        <div className="py-10 text-center text-[13px] text-text-muted" data-testid="connector-market-picker-loading">
          {t('common.loading')}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4" data-testid="connector-market-picker-list">
          {visible.map((item) => {
            const checked = selected.includes(item.id);
            return (
              <PageCard
                key={item.id}
                testId="connector-market-picker-item"
                variant={item.id}
                avatar={{ name: item.name, iconUrl: item.iconUrl }}
                title={item.name}
                description={item.description}
                selected={checked}
                onClick={() => toggle(item.id)}
                actionSlot={
                  checked ? (
                    <Check size={14} className="shrink-0 text-[color:var(--color-chat-accent)]" />
                  ) : (
                    <Plus size={14} className="shrink-0 text-text-muted" />
                  )
                }
              />
            );
          })}
          {visible.length === 0 && (
            <div
              className="col-span-full py-10 text-center text-[13px] text-text-muted"
              data-testid="connector-market-picker-empty"
            >
              {t('connectorMarket.common.noResult')}
            </div>
          )}
        </div>
      )}
    </FormDrawer>
  );
}
