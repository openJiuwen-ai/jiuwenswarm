import type { ModelEntry } from '../../../../types';
import { displayModelProtocol } from './modelAdapters';
import type { ModelTranslate } from './modelTranslation';
import { getVendorLabel } from './modelVendorLabels';

/** Primary label shown in pickers and cards. */
export function getModelDisplayName(model: ModelEntry): string {
  return model.alias?.trim() || model.model_name;
}

/**
 * Secondary identity line: provider · protocol · model id
 * e.g. "自定义 · OpenAI · GLM-5.2"
 */
export function formatModelMetadata(model: ModelEntry, t: ModelTranslate): string {
  const protocolLabel = t(`settingsPanel.models.protocols.${displayModelProtocol(model)}`);
  if (model.model_provider === 'OpenAIAccount') {
    return [t('settingsPanel.models.openaiAccount'), protocolLabel, model.model_name].join(' · ');
  }
  const vendorKey = model.vendor_key?.trim();
  const providerLabel = vendorKey ? getVendorLabel(vendorKey, t) : t('settingsPanel.models.customVendor');
  return [providerLabel, protocolLabel, model.model_name].join(' · ');
}
