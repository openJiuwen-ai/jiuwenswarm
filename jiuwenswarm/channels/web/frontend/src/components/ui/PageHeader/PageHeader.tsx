import type { ReactNode } from 'react';

export interface PageHeaderProps {
  title: ReactNode;
  subtitle?: ReactNode;
  children?: ReactNode;
}

export function PageHeader({ title, subtitle, children }: PageHeaderProps) {
  return (
    <div className="flex items-start justify-between" data-testid="common-page-header">
      <div>
        <h2 className="h-9 font-semibold text-[24px] leading-[36px]">{title}</h2>
        {subtitle && (
          <p className="text-sm text-text-muted mt-1">{subtitle}</p>
        )}
      </div>
      {children && <div className="flex items-center">{children}</div>}
    </div>
  );
}
