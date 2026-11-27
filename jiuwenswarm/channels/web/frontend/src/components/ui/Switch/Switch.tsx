import { forwardRef, type ButtonHTMLAttributes } from 'react';
import { Loader2 } from 'lucide-react';
import './Switch.css';

export type SwitchProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onChange' | 'type'> & {
  checked: boolean;
  onChange: (checked: boolean) => void;
  /** In-flight work behind the switch: shows a spinner on the track and locks interaction. */
  loading?: boolean;
};
export const Switch = forwardRef<HTMLButtonElement, SwitchProps>(function Switch(
  { checked, onChange, className, disabled, loading, ...props },
  ref,
) {
  return (
    <button
      {...props}
      ref={ref}
      type="button"
      role="switch"
      aria-checked={checked}
      aria-busy={loading || undefined}
      disabled={disabled || loading}
      className={`ui-switch${checked ? ' ui-switch--checked' : ''}${loading ? ' ui-switch--loading' : ''}${className ? ` ${className}` : ''}`}
      onClick={() => onChange(!checked)}
    >
      <span />
      {loading ? <Loader2 className="ui-switch__spinner" aria-hidden /> : null}
    </button>
  );
});
