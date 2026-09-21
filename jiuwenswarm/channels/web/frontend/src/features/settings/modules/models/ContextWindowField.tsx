import { Button, Input } from '../../../../components/ui';
import { CONTEXT_WINDOW_PRESETS, parseContextWindowTokens } from './contextWindow';
import './ContextWindowField.css';

type ContextWindowFieldProps = {
  id: string;
  value: unknown;
  error?: string;
  disabled: boolean;
  placeholder: string;
  presetLabel: string;
  onChange: (value: string) => void;
  onBlur: () => void;
};

export function ContextWindowField({
  id,
  value,
  error,
  disabled,
  placeholder,
  presetLabel,
  onChange,
  onBlur,
}: ContextWindowFieldProps) {
  const currentValue = String(value ?? '');
  const currentTokens = parseContextWindowTokens(currentValue);

  return (
    <div className="settings-context-window-field" data-testid="settings-context-window-field">
      <Input
        id={id}
        value={currentValue}
        placeholder={placeholder}
        disabled={disabled}
        invalid={Boolean(error)}
        data-testid="settings-context-window-input"
        onBlur={onBlur}
        onChange={onChange}
      />
      <div
        className="settings-context-window-field__presets"
        role="group"
        aria-label={presetLabel}
        data-testid="settings-context-window-presets"
      >
        {CONTEXT_WINDOW_PRESETS.map((preset) => {
          const selected = currentTokens === parseContextWindowTokens(preset);
          return (
            <Button
              key={preset}
              size="sm"
              variant={selected ? 'primary' : 'secondary'}
              aria-pressed={selected}
              data-testid="settings-context-window-preset"
              data-variant={preset}
              disabled={disabled}
              onClick={() => onChange(preset)}
            >
              {preset}
            </Button>
          );
        })}
      </div>
    </div>
  );
}
