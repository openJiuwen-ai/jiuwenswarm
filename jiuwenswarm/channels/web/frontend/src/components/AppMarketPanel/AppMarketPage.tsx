import { AppMarketPanel, type AppMarketEditHandler } from './index';

export function AppMarketPage({ onEditApp }: { onEditApp?: AppMarketEditHandler }) {
  return (
    <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden" data-testid="app-market-page">
      <AppMarketPanel onEdit={onEditApp} showPageHeader />
    </div>
  );
}
