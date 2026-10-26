import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { Loading } from '../Loading/Loading';
import './Button.css';

type ButtonBaseProps = Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> & {
  variant?: 'primary' | 'secondary' | 'quiet' | 'warning' | 'danger';
  size?: 'sm' | 'md';
  loading?: boolean;
};

type TextButtonProps = ButtonBaseProps & {
  children: ReactNode;
  icon?: ReactNode;
};

type IconButtonProps = ButtonBaseProps & {
  children?: never;
  icon: ReactNode;
  'aria-label': string;
};

export type ButtonProps = TextButtonProps | IconButtonProps;

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = 'secondary',
    size = 'md',
    loading = false,
    type = 'button',
    icon,
    disabled,
    children,
    className,
    ...props
  },
  ref,
) {
  return (
    <button
      {...props}
      ref={ref}
      type={type}
      disabled={disabled || loading}
      className={`ui-button ui-button--${variant} ui-button--${size}${children ? '' : ' ui-button--icon-only'}${className ? ` ${className}` : ''}`}
    >
      {loading ? <Loading size="sm" aria-label="" /> : icon ? <span className="ui-button__icon">{icon}</span> : null}
      {children ? <span className="ui-button__label">{children}</span> : null}
    </button>
  );
});
