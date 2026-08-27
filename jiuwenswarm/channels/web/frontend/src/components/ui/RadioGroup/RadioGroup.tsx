import { useId } from 'react';
import './RadioGroup.css';

export type RadioOption = { value: string; label: string; disabled?: boolean; disabledReason?: string };
export function RadioGroup({
  value,
  options,
  disabled,
  'aria-label': ariaLabel,
  onChange,
}: {
  value: string;
  options: readonly RadioOption[];
  disabled?: boolean;
  'aria-label': string;
  onChange: (value: string) => void;
}) {
  const name = useId();
  return (
    <div className="ui-radio-group" role="radiogroup" aria-label={ariaLabel}>
      {options.map((option) => (
        <label
          key={option.value}
          className="ui-radio-group__option"
          title={option.disabledReason}
        >
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={value === option.value}
            disabled={disabled || option.disabled}
            aria-describedby={option.disabledReason ? `${name}-${option.value}-reason` : undefined}
            onChange={() => onChange(option.value)}
          />
          <span>{option.label}</span>
          {option.disabledReason ? (
            <span id={`${name}-${option.value}-reason`} className="ui-radio-group__reason">
              {option.disabledReason}
            </span>
          ) : null}
        </label>
      ))}
    </div>
  );
}
