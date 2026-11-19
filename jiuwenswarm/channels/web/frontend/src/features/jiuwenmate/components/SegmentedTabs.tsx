import type { JiuwenMateTab } from '../types';

interface SegmentedTabsProps<TValue extends string> {
  tabs: readonly JiuwenMateTab<TValue>[];
  value: TValue;
  onChange: (value: TValue) => void;
  label: string;
  variant?: 'pill' | 'underline';
}

export function SegmentedTabs<TValue extends string>({ tabs, value, onChange, label, variant = 'pill' }: SegmentedTabsProps<TValue>) {
  return (
    <div
      className={`jiuwenmate-segmented-tabs${variant === 'underline' ? ' jiuwenmate-segmented-tabs--underline' : ''}`}
      role="tablist"
      aria-label={label}
      data-testid="jiuwenmate-segmented-tabs"
      data-variant={variant}
    >
      {tabs.map((tab) => (
        <button
          key={tab.value}
          type="button"
          role="tab"
          aria-selected={tab.value === value}
          onClick={() => onChange(tab.value)}
          data-testid="jiuwenmate-segmented-tab"
          data-variant={tab.value}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}
