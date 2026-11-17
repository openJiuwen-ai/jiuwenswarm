import { AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { ModelProviderIcon } from '../../../components/ModelProviderIcon';
import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import { multimodalModelOptions, type PublicMultimodalModel } from '../../settings/services/multimodalSettings';
import { designGenerationProtocolSupported, type DesignGenerationSlot } from '../designerModelConfiguration';

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
  const { tooltip, handlers } = useAdaptiveTooltip({ placement: 'right' });
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
        const protocol = String(model?.protocol ?? '').trim();
        const unsupported = !designGenerationProtocolSupported(protocol);
        const selected = !unsupported && option.value === selectedId;
        const reason = unsupported
          ? (protocol
            ? t('designer.models.protocolUnsupported', { protocol })
            : t('designer.models.protocolUnsupportedEmpty'))
          : '';
        return (
          <span
            key={option.value}
            className="designer-generation-model-list__row"
            data-tooltip={reason || undefined}
            {...(unsupported ? handlers : {})}
          >
            <button
              type="button"
              className={`chat-mode-select__option${selected ? ' chat-mode-select__option--active' : ''}${unsupported ? ' chat-mode-select__option--disabled' : ''}`}
              role="menuitemradio"
              aria-checked={selected}
              aria-disabled={unsupported || disabled || undefined}
              disabled={disabled && !unsupported}
              onClick={() => { if (!unsupported && !disabled) onSelect(option.value); }}
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
                  data-tooltip={
                    unsupported || !model ? undefined : [model.provider, protocol, model.model_name].join(' · ')
                  }
                  {...(!unsupported ? handlers : {})}
                  data-testid={selected ? `${testIdPrefix}-name` : undefined}
                >
                  {option.label}
                </span>
              </span>
              {unsupported ? (
                <AlertTriangle
                  size={14}
                  aria-hidden
                  className="designer-generation-model-list__warning"
                  data-testid={`${testIdPrefix}-protocol-warning`}
                  data-variant={option.value}
                />
              ) : selected ? (
                <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                </svg>
              ) : null}
            </button>
          </span>
        );
      })}
      {selectError ? (
        <p role="alert" className="designer-model-selector__state" data-testid={`${testIdPrefix}-select-error`}>{selectError}</p>
      ) : null}
      {tooltip}
    </div>
  );
}
