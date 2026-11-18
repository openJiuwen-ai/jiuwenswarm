import { useCallback, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Form, useForm, useFormState, type FormItem } from '../../../../components/form';
import { Select } from '../../../../components/ui';
import { generateUuidV4 } from '../../../../utils/uuid';
import { SettingsConfirmDialog } from '../../components';
import { useMultimodalSettings } from '../../services/useMultimodalSettings';
import {
  capabilitiesFor, MULTIMODAL_CAPABILITIES, multimodalErrorDetails, multimodalPurpose,
  type MultimodalCapability, type MultimodalChange, type MultimodalConnection, type MultimodalModel, type MultimodalPurpose,
} from '../../services/multimodalSettings';
import { GenerationModelConfigDialog } from '../agent/GenerationModelConfigDialog';
import { MediaModelConfigDialog } from '../agent/MediaModelConfigDialog';
import { ModelConfigSaveCancelled } from '../agent/modelConfigDialogExtension';
import { multimodalErrorMessage, MultimodalReferenceMessage } from './MultimodalSettingsFeedback';
import { syncDisplayNameWithModelName } from './modelAdapters';

type Metadata = { display_name: string; purpose: MultimodalPurpose; capabilities: MultimodalCapability[] };
type Confirmation = {
  tools: MultimodalCapability[];
  activationTools: MultimodalCapability[];
  resolve: (enabled: boolean | null) => void;
};

export function MultimodalModelDialog({
  model, defaultCapability, enableToolOnSave, onClose,
}: {
  model?: MultimodalModel;
  defaultCapability?: MultimodalCapability;
  enableToolOnSave?: boolean;
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
  // Purpose changes start with an empty connection draft; capability changes
  // retain the current editor and its validation.
  const config = useMemo(() => Object.fromEntries(MULTIMODAL_CAPABILITIES.flatMap((tool) =>
    Object.entries(connection.current).map(([key, value]) => [`${tool}_${key === 'model_name' ? 'model' : key}`, value]),
  )), [values.purpose]);
  const items: FormItem<Metadata>[] = [
    {
      name: 'purpose', label: t('settingsPanel.multimodal.purpose'), component: 'select', required: true,
      options: (['understanding', 'generation'] as const).map((purpose) => ({ value: purpose, label: t(`settingsPanel.multimodal.${purpose}`) })),
      onChange: (purpose) => {
        if (purpose === values.purpose) return;
        connection.current = {};
        nameSyncedFromModel.current = '';
        form.setValues({ capabilities: [], display_name: '' });
        form.clearValidate();
      },
    },
    {
      name: 'capabilities', label: t('settingsPanel.multimodal.capabilities'), component: 'custom', required: true,
      helpTips: t('settingsPanel.multimodal.capabilityHint'),
      render: ({ id, value, disabled, error, onChange, onBlur }) => (
        <Select id={id} multiple value={Array.isArray(value) ? value : []} disabled={disabled} invalid={Boolean(error)}
          options={capabilitiesFor(values.purpose).map((tool) => ({ value: tool, label: t(`settingsPanel.multimodal.tools.${tool}`) }))}
          placeholder={t('settingsPanel.multimodal.selectCapabilities')}
          onChange={(selected) => onChange(selected as MultimodalCapability[])}
          onBlur={onBlur} data-testid="settings-multimodal-capabilities-select" />
      ),
    },
    {
      name: 'display_name', label: t('settingsPanel.multimodal.displayName'), component: 'input', required: true,
      placeholder: t('settingsPanel.multimodal.displayNamePlaceholder'),
    },
  ];
  async function saveWithConfirmation(params: Record<string, unknown>): Promise<MultimodalChange> {
    try {
      return await store.write('models.upsert', params);
    } catch (error) {
      const details = multimodalErrorDetails(error);
      const tools = details.affected_tools ?? [];
      const activationTools = details.activation_tools ?? [];
      if (details.code !== 'MM_CONFIRM_REQUIRED' || (!tools.length && !activationTools.length)) {
        throw new Error(multimodalErrorMessage(error, t));
      }
      const enabled = await new Promise<boolean | null>((resolve) => setConfirmation({ tools, activationTools, resolve }));
      setConfirmation(null);
      if (enabled === null) throw new ModelConfigSaveCancelled();
      return saveWithConfirmation({
        ...params, confirmed_tools: tools, tool_activation: { tools: activationTools, enabled },
      });
    }
  }
  const save = async (updates: Record<string, string>) => {
    const metadata = form.getValues();
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
    const params: Record<string, unknown> = { mode: model ? 'update' : 'create', model: {
      model_id: modelId, ...metadata, display_name: metadata.display_name.trim(), connection: nextConnection,
    } };
    if (
      enableToolOnSave && defaultCapability && metadata.purpose === multimodalPurpose(defaultCapability) &&
      metadata.capabilities.length === 1 && metadata.capabilities[0] === defaultCapability
    ) {
      params.tool_activation = { tools: [defaultCapability], enabled: true };
    }
    const result = await saveWithConfirmation(params);
    if (defaultCapability && !metadata.capabilities.includes(defaultCapability)) store.announceShortcutChange(defaultCapability);
    return result;
  };
  const slot = values.capabilities.includes('video_gen') && !values.capabilities.includes('visual_gen') ? 'video_gen' : 'visual_gen';
  const renderMetadata = (disabled: boolean, renderFields: readonly (keyof Metadata)[]) => (
    <Form form={form} items={items} renderFields={renderFields} disabled={disabled} optionalText={t('common.optional')}
      showOptional={false} testIdPrefix="settings-multimodal-metadata"
      rules={{
        display_name: [
          { validator: (value) => typeof value === 'string' && value.trim().length > 0 && value.trim().length <= 100 ? undefined : t('settingsPanel.multimodal.nameInvalid') },
          { validator: (value) => store.getSnapshot().snapshot?.models.some((candidate) =>
            candidate.model_id !== modelId && candidate.display_name.trim() === value.trim(),
          ) ? t('settingsPanel.models.validation.aliasConflict') : undefined },
        ],
        capabilities: [{ validator: (value) => Array.isArray(value) && value.length > 0 ? undefined : t('settingsPanel.multimodal.selectCapabilities') }],
      }} />
  );
  const extension = {
    dirty: hasUnsavedChanges, onDraftChange, onModelNameCommit,
    validate: () => form.validate().valid,
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
  const confirmsActivation = Boolean(confirmation?.activationTools.length);
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
      <SettingsConfirmDialog
        open={confirmation !== null}
        title={t(confirmsActivation ? 'settingsPanel.multimodal.enableTitle' : 'settingsPanel.multimodal.changeTitle')}
        message={
          <>
            {confirmation && confirmsActivation ? (
              <div data-testid="settings-multimodal-activation-message">
                <p data-testid="settings-multimodal-activation-tools">
                  {t('settingsPanel.multimodal.enableTools', {
                    tools: confirmation.activationTools.map((tool) => t(`settingsPanel.multimodal.tools.${tool}`))
                      .join(t('settingsPanel.multimodal.separator')),
                    model: values.display_name.trim(),
                  })}
                </p>
                <p data-testid="settings-multimodal-activation-rule">{t('settingsPanel.multimodal.keepDisabledRule')}</p>
              </div>
            ) : null}
            {confirmation?.tools.length ? (
              <MultimodalReferenceMessage
                tools={confirmation.tools}
                operation="edit"
              />
            ) : null}
          </>
        }
        confirmLabel={t(confirmsActivation ? 'settingsPanel.multimodal.enableAndUse' : 'settingsPanel.multimodal.confirmChanges')}
        cancelLabel={confirmsActivation ? t('settingsPanel.multimodal.keepDisabled') : undefined}
        onConfirm={() => confirmation?.resolve(true)}
        onCancel={() => confirmation?.resolve(confirmsActivation ? false : null)}
        onDismiss={() => confirmation?.resolve(null)}
      />
    </>
  );
}
