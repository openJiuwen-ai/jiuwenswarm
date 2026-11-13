import { useTranslation } from 'react-i18next';
import { ModelProviderIcon } from '../../../components/ModelProviderIcon';
import { multimodalModelOptions, type PublicMultimodalModel } from '../../settings/services/multimodalSettings';
import type { DesignGenerationSlot } from '../designerModelConfiguration';

type DesignGenerationModelListProps = {
  slot: DesignGenerationSlot;
  models: readonly PublicMultimodalModel[];
  selectedId: string | null;
  loading: boolean;
  failed: boolean;
  disabled?: boolean;
  selectError?: string;
  onSelect: (modelId: string) => void;
  onRetry?: () => void;
  testIdPrefix: string;
};

export default function DesignGenerationModelList({
  slot,
  models,
  selectedId,
  loading,
  failed,
  disabled = false,
  selectError = '',
  onSelect,
  onRetry,
  testIdPrefix,
}: DesignGenerationModelListProps) {
  const { t } = useTranslation();
  const key = 'designer.modelConfiguration';
  const options = multimodalModelOptions([...models], slot);
  if (failed) {
    return (
      <div role="alert" className="designer-model-selector__state" data-testid={`${testIdPrefix}-error`}>
        <p>{t(`${key}.loadFailed`)}</p>
        {onRetry ? (
          <button type="button" onClick={onRetry} data-testid={`${testIdPrefix}-retry`}>{t(`${key}.retry`)}</button>
        ) : null}
      </div>
    );
  }
  if (loading && options.length === 0) {
    return (
      <p role="status" className="designer-model-selector__state" data-testid={`${testIdPrefix}-loading`}>
        {t(`${key}.loading`)}
      </p>
    );
  }
  if (options.length === 0) {
    return (
      <p className="designer-model-selector__state" data-testid={`${testIdPrefix}-empty`}>
        {t(`designer.models.${slot === 'video_gen' ? 'videoMissing' : 'imageMissing'}`)}
      </p>
    );
  }
  return (
    <div role="group" aria-label={t(`${key}.${slot}`)} className="designer-generation-model-list" data-testid={testIdPrefix}>
      {options.map((option) => {
        const model = models.find((item) => item.model_id === option.value);
        const selected = option.value === selectedId;
        return (
          <button
            key={option.value}
            type="button"
            className={`chat-mode-select__option${selected ? ' chat-mode-select__option--active' : ''}`}
            role="menuitemradio"
            aria-checked={selected}
            disabled={disabled}
            title={model ? [model.provider, model.model_name].filter(Boolean).join(' · ') : option.label}
            onClick={() => onSelect(option.value)}
            data-testid={`${testIdPrefix}-option`}
            data-variant={option.value}
          >
            <span className="chat-mode-select__option-main">
              <span className="chat-mode-select__icon" aria-hidden="true">
                {model ? (
                  <ModelProviderIcon model={{ model_name: model.model_name, model_provider: model.provider }} />
                ) : null}
              </span>
              <span
                className="chat-mode-select__label"
                data-testid={selected ? `${testIdPrefix}-name` : undefined}
              >
                {option.label}
              </span>
            </span>
            {selected ? (
              <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
              </svg>
            ) : null}
          </button>
        );
      })}
      {selectError ? (
        <p role="alert" className="designer-model-selector__state" data-testid={`${testIdPrefix}-select-error`}>{selectError}</p>
      ) : null}
    </div>
  );
}
