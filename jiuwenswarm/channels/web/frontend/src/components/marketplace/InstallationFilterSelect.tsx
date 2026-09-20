import { useTranslation } from 'react-i18next';
import SimpleSelect from '../CronPanel/SimpleSelect';
export type InstallationFilter = 'all' | 'installed' | 'uninstalled';
export function matchesInstallation(installed: boolean, filter: InstallationFilter) {
  return filter === 'all' || installed === (filter === 'installed');
}
export function InstallationFilterSelect({ value, onChange }: { value: InstallationFilter; onChange: (value: InstallationFilter) => void }) {
 const { i18n } = useTranslation();
 const zh = i18n.language.startsWith('zh');
 return <div className="flex items-center gap-2 whitespace-nowrap text-sm text-text" data-testid="marketplace-installation-filter"><span>{zh ? '安装状态' : 'Installation status'}</span><SimpleSelect value={value} onChange={v => onChange(v as InstallationFilter)} options={[{value:'all', label:zh?'全部':'All'},{value:'installed',label:zh?'已安装':'Installed'},{value:'uninstalled',label:zh?'未安装':'Not installed'}]} /></div>;
}
