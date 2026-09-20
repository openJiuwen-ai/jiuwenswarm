import { forwardRef, type ButtonHTMLAttributes } from 'react';
import './Switch.css';

export type SwitchProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onChange' | 'type'> & {
  checked: boolean;
  onChange: (checked: boolean) => void;
};
export const Switch = forwardRef<HTMLButtonElement, SwitchProps>(function Switch(
  { checked, onChange, className, disabled, ...props },
  ref,
) {
  return (
    <button
      {...props}
      ref={ref}
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      className={`ui-switch${checked ? ' ui-switch--checked' : ''}${className ? ` ${className}` : ''}`}
      onClick={() => onChange(!checked)}
    >
      <span />
    </button>
  );
});
