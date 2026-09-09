import {
  forwardRef,
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type FocusEvent,
  type InputHTMLAttributes,
  type MouseEvent,
  type ReactNode,
  type Ref,
} from 'react';
import { Eye, EyeOff, X } from 'lucide-react';
import './Input.css';

type PasswordVisibilityLabels = { show: string; hide: string };

type AllowClearConfig = {
  clearIcon?: ReactNode;
};

export type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'size' | 'prefix'> & {
  invalid?: boolean;
  changeOnBlur?: boolean;
  passwordVisibilityLabels?: PasswordVisibilityLabels;

  prefix?: ReactNode;
  suffix?: ReactNode;

  allowClear?: boolean | AllowClearConfig;
  clearLabel?: string;
  onClear?: () => void;

  size?: 'small' | 'middle';
  rootClassName?: string;

  onChange?: (value: string) => void;
};

function assignRef<T>(ref: Ref<T> | undefined, value: T | null) {
  if (!ref) return;
  if (typeof ref === 'function') {
    ref(value);
    return;
  }
  (ref as { current: T | null }).current = value;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  {
    type = 'text',
    invalid = false,
    passwordVisibilityLabels,
    className,
    rootClassName,
    onChange,
    value,
    defaultValue,
    id,
    changeOnBlur = true,
    onBlur,
    min,
    max,
    size = 'middle',
    prefix,
    suffix,
    allowClear,
    clearLabel,
    onClear,
    disabled,
    readOnly,
    ...props
  },
  ref,
) {
  const generatedId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [passwordVisible, setPasswordVisible] = useState(false);
  const isControlled = value !== undefined;
  const [uncontrolledValue, setUncontrolledValue] = useState(() =>
    defaultValue === undefined || defaultValue === null ? '' : String(defaultValue),
  );
  const mergedValue = isControlled ? value : uncontrolledValue;
  const canToggle = type === 'password' && passwordVisibilityLabels !== undefined;
  const allowClearEnabled = Boolean(allowClear);
  const clearIcon = typeof allowClear === 'object' ? allowClear.clearIcon : undefined;
  const needsAffix = Boolean(prefix) || Boolean(suffix) || allowClearEnabled || canToggle;
  const showClear =
    allowClearEnabled && String(mergedValue ?? '').length > 0 && !disabled && !readOnly;
  const inputId = id ?? generatedId;
  const sizeClass = size === 'small' ? ' ui-input--small' : '';

  const setInputNode = (node: HTMLInputElement | null) => {
    inputRef.current = node;
    assignRef(ref, node);
  };

  const triggerChange = (nextValue: string) => {
    if (!isControlled) {
      setUncontrolledValue(nextValue);
    }
    onChange?.(nextValue);
  };

  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    triggerChange(event.target.value);
  };

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
            if (clamped !== parsed) triggerChange(String(clamped));
          }
        }
      }
    }
    onBlur?.(event);
  };

  const handleClearMouseDown = (event: MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
  };

  const handleClearClick = () => {
    triggerChange('');
    onClear?.();
    inputRef.current?.focus();
  };

  const clearButton = showClear ? (
    <button
      type="button"
      className="ui-input__clear"
      aria-label={clearLabel}
      onMouseDown={handleClearMouseDown}
      onClick={handleClearClick}
    >
      {clearIcon ?? <X size={16} aria-hidden />}
    </button>
  ) : null;

  const visibilityButton = canToggle ? (
    <button
      type="button"
      className="ui-input__visibility"
      aria-label={passwordVisible ? passwordVisibilityLabels.hide : passwordVisibilityLabels.show}
      onClick={() => setPasswordVisible((current) => !current)}
    >
      {passwordVisible ? <EyeOff size={16} aria-hidden /> : <Eye size={16} aria-hidden />}
    </button>
  ) : null;

  const inputClassName = `ui-input${invalid ? ' ui-input--invalid' : ''}${
    needsAffix ? ' ui-input--affixed' : ''
  }${sizeClass}${allowClearEnabled ? ' ui-input--allow-clear' : ''}${className ? ` ${className}` : ''}`;

  if (!needsAffix) {
    return (
      <span className={`ui-input-wrap${rootClassName ? ` ${rootClassName}` : ''}`}>
        <input
          {...props}
          ref={setInputNode}
          id={inputId}
          type={type}
          disabled={disabled}
          readOnly={readOnly}
          min={min}
          max={max}
          aria-invalid={invalid || undefined}
          className={`ui-input${invalid ? ' ui-input--invalid' : ''}${sizeClass}${
            className ? ` ${className}` : ''
          }`}
          {...(isControlled ? { value: mergedValue } : { defaultValue })}
          onChange={handleChange}
          onBlur={handleBlur}
        />
      </span>
    );
  }

  const hasSuffixContent = Boolean(clearButton || suffix || visibilityButton);

  return (
    <span
      className={`ui-input-wrap ui-input-affix-wrapper${
        size === 'small' ? ' ui-input-affix-wrapper--small' : ''
      }${invalid ? ' ui-input-affix-wrapper--invalid' : ''}${
        disabled ? ' ui-input-affix-wrapper--disabled' : ''
      }${rootClassName ? ` ${rootClassName}` : ''}`}
    >
      {prefix ? <span className="ui-input__prefix">{prefix}</span> : null}
      <input
        {...props}
        ref={setInputNode}
        id={inputId}
        type={canToggle && passwordVisible ? 'text' : type}
        disabled={disabled}
        readOnly={readOnly}
        min={min}
        max={max}
        value={mergedValue ?? ''}
        aria-invalid={invalid || undefined}
        className={inputClassName}
        onChange={handleChange}
        onBlur={handleBlur}
      />
      {hasSuffixContent ? (
        <span className="ui-input__suffix">
          {clearButton}
          {suffix}
          {visibilityButton}
        </span>
      ) : null}
    </span>
  );
});
