import type { SettingItemDefinition } from '../../registry/types';
import type { MultimodalCapability } from '../../services/multimodalSettings';
import { useMultimodalSettings } from '../../services/useMultimodalSettings';
import { MultimodalModelDialog } from '../models/MultimodalModelDialog';
import { MultimodalSettingsFeedback } from '../models/MultimodalSettingsFeedback';

function configurationFor(tool: MultimodalCapability) {
  return function ConfigureModel({ onClose }: { onClose: () => void }) {
    const { snapshot } = useMultimodalSettings();
    return snapshot ? <MultimodalModelDialog defaultCapability={tool} onClose={onClose} /> : null;
  };
}

export function multimodalToolItems(tool: MultimodalCapability): SettingItemDefinition[] {
  const Configure = configurationFor(tool);
  return [{
    id: `${tool}-enabled`, component: 'switch', key: `${tool}_enabled`, source: 'multimodal', configure: Configure,
    subItems: { show: 'when-parent-checked', disabled: 'never', items: [{
      id: `${tool}-model`, component: 'select', key: `${tool}_model_id`, source: 'multimodal', options: [], optionsFromSource: true, configure: Configure,
    }] },
  }];
}

export { MultimodalSettingsFeedback };
