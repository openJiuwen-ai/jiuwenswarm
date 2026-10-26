import { forwardRef, type SelectHTMLAttributes } from 'react';
import './Select.css';

export type SelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
  disabledReason?: string;
};
export type SelectProps = Omit<SelectHTMLAttributes<HTMLSelectElement>, 'onChange'> & {
  options: readonly SelectOption[];
  invalid?: boolean;
  onChange?: (value: string) => void;
};
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { options, invalid = false, className, onChange, ...props },
  ref,
) {
  return (
    <select
      {...props}
      ref={ref}
      aria-invalid={invalid || undefined}
      className={`ui-select${invalid ? ' ui-select--invalid' : ''}${className ? ` ${className}` : ''}`}
      onChange={(event) => onChange?.(event.target.value)}
    >
      {options.map((option) => (
        <option
          key={option.value}
          value={option.value}
          disabled={option.disabled}
          title={option.disabledReason}
        >
          {option.label}
          {option.disabledReason ? ` (${option.disabledReason})` : ''}
        </option>
      ))}
    </select>
  );
});
