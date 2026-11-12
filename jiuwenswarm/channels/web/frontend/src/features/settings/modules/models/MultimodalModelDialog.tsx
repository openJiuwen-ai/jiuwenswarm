import { useCallback, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Form, useForm, useFormState, type FormItem } from '../../../../components/form';
import { Select } from '../../../../components/ui';
import { generateUuidV4 } from '../../../../utils/uuid';
import { SettingsConfirmDialog } from '../../components';
import { useMultimodalSettings } from '../../services/useMultimodalSettings';
import {
  capabilitiesFor, MULTIMODAL_CAPABILITIES, multimodalErrorDetails, multimodalPurpose,
  type MultimodalCapability, type MultimodalConnection, type MultimodalModel, type MultimodalPurpose,
} from '../../services/multimodalSettings';
import { GenerationModelConfigDialog } from '../agent/GenerationModelConfigDialog';
import { MediaModelConfigDialog } from '../agent/MediaModelConfigDialog';
import { ModelConfigSaveCancelled } from '../agent/modelConfigDialogExtension';
import { multimodalErrorMessage, MultimodalReferenceMessage } from './MultimodalSettingsFeedback';
import { syncDisplayNameWithModelName } from './modelAdapters';

type Metadata = { display_name: string; purpose: MultimodalPurpose; capabilities: MultimodalCapability[] };
type Confirmation = { tools: MultimodalCapability[]; resolve: (confirmed: boolean) => void };

export function MultimodalModelDialog({
  model, defaultCapability, onClose,
}: {
  model?: MultimodalModel;
  defaultCapability?: MultimodalCapability;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { store } = useMultimodalSettings();
  const [modelId] = useState(() => model?.model_id ?? `mm_${generateUuidV4()}`);
  const form = useForm<Metadata>({ initialValues: {
    display_name: model?.display_name ?? '',
    purpose: model?.purpose ?? (defaultCapability ? multimodalPurpose(defaultCapability) : 'understanding'),
    capabilities: model?.capabilities ?? (defaultCapability ? [defaultCapability] : []),
  } });
  const { hasUnsavedChanges } = useFormState(form);
  const values = form.getValues();
  const nameSyncedFromModel = useRef(model?.connection.model_name ?? '');
  const connection = useRef<Record<string, unknown>>({ ...model?.connection });
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const onDraftChange = useCallback((updates: Record<string, string>) => {
    const prefix = Object.keys(updates).find((key) => key.endsWith('_api_base'))?.replace(/_api_base$/, '');
    if (!prefix) return;
    const next = Object.fromEntries(Object.entries(updates).map(([key, value]) => [
      key.slice(prefix.length + 1) === 'model' ? 'model_name' : key.slice(prefix.length + 1), value,
    ]));
    connection.current = next;
  }, []);
  const onModelNameCommit = useCallback((modelName: string, previousModelName?: string) => {
    const current = form.getValues().display_name;
    const previous = previousModelName !== undefined && current.trim() === previousModelName.trim()
      ? previousModelName : nameSyncedFromModel.current;
    const synced = syncDisplayNameWithModelName({ alias: current, model_name: modelName }, previous);
    if (synced.alias !== current) form.setFieldValue('display_name', synced.alias);
    if (previousModelName !== undefined || !current.trim() || current.trim() === previous.trim()) {
      nameSyncedFromModel.current = modelName.trim();
    }
    if (previousModelName !== undefined) form.clearValidate('display_name');
    else form.validate('display_name');
  }, [form]);
  // Reuse the connection draft when changing purpose. Each purpose retains its
  // existing editor/validation; capability changes do not reset its form.
  const config = useMemo(() => Object.fromEntries(MULTIMODAL_CAPABILITIES.flatMap((tool) =>
    Object.entries(connection.current).map(([key, value]) => [`${tool}_${key === 'model_name' ? 'model' : key}`, value]),
  )), [values.purpose]);
  const items: FormItem<Metadata>[] = [
    {
      name: 'purpose', label: t('settingsPanel.multimodal.purpose'), component: 'select', required: true,
      options: (['understanding', 'generation'] as const).map((purpose) => ({ value: purpose, label: t(`settingsPanel.multimodal.${purpose}`) })),
      onChange: () => form.setFieldValue('capabilities', []),
    },
    {
      name: 'capabilities', label: t('settingsPanel.multimodal.capabilities'), component: 'custom', required: true,
      helpTips: t('settingsPanel.multimodal.capabilityHint'),
      render: ({ id, value, disabled, error, onBlur }) => (
        <Select id={id} multiple value={Array.isArray(value) ? value : []} disabled={disabled} invalid={Boolean(error)}
          options={capabilitiesFor(values.purpose).map((tool) => ({ value: tool, label: t(`settingsPanel.multimodal.tools.${tool}`) }))}
          placeholder={t('settingsPanel.multimodal.selectCapabilities')}
          onChange={(selected) => form.setFieldValue('capabilities', selected as MultimodalCapability[])}
          onBlur={onBlur} data-testid="settings-multimodal-capabilities-select" />
      ),
    },
    {
      name: 'display_name', label: t('settingsPanel.multimodal.displayName'), component: 'input', required: true,
      placeholder: t('settingsPanel.multimodal.displayNamePlaceholder'),
    },
  ];
  const save = async (updates: Record<string, string>) => {
    const metadata = form.validate();
    if (!metadata.valid) throw new Error(t('settingsPanel.multimodal.completeFields'));
    const prefix = values.purpose === 'understanding' ? 'vision' : slot;
    const read = (key: string) => updates[`${prefix}_${key}`] ?? '';
    const nextConnection: MultimodalConnection = {
      provider: read('provider'), protocol: values.purpose === 'understanding' ? 'openai' : read('protocol'),
      api_base: read('api_base'), api_key: read('api_key'), model_name: read('model'),
      ...(values.purpose === 'understanding' ? {
        endpoint_profile: read('endpoint_profile'), vendor_key: read('vendor_key'), plan: read('plan'),
        context_window_tokens: Number(read('context_window_tokens')),
      } : {}),
    };
    const params = { mode: model ? 'update' : 'create', model: {
      model_id: modelId, ...metadata.values, display_name: metadata.values.display_name.trim(), connection: nextConnection,
    } };
    let result;
    try {
      result = await store.write('models.upsert', params);
    } catch (error) {
      const details = multimodalErrorDetails(error);
      if (details.field_errors) {
        const connectionErrors: Record<string, string> = {};
        const metadataErrors: Partial<Record<keyof Metadata, string>> = {};
        for (const field of details.field_errors) {
          const message = t('settingsPanel.multimodal.completeFields');
          if (field.field.startsWith('connection.')) connectionErrors[field.field.slice('connection.'.length)] = message;
          else if (field.field === 'display_name' || field.field === 'purpose' || field.field === 'capabilities') metadataErrors[field.field] = message;
        }
        setFieldErrors(connectionErrors);
        form.setErrors(metadataErrors);
      }
      if (details.code !== 'MM_CONFIRM_REQUIRED' || !details.affected_tools) {
        throw new Error(multimodalErrorMessage(error, t));
      }
      const confirmed = await new Promise<boolean>((resolve) => setConfirmation({ tools: details.affected_tools!, resolve }));
      setConfirmation(null);
      if (!confirmed) throw new ModelConfigSaveCancelled();
      try {
        result = await store.write('models.upsert', { ...params, confirmed_tools: details.affected_tools });
      } catch (error) {
        throw new Error(multimodalErrorMessage(error, t));
      }
    }
    if (defaultCapability && !metadata.values.capabilities.includes(defaultCapability)) store.announceShortcutChange(defaultCapability);
    return result;
  };
  const slot = values.capabilities.includes('video_gen') && !values.capabilities.includes('visual_gen') ? 'video_gen' : 'visual_gen';
  const renderMetadata = (disabled: boolean, renderFields: readonly (keyof Metadata)[]) => (
    <Form form={form} items={items} renderFields={renderFields} disabled={disabled} optionalText={t('common.optional')}
      showOptional={false} testIdPrefix="settings-multimodal-metadata"
      rules={{
        display_name: [{ validator: (value) => typeof value === 'string' && value.trim().length > 0 && value.trim().length <= 100 ? undefined : t('settingsPanel.multimodal.nameInvalid') }],
        capabilities: [{ validator: (value) => Array.isArray(value) && value.length > 0 ? undefined : t('settingsPanel.multimodal.selectCapabilities') }],
      }} />
  );
  const extension = {
    dirty: hasUnsavedChanges, onDraftChange, onModelNameCommit, fieldErrors,
    render: (disabled: boolean) => (
      <div className="settings-multimodal-model-metadata" data-testid="settings-multimodal-model-dialog" data-variant={model ? 'edit' : 'add'}>
        {renderMetadata(disabled, ['purpose', 'capabilities'])}
      </div>
    ),
    renderDisplayName: (disabled: boolean) => (
      <div className="settings-multimodal-model-display-name" data-testid="settings-multimodal-display-name">
        {renderMetadata(disabled, ['display_name'])}
      </div>
    ),
  };
  return (
    <>
      {values.purpose === 'understanding' ? (
        <MediaModelConfigDialog key="understanding" modality="vision" config={config} enableOnSave={false}
          titleKey={model ? 'settingsPanel.multimodal.edit' : 'settingsPanel.multimodal.add'} extension={extension}
          save={save} onSaved={() => undefined} onClose={onClose} />
      ) : (
        <GenerationModelConfigDialog key="generation" slot={slot} config={config} enableOnSave={false}
          slots={values.capabilities.filter((tool) => tool === 'visual_gen' || tool === 'video_gen')}
          titleKey={model ? 'settingsPanel.multimodal.edit' : 'settingsPanel.multimodal.add'} extension={extension}
          save={save} onClose={onClose} />
      )}
      <SettingsConfirmDialog open={confirmation !== null} title={t('settingsPanel.multimodal.changeTitle')}
        message={<MultimodalReferenceMessage tools={confirmation?.tools ?? []} operation="edit" />}
        confirmLabel={t('settingsPanel.multimodal.confirmChanges')}
        onConfirm={() => confirmation?.resolve(true)} onCancel={() => confirmation?.resolve(false)} />
    </>
  );
}
