import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useDesignerStore } from '../../designerStore';
import type {
  DesignerComfyuiConfig,
  DesignerComfyuiFields,
  DesignerComfyuiSamplingParams,
} from '../../executionGraphTypes';

type NumberSpec = { kind: 'int' | 'float'; min?: number; step?: number };

const FIELD_NUMBERS: Partial<Record<keyof DesignerComfyuiFields, NumberSpec>> = {
  width: { kind: 'int', min: 1 },
  height: { kind: 'int', min: 1 },
  fps: { kind: 'int', min: 1 },
  duration: { kind: 'float', min: 0.1, step: 0.1 },
};

const SAMPLING_NUMBERS: Partial<Record<keyof DesignerComfyuiSamplingParams, NumberSpec>> = {
  num_inference_steps: { kind: 'int', min: 1 },
  guidance_scale: { kind: 'float', min: 0, step: 0.1 },
  true_cfg_scale: { kind: 'float', min: 0, step: 0.5 },
  seed: { kind: 'int', min: -1 },
};

const SAMPLING_FLAGS: Array<keyof DesignerComfyuiSamplingParams> = ['vae_use_slicing', 'vae_use_tiling'];

function NumberInput({
  value,
  spec,
  testId,
  onCommit,
}: {
  value: number;
  spec: NumberSpec;
  testId: string;
  onCommit: (value: number) => void;
}) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  return (
    <input
      type="number"
      className="designer-node-toolbar__param-input"
      value={draft}
      min={spec.min}
      step={spec.step ?? 1}
      data-testid={testId}
      onChange={(event) => {
        const raw = event.target.value;
        setDraft(raw);
        const parsed = Number(raw);
        if (raw.trim() === '' || !Number.isFinite(parsed)) return;
        onCommit(spec.kind === 'int' ? Math.round(parsed) : parsed);
      }}
    />
  );
}

/** Imported ComfyUI widgets and sampling params, edited in place on `config.comfyui`. */
export function DesignerComfyuiParamsForm({ nodeId, comfyui }: { nodeId: string; comfyui: DesignerComfyuiConfig }) {
  const { t } = useTranslation();
  const updateNodeConfig = useDesignerStore((state) => state.updateNodeConfig);

  const patch = (next: (previous: DesignerComfyuiConfig) => Partial<DesignerComfyuiConfig>) =>
    updateNodeConfig(nodeId, (current) => {
      const previous = (current.comfyui ?? comfyui) as DesignerComfyuiConfig;
      return { ...current, comfyui: { ...previous, ...next(previous) } };
    });
  const patchField = (key: keyof DesignerComfyuiFields, value: string | number) =>
    patch((previous) => ({ fields: { ...previous.fields, [key]: value } }));
  const patchSampling = (key: keyof DesignerComfyuiSamplingParams, value: number | boolean) =>
    patch((previous) =>
      previous.sampling_params ? { sampling_params: { ...previous.sampling_params, [key]: value } } : {},
    );

  const fieldLabel = (key: string) => t(`designer.comfyui.field.${key}`, { defaultValue: key });
  const numberFields = (Object.keys(FIELD_NUMBERS) as Array<keyof DesignerComfyuiFields>).filter(
    (key) => typeof comfyui.fields[key] === 'number',
  );
  const sampling = comfyui.sampling_params;

  return (
    <div className="designer-node-toolbar__comfyui" data-testid="designer-node-toolbar-comfyui">
      <div className="designer-node-toolbar__params" data-testid="designer-node-toolbar-comfyui-fields">
        {(['url', 'model'] as const).map((key) => (
          <label key={key} className="designer-node-toolbar__param designer-node-toolbar__param--wide">
            {fieldLabel(key)}
            <input
              type="text"
              className="designer-node-toolbar__param-input"
              value={comfyui.fields[key]}
              spellCheck={false}
              data-testid={`designer-node-toolbar-comfyui-${key}`}
              onChange={(event) => patchField(key, event.target.value)}
            />
          </label>
        ))}
        <label className="designer-node-toolbar__param designer-node-toolbar__param--wide">
          {fieldLabel('negative_prompt')}
          <textarea
            className="designer-node-toolbar__param-input"
            rows={2}
            value={comfyui.fields.negative_prompt}
            data-testid="designer-node-toolbar-comfyui-negative-prompt"
            onChange={(event) => patchField('negative_prompt', event.target.value)}
          />
        </label>
        {numberFields.map((key) => (
          <label key={key} className="designer-node-toolbar__param">
            {fieldLabel(key)}
            <NumberInput
              value={comfyui.fields[key] as number}
              spec={FIELD_NUMBERS[key] as NumberSpec}
              testId={`designer-node-toolbar-comfyui-${key.replace(/_/g, '-')}`}
              onCommit={(value) => patchField(key, value)}
            />
          </label>
        ))}
      </div>
      {sampling ? (
        <div className="designer-node-toolbar__params" data-testid="designer-node-toolbar-comfyui-sampling">
          <p className="designer-node-toolbar__params-title">{t('designer.comfyui.samplingTitle')}</p>
          {(Object.keys(SAMPLING_NUMBERS) as Array<keyof DesignerComfyuiSamplingParams>).map((key) => (
            <label key={key} className="designer-node-toolbar__param">
              {fieldLabel(key)}
              <NumberInput
                value={sampling[key] as number}
                spec={SAMPLING_NUMBERS[key] as NumberSpec}
                testId={`designer-node-toolbar-comfyui-${key.replace(/_/g, '-')}`}
                onCommit={(value) => patchSampling(key, value)}
              />
            </label>
          ))}
          {SAMPLING_FLAGS.map((key) => (
            <label key={key} className="designer-node-toolbar__param designer-node-toolbar__param--check">
              <input
                type="checkbox"
                checked={Boolean(sampling[key])}
                data-testid={`designer-node-toolbar-comfyui-${key.replace(/_/g, '-')}`}
                onChange={(event) => patchSampling(key, event.target.checked)}
              />
              {fieldLabel(key)}
            </label>
          ))}
        </div>
      ) : null}
    </div>
  );
}
