import { type KeyboardEvent, type MouseEvent, type ReactNode } from 'react';
import { EntityHeader, type EntityHeaderAvatar } from '../EntityHeader/EntityHeader';
import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import './PageCard.css';

export interface PageCardActionProps {
  icon: ReactNode;
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  tooltip?: string;
}

export type PageCardAvatar = EntityHeaderAvatar;

export interface PageCardProps {
  avatar: PageCardAvatar;
  title: string;
  titleEnd?: ReactNode;
  label?: string[];
  action?: PageCardActionProps;
  actionSlot?: ReactNode;
  description?: string;
  onClick?: () => void;
  interactive?: boolean;
  ariaLabel?: string;
  className?: string;
  testId?: string;
  variant?: string;
}

export function PageCard({
  avatar,
  title,
  titleEnd,
  label,
  action,
  actionSlot,
  description,
  onClick,
  interactive = false,
  ariaLabel,
  className,
  testId,
  variant,
}: PageCardProps) {
  const classNames = ['page-card'];
  if (className) classNames.push(className);

  const { tooltip, handlers: tooltipHandlers } = useAdaptiveTooltip({ placement: 'top' });

  const hasLabel = Array.isArray(label) && label.length > 0;
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!interactive || !onClick || event.target !== event.currentTarget) return;
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    onClick();
  };

  return (
    <div
      onClick={onClick}
      onKeyDown={handleKeyDown}
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      aria-label={interactive ? ariaLabel : undefined}
      className={[...classNames, ...(interactive ? ['page-card--interactive'] : [])].join(' ')}
      data-testid={testId}
      data-variant={variant}
    >
      <EntityHeader
        variant="card"
        avatar={avatar}
        title={title}
        titleEnd={titleEnd}
        tags={hasLabel ? label : undefined}
        actions={
          action ? (
            <button
              type="button"
              className="page-card-action"
              disabled={action.disabled}
              onClick={(e) => {
                e.stopPropagation();
                action.onClick?.(e);
              }}
              data-tooltip={action.tooltip}
              {...(action.tooltip ? tooltipHandlers : {})}
            >
              {action.icon}
            </button>
          ) : (
            actionSlot
          )
        }
      />
      {description ? <div className="page-card__body">{description}</div> : null}
      {tooltip}
    </div>
  );
}
