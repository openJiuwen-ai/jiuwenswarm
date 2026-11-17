import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { settingsActionIcons } from '../../../../assets/settings';
import { Button, Loading, Tag } from '../../../../components/ui';
import { SettingsConfirmDialog, SettingsSection } from '../../components';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useMultimodalSettings } from '../../services/useMultimodalSettings';
import { multimodalErrorDetails, type MultimodalCapability, type MultimodalModel, type PublicMultimodalModel } from '../../services/multimodalSettings';
import { MultimodalModelDialog } from './MultimodalModelDialog';
import { multimodalErrorMessage, MultimodalReferenceMessage, MultimodalSettingsFeedback } from './MultimodalSettingsFeedback';

type Deletion = { model: PublicMultimodalModel; tools: MultimodalCapability[] };

export function MultimodalModelsSettings({ disabled = false }: { disabled?: boolean }) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const { snapshot, saving, loading, store } = useMultimodalSettings();
  const [dialog, setDialog] = useState<{ model?: MultimodalModel } | null>(null);
  const [opening, setOpening] = useState(false);
  const [deletion, setDeletion] = useState<Deletion | null>(null);
  const [error, setError] = useState('');
  const blocked = disabled || !isConnected || saving || loading || opening || !snapshot;
  const edit = async (modelId: string) => {
    setOpening(true);
    setError('');
    try {
      const result = await store.getModel(modelId);
      setDialog({ model: result.model });
    } catch (error) {
      setError(multimodalErrorMessage(error, t));
      void store.load(true);
    } finally {
      setOpening(false);
    }
  };
  const remove = async () => {
    if (!deletion) return;
    setError('');
    try {
      await store.write('models.delete', { model_id: deletion.model.model_id, confirmed_tools: deletion.tools });
      setDeletion(null);
    } catch (error) {
      setError(multimodalErrorMessage(error, t));
      const details = multimodalErrorDetails(error);
      if (details.code === 'MM_CONFIRM_REQUIRED' && details.affected_tools) {
        setDeletion({ ...deletion, tools: details.affected_tools });
        void store.load(true);
      }
    }
  };
  return <>
    <MultimodalSettingsFeedback />
    <SettingsSection separatedRows title={t('settingsPanel.multimodal.tab')} action={
      <Button variant="primary" disabled={blocked} onClick={() => setDialog({})}
        data-testid="settings-multimodal-add-btn">{t('settingsPanel.models.addModel')}</Button>
    }>
      {loading && !snapshot ? <Loading /> : null}
      {error && !deletion ? <div className="settings-page__error" role="alert" data-testid="settings-multimodal-list-error">{error}</div> : null}
      {snapshot && snapshot.models.length === 0 ? <div className="settings-models__empty" data-testid="settings-multimodal-empty">
        <p data-testid="settings-multimodal-empty-title">{t('settingsPanel.multimodal.empty')}</p>
        <p data-testid="settings-multimodal-empty-description">{t('settingsPanel.multimodal.emptyDescription')}</p>
      </div> : null}
      <div className="settings-models__list" data-testid="settings-multimodal-model-list" aria-busy={saving || undefined}>
        {snapshot?.models.map((model) => <article key={model.model_id} className="settings-model-card settings-model-card--no-logo"
          data-testid="settings-multimodal-model-card" data-variant={model.model_id}>
          <div className="settings-model-card__copy">
            <div className="settings-model-card__title-row">
              <h3 data-testid="settings-multimodal-model-name">{model.display_name}</h3>
              <Tag variant="neutral" data-testid="settings-multimodal-model-purpose">{t(`settingsPanel.multimodal.${model.purpose}`)}</Tag>
            </div>
            <p data-testid="settings-multimodal-model-metadata">{[model.provider, model.model_name].filter(Boolean).join(' · ')}</p>
            <p data-testid="settings-multimodal-model-capabilities">{model.capabilities.map((tool) => t(`settingsPanel.multimodal.tools.${tool}`)).join(t('settingsPanel.multimodal.separator'))}</p>
          </div>
          <div className="settings-model-card__actions">
            <Button icon={<settingsActionIcons.edit aria-hidden />} disabled={blocked} aria-label={t('common.modify')}
              onClick={() => void edit(model.model_id)} data-testid="settings-multimodal-edit-btn" />
            <Button icon={<settingsActionIcons.delete aria-hidden />} disabled={blocked} aria-label={t('common.delete')}
              onClick={() => { setError(''); setDeletion({ model, tools: model.references }); }}
              data-testid="settings-multimodal-delete-btn" />
          </div>
        </article>)}
      </div>
    </SettingsSection>
    {dialog ? <MultimodalModelDialog {...dialog} onClose={() => setDialog(null)} /> : null}
    <SettingsConfirmDialog open={deletion !== null} title={t('settingsPanel.multimodal.deleteTitle')}
      message={<MultimodalReferenceMessage tools={deletion?.tools ?? []} operation="delete" />}
      confirming={saving} error={error || undefined} confirmVariant="danger" confirmLabel={t('settingsPanel.multimodal.confirmDelete')}
      onConfirm={() => void remove()} onCancel={() => { if (!saving) { setDeletion(null); setError(''); } }} />
  </>;
}
