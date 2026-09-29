import { type ReactNode } from 'react';
import { Search } from 'lucide-react';
import { FormDrawer } from '../FormDrawer/FormDrawer';
import { Tabs } from '../Tabs/Tabs';

export interface PickerDrawerTab {
  value: string;
  label: ReactNode;
}

export interface PickerDrawerTabsConfig<T extends string = string> {
  value: T;
  onChange: (value: T) => void;
  ariaLabel: string;
  items: PickerDrawerTab[];
}

export interface PickerDrawerProps<T extends string = string> {
  title: ReactNode;
  testId: string;
  onClose: () => void;
  onConfirm: () => void;
  confirmDisabled?: boolean;
  footerLeading?: ReactNode;
  search: string;
  onSearchChange: (value: string) => void;
  searchPlaceholder: string;
  tabs?: PickerDrawerTabsConfig<T>;
  children: ReactNode;
}

/** 选择器抽屉外壳：FormDrawer（flush body）+ 搜索框 + 可选页签。
    testid 派生规则：搜索框 `${testId}-search`、页签容器 `${testId}-tabs`、
    页签项 `${testId}-tab-${value}`。列表区请用 PickerListRegion 作为 children。 */
export function PickerDrawer<T extends string = string>({
  title,
  testId,
  onClose,
  onConfirm,
  confirmDisabled,
  footerLeading,
  search,
  onSearchChange,
  searchPlaceholder,
  tabs,
  children,
}: PickerDrawerProps<T>) {
  return (
    <FormDrawer
      title={title}
      onClose={onClose}
      onConfirm={onConfirm}
      confirmDisabled={confirmDisabled}
      testId={testId}
      width={900}
      bodyClassName="form-drawer__body--flush"
      footerLeading={footerLeading}
    >
      <div className="relative mx-6 mb-4 shrink-0">
        <Search
          size={14}
          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[color:var(--color-text-placeholder)]"
          aria-hidden="true"
        />
        <input
          value={search}
          onChange={(event) => onSearchChange(event.target.value)}
          placeholder={searchPlaceholder}
          className="h-8 w-full rounded-lg border border-border bg-bg pl-8 pr-3 text-[12px] leading-[18px] text-text outline-none focus:border-border-hover"
          data-testid={`${testId}-search`}
        />
      </div>
      {tabs ? (
        <Tabs
          className="mb-4 px-6"
          role="tablist"
          ariaLabel={tabs.ariaLabel}
          items={tabs.items.map((item) => ({ ...item, testId: `${testId}-tab-${item.value}` }))}
          value={tabs.value}
          onChange={tabs.onChange}
          wrapperTestId={`${testId}-tabs`}
        />
      ) : null}
      {children}
    </FormDrawer>
  );
}
