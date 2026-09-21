import { forwardRef, useCallback, useEffect, useRef, useImperativeHandle, type TextareaHTMLAttributes } from 'react';
import './Textarea.css';

export type TextareaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'onChange'> & {
  invalid?: boolean;
  onChange?: (value: string) => void;
  scrollable?: boolean;
  showCounter?: boolean;
  counterTestId?: string;
};

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { invalid = false, className, onChange, scrollable = false, showCounter = false, counterTestId, ...props },
  ref,
) {
  const innerRef = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => innerRef.current!);

  const resizeTextArea = useCallback(() => {
    const el = innerRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = el.scrollHeight + 'px';
  }, []);

  useEffect(() => {
    if (scrollable) resizeTextArea();
  }, [scrollable, props.value, resizeTextArea]);

  const handleChange = useCallback(
    (event: React.ChangeEvent<HTMLTextAreaElement>) => {
      onChange?.(event.target.value);
      if (scrollable) {
        const el = event.target;
        el.style.height = 'auto';
        el.style.height = el.scrollHeight + 'px';
      }
    },
    [onChange, scrollable],
  );

  const ariaInvalid = invalid || undefined;
  const valueLength = String(props.value ?? '').length;
  const max = typeof props.maxLength === 'number' ? props.maxLength : undefined;
  const isAtLimit = max != null && valueLength >= max;
  const needRoot = scrollable || showCounter;

  if (!needRoot) {
    return (
      <textarea
        {...props}
        ref={innerRef}
        aria-invalid={ariaInvalid}
        className={`ui-textarea${invalid ? ' ui-textarea--invalid' : ''}${className ? ` ${className}` : ''}`}
        onChange={handleChange}
      />
    );
  }

  return (
    <div className="ui-textarea-root">
      {scrollable ? (
        <div
          className={`ui-textarea-wrapper${invalid ? ' ui-textarea-wrapper--invalid' : ''}${className ? ` ${className}` : ''}`}
        >
          <div className="ui-textarea-scroll-container">
            <textarea
              {...props}
              ref={innerRef}
              aria-invalid={ariaInvalid}
              className="ui-textarea-scrollable"
              onChange={handleChange}
            />
          </div>
        </div>
      ) : (
        <textarea
          {...props}
          ref={innerRef}
          aria-invalid={ariaInvalid}
          className={`ui-textarea${invalid ? ' ui-textarea--invalid' : ''}${className ? ` ${className}` : ''}`}
          onChange={handleChange}
        />
      )}
      {showCounter && max != null ? (
        <span
          aria-hidden="true"
          className={`ui-textarea-counter${isAtLimit ? ' is-at-limit' : ''}`}
          data-testid={counterTestId}
        >
          {valueLength}
          <span className="ui-textarea-counter-separator">/{max}</span>
        </span>
      ) : null}
    </div>
  );
});
