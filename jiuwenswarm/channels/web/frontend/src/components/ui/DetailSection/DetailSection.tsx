import type { ReactNode } from 'react';
import './DetailSection.css';

export interface DetailSectionProps {
  title: ReactNode;
  titleTestId?: string;
  children: ReactNode;
  className?: string;
  testId?: string;
}

export function DetailSection({ title, titleTestId, children, className, testId }: DetailSectionProps) {
  const classes = ['detail-section', className].filter(Boolean).join(' ');
  return (
    <section className={classes} data-testid={testId}>
      <h2 className="detail-section__title" data-testid={titleTestId}>
        {title}
      </h2>
      <div className="detail-section__body">{children}</div>
    </section>
  );
}
