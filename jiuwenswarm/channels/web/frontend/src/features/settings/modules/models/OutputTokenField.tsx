import { Button, Input } from '../../../../components/ui';
import { OUTPUT_TOKEN_PRESETS, parseOutputTokens } from './outputTokens';
import './OutputTokenField.css';

type OutputTokenFieldProps = {
  id: string;
  value: unknown;
  error?: string;
  disabled: boolean;
  placeholder: string;
  presetLabel: string;
  unlimitedLabel: string;
  showUnlimited: boolean;
  maxTokens?: number;
  onChange: (value: string) => void;
  onBlur: () => void;
};

export function OutputTokenField({
  id,
  value,
  error,
  disabled,
  placeholder,
  presetLabel,
  unlimitedLabel,
  showUnlimited,
  maxTokens,
  onChange,
  onBlur,
}: OutputTokenFieldProps) {
  const currentValue = String(value ?? '');
  const currentTokens = parseOutputTokens(currentValue);
  const unlimited = !currentValue.trim();

  return (
    <div className="settings-output-token-field" data-testid="settings-output-token-field">
      <Input
        id={id}
        value={currentValue}
        placeholder={placeholder}
        disabled={disabled}
        invalid={Boolean(error)}
        data-testid="settings-output-token-input"
        onBlur={onBlur}
        onChange={onChange}
      />
      <div
        className="settings-output-token-field__presets"
        role="group"
        aria-label={presetLabel}
        data-testid="settings-output-token-presets"
      >
        {OUTPUT_TOKEN_PRESETS.map((preset) => {
          const selected = currentTokens === parseOutputTokens(preset);
          const exceedsModelLimit = maxTokens !== undefined && (parseOutputTokens(preset) ?? 0) > maxTokens;
          return (
            <Button
              key={preset}
              size="sm"
              variant={selected ? 'primary' : 'secondary'}
              aria-pressed={selected}
              data-testid="settings-output-token-preset"
              data-variant={preset}
              disabled={disabled || exceedsModelLimit}
              onClick={() => onChange(preset)}
            >
              {preset}
            </Button>
          );
        })}
        {showUnlimited ? (
          <Button
            size="sm"
            variant={unlimited ? 'primary' : 'secondary'}
            aria-pressed={unlimited}
            data-testid="settings-output-token-unlimited"
            disabled={disabled}
            onClick={() => onChange('')}
          >
            {unlimitedLabel}
          </Button>
        ) : null}
      </div>
    </div>
  );
}
