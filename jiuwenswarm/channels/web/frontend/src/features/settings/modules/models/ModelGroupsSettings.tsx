import { useState } from 'react';
import { CheckCircle2, ChevronDown } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ModelEntry, ModelGroup } from '../../../../types';
import { Button, Tag } from '../../../../components/ui';
import { settingsActionIcons, settingsEmptyBoxIllustration } from '../../../../assets/settings';
import { SettingsSection } from '../../components';
import { getGroupDefaultRouteId, getModelGroupDisplayName } from './modelGroupAdapters';

type ModelPresentation = {
  customName: string;
  metadata: string;
  logo?: string;
};

type ModelGroupsSettingsProps = {
  models: ModelEntry[];
  modelGroups: ModelGroup[];
  loading: boolean;
  hasError: boolean;
  saving: boolean;
  actionsDisabled: boolean;
  getModelPresentation: (model: ModelEntry) => ModelPresentation;
  getValidationState: (model: ModelEntry, index: number) => 'testing' | 'success' | undefined;
  onAdd: () => void;
  onEdit: (group: ModelGroup) => void;
  onDelete: (group: ModelGroup) => void;
  onSetDefault: (group: ModelGroup, routeId: string) => Promise<void>;
  onTestModel: (model: ModelEntry, index: number) => void;
  onRemoveRoute: (group: ModelGroup, routeId: string, modelName: string) => void;
};

export function ModelGroupsSettings({
  models,
  modelGroups,
  loading,
  hasError,
  saving,
  actionsDisabled,
  getModelPresentation,
  getValidationState,
  onAdd,
  onEdit,
  onDelete,
  onSetDefault,
  onTestModel,
  onRemoveRoute,
}: ModelGroupsSettingsProps) {
  const { t } = useTranslation();
  const [collapsedGroupIds, setCollapsedGroupIds] = useState<Record<string, true>>({});

  const toggleGroupExpanded = (groupId: string) => {
    setCollapsedGroupIds((current) => {
      if (current[groupId]) {
        const next = { ...current };
        delete next[groupId];
        return next;
      }
      return { ...current, [groupId]: true };
    });
  };

  const renderModelGroupCard = (group: ModelGroup) => {
    const groupId = group.model_group_id ?? getModelGroupDisplayName(group);
    const title = getModelGroupDisplayName(group);
    const expanded = !collapsedGroupIds[groupId];
    const defaultRouteId = getGroupDefaultRouteId(group);
    const headerLabel = t('settingsPanel.models.modelGroupCardHeader', {
      name: title,
      count: group.routes.length,
    });

    return (
      <article
        className="settings-models-group-card"
        key={groupId}
        data-testid="settings-models-group-card"
        data-variant={groupId}
        data-expanded={expanded || undefined}
      >
        <div className="settings-models-group-card__heading">
          <button
            type="button"
            className="settings-models-group-card__toggle"
            aria-expanded={expanded}
            aria-label={t(
              expanded ? 'settingsPanel.models.collapseModelGroup' : 'settingsPanel.models.expandModelGroup',
            )}
            onClick={() => toggleGroupExpanded(groupId)}
            data-testid="settings-models-group-card-toggle"
            data-variant={groupId}
          >
            <span
              className="settings-models-group-card__title"
              title={headerLabel}
              data-testid="settings-models-group-card-title"
              data-variant={groupId}
            >
              <span className="settings-models-group-card__title-label">
                {t('settingsPanel.models.modelGroupLabel')}
              </span>
              <span className="settings-models-group-card__title-name">{title}</span>
              <span className="settings-models-group-card__title-count">
                {t('settingsPanel.models.modelGroupCount', { count: group.routes.length })}
              </span>
            </span>
            {group.is_default === true ? (
              <Tag variant="info" data-testid="settings-models-group-card-default-tag" data-variant={groupId}>
                {t('settingsPanel.models.modelGroupDefault')}
              </Tag>
            ) : null}
            {group.enabled === false ? (
              <Tag variant="neutral" data-testid="settings-models-group-card-disabled-tag" data-variant={groupId}>
                {t('settingsPanel.models.modelGroupDisabled')}
              </Tag>
            ) : null}
            <ChevronDown
              className="settings-models-group-card__chevron"
              aria-hidden
              data-expanded={expanded || undefined}
            />
          </button>
          <div className="settings-models-group-card__heading-actions">
            <Button
              className="settings-models-group-card__edit"
              variant="quiet"
              size="sm"
              icon={<settingsActionIcons.edit aria-hidden />}
              disabled={actionsDisabled || !group.model_group_id}
              onClick={() => onEdit(group)}
              data-testid="settings-models-group-card-edit-btn"
              data-variant={groupId}
            >
              {t('settingsPanel.models.editModelGroup')}
            </Button>
            <Button
              variant="quiet"
              icon={<settingsActionIcons.delete aria-hidden />}
              aria-label={t('common.delete')}
              title={t('common.delete')}
              disabled={actionsDisabled || !group.model_group_id}
              onClick={() => onDelete(group)}
              data-testid="settings-models-group-card-delete-btn"
              data-variant={groupId}
            />
          </div>
        </div>
        {expanded ? (
          <div
            className="settings-models-group-card__routes"
            data-testid="settings-models-group-card-routes"
            data-variant={groupId}
          >
            {group.routes.length === 0 ? (
              <p
                className="settings-models-group-card__empty"
                data-testid="settings-models-group-card-empty"
                data-variant={groupId}
              >
                {t('settingsPanel.models.modelGroupNoRoutes')}
              </p>
            ) : (
              group.routes.map((route) => {
                const modelIndex = models.findIndex((entry) => entry.model_id === route.model_id);
                const model = modelIndex >= 0 ? models[modelIndex] : undefined;
                const validationState = model ? getValidationState(model, modelIndex) : undefined;
                const presentation = model
                  ? getModelPresentation(model)
                  : {
                      customName: route.model_id,
                      metadata: t('settingsPanel.models.missingConfiguredModel'),
                      logo: undefined,
                    };
                const isRouteDefault = Boolean(route.route_id && route.route_id === defaultRouteId);
                const canRemove = group.routes.length > 1;
                const hasStableRouteId = Boolean(route.route_id);

                return (
                  <div
                    className={`settings-models-group-route${presentation.logo ? '' : ' settings-models-group-route--no-logo'}`}
                    key={route.route_id ?? route.model_id}
                    data-testid="settings-models-group-route"
                    data-variant={route.model_id}
                  >
                    {presentation.logo ? (
                      <img className="settings-models-group-route__logo" src={presentation.logo} alt="" aria-hidden />
                    ) : null}
                    <div className="settings-models-group-route__copy">
                      <div className="settings-models-group-route__title-row">
                        <h3
                          title={presentation.customName}
                          data-testid="settings-models-group-route-title"
                          data-variant={route.model_id}
                        >
                          {presentation.customName}
                        </h3>
                        {isRouteDefault ? (
                          <CheckCircle2
                            className="settings-models-group-route__default-icon"
                            aria-hidden
                            data-testid="settings-models-group-route-default-icon"
                            data-variant={route.model_id}
                          />
                        ) : null}
                        {isRouteDefault ? (
                          <span
                            className="settings-models-group-route__default-badge"
                            data-testid="settings-models-group-route-default-badge"
                            data-variant={route.model_id}
                          >
                            {t('settingsPanel.models.modelGroupRouteDefault')}
                          </span>
                        ) : null}
                      </div>
                      <p
                        title={presentation.metadata}
                        data-testid="settings-models-group-route-metadata"
                        data-variant={route.model_id}
                      >
                        {presentation.metadata}
                      </p>
                    </div>
                    <div className="settings-models-group-route__actions">
                      {!isRouteDefault ? (
                        <Button
                          className="settings-models-group-route__set-default"
                          variant="quiet"
                          size="sm"
                          disabled={actionsDisabled || !group.model_group_id || !hasStableRouteId || route.enabled === false}
                          onClick={() => {
                            if (route.route_id) void onSetDefault(group, route.route_id).catch(() => undefined);
                          }}
                          data-testid="settings-models-group-route-set-default-btn"
                          data-variant={route.model_id}
                        >
                          {t('settingsPanel.models.setModelGroupRouteDefault')}
                        </Button>
                      ) : null}
                      <span className="settings-models-group-route__divider" aria-hidden />
                      <Button
                        variant="quiet"
                        icon={<settingsActionIcons.test aria-hidden />}
                        aria-label={t('settingsPanel.models.testConnection')}
                        title={t('settingsPanel.models.testConnection')}
                        loading={validationState === 'testing'}
                        disabled={actionsDisabled || !model}
                        onClick={() => {
                          if (model) onTestModel(model, modelIndex);
                        }}
                        data-testid="settings-models-group-route-test-btn"
                        data-variant={route.model_id}
                      />
                      <Button
                        variant="quiet"
                        icon={<settingsActionIcons.delete aria-hidden />}
                        aria-label={t('settingsPanel.models.removeModelFromGroup')}
                        title={
                          canRemove
                            ? t('settingsPanel.models.removeModelFromGroup')
                            : t('settingsPanel.models.keepOneGroupRoute')
                        }
                        disabled={actionsDisabled || !group.model_group_id || !canRemove || !hasStableRouteId}
                        onClick={() => {
                          if (route.route_id) onRemoveRoute(group, route.route_id, presentation.customName);
                        }}
                        data-testid="settings-models-group-route-remove-btn"
                        data-variant={route.model_id}
                      />
                    </div>
                  </div>
                );
              })
            )}
          </div>
        ) : null}
      </article>
    );
  };

  return (
    <div className="settings-models__groups-pane">
      <SettingsSection
        title={t('settingsPanel.models.modelGroupsManagement')}
        separatedRows
        action={
          <Button
            className="settings-models__groups-add"
            variant="secondary"
            disabled={actionsDisabled}
            onClick={onAdd}
            data-testid="settings-models-groups-add-btn"
          >
            {t('settingsPanel.models.addModelGroup')}
          </Button>
        }
      >
        {!hasError && !loading && modelGroups.length === 0 ? (
          <div className="settings-models__empty" data-testid="settings-models-groups-empty">
            <img src={settingsEmptyBoxIllustration} alt="" aria-hidden />
            <strong>{t('settingsPanel.models.modelGroupsEmpty')}</strong>
            <p>{t('settingsPanel.models.modelGroupsEmptyDescription')}</p>
            <Button
              variant="primary"
              disabled={actionsDisabled}
              onClick={onAdd}
              data-testid="settings-models-groups-empty-add-btn"
            >
              {t('settingsPanel.models.addModelGroup')}
            </Button>
          </div>
        ) : null}
        <div
          className="settings-models__list"
          aria-busy={saving || undefined}
          data-testid="settings-models-groups-list"
        >
          {!hasError && !loading ? modelGroups.map(renderModelGroupCard) : null}
        </div>
      </SettingsSection>
    </div>
  );
}
