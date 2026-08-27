import { forwardRef, useId, useState, type InputHTMLAttributes } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import './Input.css';

type PasswordVisibilityLabels = { show: string; hide: string };
export type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'size'> & {
  invalid?: boolean;
  passwordVisibilityLabels?: PasswordVisibilityLabels;
  onChange?: (value: string) => void;
};

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { type = 'text', invalid = false, passwordVisibilityLabels, className, onChange, value, id, ...props },
  ref,
) {
  const generatedId = useId();
  const [passwordVisible, setPasswordVisible] = useState(false);
  const canToggle = type === 'password' && passwordVisibilityLabels !== undefined;
  const inputId = id ?? generatedId;
  return (
    <span className="ui-input-wrap">
      <input
        {...props}
        ref={ref}
        id={inputId}
        type={canToggle && passwordVisible ? 'text' : type}
        value={value}
        aria-invalid={invalid || undefined}
        className={`ui-input${invalid ? ' ui-input--invalid' : ''}${canToggle ? ' ui-input--password' : ''}${className ? ` ${className}` : ''}`}
        onChange={(event) => onChange?.(event.target.value)}
      />
      {canToggle ? (
        <button
          type="button"
          className="ui-input__visibility"
          aria-label={passwordVisible ? passwordVisibilityLabels.hide : passwordVisibilityLabels.show}
          onClick={() => setPasswordVisible((current) => !current)}
        >
          {passwordVisible ? <EyeOff size={16} aria-hidden /> : <Eye size={16} aria-hidden />}
        </button>
      ) : null}
    </span>
  );
});
