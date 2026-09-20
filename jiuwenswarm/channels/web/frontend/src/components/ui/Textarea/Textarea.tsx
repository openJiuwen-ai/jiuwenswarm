import { forwardRef, type TextareaHTMLAttributes } from 'react';
import './Textarea.css';

export type TextareaProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, 'onChange'> & {
  invalid?: boolean;
  onChange?: (value: string) => void;
};
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { invalid = false, className, onChange, ...props },
  ref,
) {
  return (
    <textarea
      {...props}
      ref={ref}
      aria-invalid={invalid || undefined}
      className={`ui-textarea${invalid ? ' ui-textarea--invalid' : ''}${className ? ` ${className}` : ''}`}
      onChange={(event) => onChange?.(event.target.value)}
    />
  );
});
