import type { SettingItemDefinition } from '../../registry/types';
import type { MultimodalCapability } from '../../services/multimodalSettings';
import { useMultimodalSettings } from '../../services/useMultimodalSettings';
import { MultimodalModelDialog } from '../models/MultimodalModelDialog';
import { MultimodalSettingsFeedback } from '../models/MultimodalSettingsFeedback';

function configurationFor(tool: MultimodalCapability, { enableToolOnSave = false } = {}) {
  return function ConfigureModel({ onClose }: { onClose: () => void }) {
    const { snapshot } = useMultimodalSettings();
    return snapshot ? (
      <MultimodalModelDialog defaultCapability={tool} enableToolOnSave={enableToolOnSave} onClose={onClose} />
    ) : null;
  };
}

export function multimodalToolItems(tool: MultimodalCapability): SettingItemDefinition[] {
  return [
    {
      id: `${tool}-enabled`,
      component: 'switch',
      key: `${tool}_enabled`,
      source: 'multimodal',
      configure: configurationFor(tool, { enableToolOnSave: true }),
      subItems: {
        show: 'when-parent-checked',
        disabled: 'never',
        items: [
          {
            id: `${tool}-model`,
            component: 'select',
            key: `${tool}_model_id`,
            options: [],
            optionsFromSource: true,
            configure: configurationFor(tool),
          },
        ],
      },
    },
  ];
}

export { MultimodalSettingsFeedback };
