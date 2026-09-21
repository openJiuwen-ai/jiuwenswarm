import { Boxes, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { applicationPluginSettingsComponent } from './ApplicationPluginOutlet';
import type { ApplicationPluginContribution } from './types';
import { Button, EntityHeader, PageHeader, Tag } from '../components/ui';
import { IconAvatar } from '../components/ConnectorMarket/Buttons';
import BackIcon from '../assets/work-mode/arrow-left.svg?react';

// 连接器市场（ConnectorMarketPanel）的内嵌子页，布局与市场其他子页同款：detail-back 返回键 +
// page-shell 固定页头 + page-scroll 滚动列；卡片视觉复用 ui/EntityHeader（variant="card"）与
// 插件/MCP 详情页能力卡片同体系，不自带样式文件（原 applicationPlugins.css 的面板样式已删除，
// 仅保留 ApplicationPluginOutlet 用的 .application-plugin-frame）。
export function ApplicationPluginsPanel({
  plugins,
  loading,
  error,
  onRefresh,
  onBack,
}: {
  plugins: ApplicationPluginContribution[];
  loading: boolean;
  error: string;
  onRefresh: () => Promise<void>;
  onBack?: () => void;
}) {
  const { t } = useTranslation();
  const uniquePlugins = plugins.filter(
    (plugin, index) => plugins.findIndex((candidate) => candidate.plugin_id === plugin.plugin_id) === index,
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="application-plugins-panel">
      {onBack && (
        <button type="button" onClick={onBack} className="detail-back" data-testid="application-plugins-panel-back">
          <BackIcon aria-hidden="true" />
          {t('applicationPlugins.backToExtensions')}
        </button>
      )}

      <div className="page-shell flex-none">
        <PageHeader
          title={t('applicationPlugins.title')}
          subtitle={t('applicationPlugins.description')}
          titleTestId="application-plugins-panel-title"
          subtitleTestId="application-plugins-panel-subtitle"
        >
          <Button
            icon={<RefreshCw size={14} />}
            loading={loading}
            onClick={() => void onRefresh()}
            data-testid="application-plugins-panel-refresh"
          >
            {t('applicationPlugins.refresh')}
          </Button>
        </PageHeader>
      </div>

      <div className="page-scroll min-h-0 flex-1 overflow-y-auto">
        {error && (
          <div
            role="alert"
            className="flex items-center gap-1.5 rounded-lg bg-[color:var(--color-feedback-danger-banner)] px-3 py-2 text-[13px] text-text-muted"
            data-testid="application-plugins-panel-error"
          >
            {error}
          </div>
        )}

        {uniquePlugins.length === 0 && (
          <div
            className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border py-16 text-[13px] text-text-muted"
            data-testid="application-plugins-panel-empty"
          >
            <Boxes size={20} aria-hidden />
            <span>{loading ? t('common.loading') : t('applicationPlugins.empty')}</span>
          </div>
        )}

        <div className="flex flex-col gap-4" data-testid="application-plugins-panel-list">
          {uniquePlugins.map((plugin) => {
            const Settings = applicationPluginSettingsComponent(plugin.plugin_id);
            const enabled = plugin.enabled !== false;
            return (
              <article
                key={plugin.plugin_id}
                className="rounded-2xl border border-border bg-card p-6"
                data-testid="application-plugins-panel-item"
                data-variant={plugin.plugin_id}
              >
                <EntityHeader
                  variant="card"
                  testId="application-plugins-panel-item-header"
                  avatar={<IconAvatar icon={<Boxes size={21} />} />}
                  title={plugin.title_i18n_key ? t(plugin.title_i18n_key, plugin.title) : plugin.title}
                  tags={[
                    { label: plugin.plugin_id, testId: 'application-plugins-panel-item-id' },
                    { label: `v${plugin.plugin_version}`, testId: 'application-plugins-panel-item-version' },
                  ]}
                  actions={
                    <Tag
                      variant={enabled ? 'success' : 'neutral'}
                      data-testid="application-plugins-panel-item-status"
                      data-variant={enabled ? 'enabled' : 'disabled'}
                    >
                      {enabled ? t('applicationPlugins.enabled') : t('applicationPlugins.disabled')}
                    </Tag>
                  }
                />
                <div className="mt-4">
                  {Settings ? (
                    <Settings contribution={plugin} onManifestChanged={() => void onRefresh()} />
                  ) : (
                    <p className="text-[13px] text-text-muted" data-testid="application-plugins-panel-item-no-settings">
                      {t('applicationPlugins.noSettings')}
                    </p>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      </div>
    </div>
  );
}
