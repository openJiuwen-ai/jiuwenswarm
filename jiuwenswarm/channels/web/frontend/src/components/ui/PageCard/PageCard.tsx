import { type KeyboardEvent, type MouseEvent, type ReactNode } from 'react';
import { EntityHeader, type EntityHeaderAvatar } from '../EntityHeader/EntityHeader';
import './PageCard.css';

export interface PageCardActionProps {
  icon: ReactNode;
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
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
  selected?: boolean;
  disabled?: boolean;
  actionsHover?: boolean;
  ariaLabel?: string;
  className?: string;
  testId?: string;
  headerTestId?: string;
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
  selected,
  disabled = false,
  actionsHover = false,
  ariaLabel,
  className,
  testId,
  headerTestId,
  variant,
}: PageCardProps) {
  const classNames = ['page-card'];
  if (className) classNames.push(className);
  if (selected) classNames.push('page-card--selected');
  if (disabled) classNames.push('page-card--disabled');

  const hasLabel = Array.isArray(label) && label.length > 0;
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!interactive || disabled || !onClick || event.target !== event.currentTarget) return;
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    onClick();
  };

  return (
    <div
      onClick={disabled ? undefined : onClick}
      onKeyDown={handleKeyDown}
      role={interactive ? 'button' : undefined}
      tabIndex={interactive && !disabled ? 0 : undefined}
      aria-label={interactive ? ariaLabel : undefined}
      aria-disabled={interactive && disabled ? true : undefined}
      aria-pressed={interactive && selected !== undefined ? selected : undefined}
      className={[...classNames, ...(interactive ? ['page-card--interactive'] : [])].join(' ')}
      data-testid={testId}
      data-variant={variant}
    >
      <EntityHeader
        variant="card"
        testId={headerTestId}
        avatar={avatar}
        title={title}
        titleEnd={titleEnd}
        tags={hasLabel ? label : undefined}
        actions={
          actionsHover ? (
            <div className="page-card-actions-hover">
              {action ? (
                <button
                  type="button"
                  className="page-card-action"
                  disabled={action.disabled}
                  onClick={(e) => {
                    e.stopPropagation();
                    action.onClick?.(e);
                  }}
                >
                  {action.icon}
                </button>
              ) : (
                actionSlot
              )}
            </div>
          ) : (
            action ? (
              <button
                type="button"
                className="page-card-action"
                disabled={action.disabled}
                onClick={(e) => {
                  e.stopPropagation();
                  action.onClick?.(e);
                }}
              >
                {action.icon}
              </button>
            ) : (
              actionSlot
            )
          )
        }
      />
      {description ? <div className="page-card__body">{description}</div> : null}
    </div>
  );
}
