import { forwardRef, useId, useState, type FocusEvent, type InputHTMLAttributes } from 'react';
import { Eye, EyeOff } from 'lucide-react';
import './Input.css';

type PasswordVisibilityLabels = { show: string; hide: string };
export type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'size'> & {
  invalid?: boolean;
  passwordVisibilityLabels?: PasswordVisibilityLabels;
  changeOnBlur?: boolean;
  onChange?: (value: string) => void;
};

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  {
    type = 'text',
    invalid = false,
    passwordVisibilityLabels,
    className,
    onChange,
    value,
    id,
    changeOnBlur = true,
    onBlur,
    min,
    max,
    ...props
  },
  ref,
) {
  const generatedId = useId();
  const [passwordVisible, setPasswordVisible] = useState(false);
  const canToggle = type === 'password' && passwordVisibilityLabels !== undefined;
  const inputId = id ?? generatedId;
  const handleBlur = (event: FocusEvent<HTMLInputElement>) => {
    if (changeOnBlur && type === 'number') {
      const raw = event.currentTarget.value;
      if (raw !== '') {
        const parsed = Number(raw);
        if (Number.isFinite(parsed)) {
          const minNum = min !== undefined ? Number(min) : NaN;
          const maxNum = max !== undefined ? Number(max) : NaN;
          const hasMin = Number.isFinite(minNum);
          const hasMax = Number.isFinite(maxNum);
          if (!(hasMin && hasMax && minNum > maxNum)) {
            let clamped = parsed;
            if (hasMin && clamped < minNum) clamped = minNum;
            if (hasMax && clamped > maxNum) clamped = maxNum;
            if (clamped !== parsed) onChange?.(String(clamped));
          }
        }
      }
    }
    onBlur?.(event);
  };
  return (
    <span className="ui-input-wrap">
      <input
        {...props}
        ref={ref}
        id={inputId}
        type={canToggle && passwordVisible ? 'text' : type}
        value={value}
        min={min}
        max={max}
        aria-invalid={invalid || undefined}
        className={`ui-input${invalid ? ' ui-input--invalid' : ''}${canToggle ? ' ui-input--password' : ''}${className ? ` ${className}` : ''}`}
        onChange={(event) => onChange?.(event.target.value)}
        onBlur={handleBlur}
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
