import { useTranslation } from 'react-i18next';
import { DesignerAssetsPanel } from './DesignerAssetsPanel';

export function DesignerAssetsSidebar() {
  const { t } = useTranslation();

  return (
    <aside
      className="designer-assets-sidebar"
      aria-label={t('designer.sidebar.canvas')}
      data-testid="designer-assets-sidebar"
    >
      <div className="designer-assets-sidebar__header">
        <h2 className="designer-assets-sidebar__title" data-testid="designer-assets-sidebar-title">
          {t('designer.sidebar.canvas')}
        </h2>
      </div>
      <DesignerAssetsPanel />
    </aside>
  );
}
