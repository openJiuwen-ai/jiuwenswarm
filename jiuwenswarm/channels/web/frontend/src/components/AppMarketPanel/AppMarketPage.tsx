import { useTranslation } from 'react-i18next';
import { PageHeader } from '../ui';
import { AppMarketPanel, type AppMarketEditHandler } from './index';

/**
 * 应用市场独立页（侧边栏一级入口的宿主）。应用市场此前是扩展页里的一个 tab，
 * 提升为一级导航后由本组件提供页头；列表、启停/编辑/删除操作和内嵌浏览窗都由
 * AppMarketPanel 自带，这里不重复实现。页头标题复用 appMarket.title（原 tab
 * 直接用 nav.appMarket，title 一直空置，正好用在这里）。
 */
export function AppMarketPage({ onEditApp }: { onEditApp?: AppMarketEditHandler }) {
  const { t } = useTranslation();
  return (
    <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden" data-testid="app-market-page">
      <div className="page-shell flex-none">
        <PageHeader
          title={t('appMarket.title')}
          titleTestId="app-market-page-title"
        />
      </div>
      <AppMarketPanel onEdit={onEditApp} />
    </div>
  );
}
