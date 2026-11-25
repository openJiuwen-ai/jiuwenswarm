import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { PageHeader } from '../ui';
import { AppMarketPanel, type AppMarketEditHandler } from './index';

/**
 * 应用市场独立页（侧边栏一级入口的宿主）。列表、启停/编辑/删除操作和内嵌浏览窗都由
 * AppMarketPanel 自带；列表页保留「应用」页头与副标题，进入详情或运行中的应用页面时
 * 收起页头，避免和内容自带的标题重复。
 */
export function AppMarketPage({ onEditApp }: { onEditApp?: AppMarketEditHandler }) {
  const { t } = useTranslation();
  const [detailOpen, setDetailOpen] = useState(false);
  const [applicationOpen, setApplicationOpen] = useState(false);
  return (
    <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden" data-testid="app-market-page">
      {!detailOpen && !applicationOpen && (
        <div className="page-shell flex-none mb-8">
          <PageHeader
            title={t('appMarket.title')}
            subtitle={t('appMarket.subtitle')}
            titleTestId="app-market-page-title"
            subtitleTestId="app-market-page-subtitle"
          />
        </div>
      )}
      <AppMarketPanel
        onEdit={onEditApp}
        onDetailChange={setDetailOpen}
        onApplicationOpenChange={setApplicationOpen}
      />
    </div>
  );
}
