import { type HTMLAttributes, type ReactNode } from 'react';
import './Tag.css';

export type TagVariant = 'success' | 'info' | 'warning' | 'danger' | 'neutral';

export type TagProps = Omit<HTMLAttributes<HTMLSpanElement>, 'children'> & {
  children: ReactNode;
  variant?: TagVariant;
};

export function Tag({ children, variant = 'neutral', className, ...props }: TagProps) {
  return (
    <span {...props} className={`ui-tag ui-tag--${variant}${className ? ` ${className}` : ''}`}>
      {children}
    </span>
  );
}
