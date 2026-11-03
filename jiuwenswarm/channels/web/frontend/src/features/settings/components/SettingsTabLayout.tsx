import { useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { HelpTips, Tabs } from '../../../components/ui';
import type { I18nKey } from '../registry/types';

export type SettingsTabDefinition<TabId extends string = string> = {
  id: TabId;
  labelKey: I18nKey;
  helpKey?: I18nKey;
};

type SettingsTabLayoutProps<TabId extends string> = {
  tabs: readonly SettingsTabDefinition<TabId>[];
  defaultTab: TabId;
  ariaLabelKey: I18nKey;
  className?: string;
  testIdPrefix: string;
  renderPanel: (activeTab: TabId) => ReactNode;
};

/** Shared settings-page tabs. Module definitions own the tab catalog; pages only render panel content. */
export function SettingsTabLayout<TabId extends string>({
  tabs,
  defaultTab,
  ariaLabelKey,
  className,
  testIdPrefix,
  renderPanel,
}: SettingsTabLayoutProps<TabId>) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<TabId>(defaultTab);

  return (
    <>
      <Tabs
        role="tablist"
        ariaLabel={t(ariaLabelKey)}
        bordered
        className={className}
        wrapperTestId={`${testIdPrefix}-tabs`}
        itemTestId={`${testIdPrefix}-tab`}
        value={activeTab}
        onChange={setActiveTab}
        items={tabs.map((tab) => ({
          value: tab.id,
          label: t(tab.labelKey),
          suffix: tab.helpKey ? <HelpTips content={t(tab.helpKey)} size="large" /> : undefined,
        }))}
      />
      {renderPanel(activeTab)}
    </>
  );
}
