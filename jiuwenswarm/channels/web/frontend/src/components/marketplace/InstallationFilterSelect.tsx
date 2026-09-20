import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FilterDropdown } from '../SkillPanel/SkillPanelWidgets';

export type InstallationFilter = 'all' | 'installed' | 'uninstalled';

export function matchesInstallation(installed: boolean, filter: InstallationFilter) {
  return filter === 'all' || installed === (filter === 'installed');
}

export function InstallationFilterSelect({
  value,
  onChange,
}: {
  value: InstallationFilter;
  onChange: (value: InstallationFilter) => void;
}) {
  const { i18n } = useTranslation();
  const zh = i18n.language.startsWith('zh');
  const [open, setOpen] = useState(false);
  return (
    <FilterDropdown
      open={open}
      onToggle={setOpen}
      onClose={() => setOpen(false)}
      value={value}
      onChange={(next) => {
        onChange(next);
        setOpen(false);
      }}
      options={[
        { value: 'all', label: zh ? '全部' : 'All' },
        { value: 'installed', label: zh ? '已安装' : 'Installed' },
        { value: 'uninstalled', label: zh ? '未安装' : 'Not installed' },
      ]}
      testId="marketplace-installation-filter"
    />
  );
}
