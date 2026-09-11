import type { AriaRole, CSSProperties, ReactNode } from 'react';
import './PageToolbar.css';

export interface PageToolbarProps {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
  testId?: string;
  role?: AriaRole;
  ariaLabel?: string;
}

export function PageToolbar({ children, className, style, testId, role, ariaLabel }: PageToolbarProps) {
  const classes = ['page-toolbar', 'page-toolbar--page', className].filter(Boolean).join(' ');
  return (
    <div className={classes} style={style} role={role} aria-label={ariaLabel} data-testid={testId}>
      {children}
    </div>
  );
}
