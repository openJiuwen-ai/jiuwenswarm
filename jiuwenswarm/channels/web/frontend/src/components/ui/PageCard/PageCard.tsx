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
  /** Selection state for interactive cards such as management pickers. */
  selected?: boolean;
  /** Disabled state for interactive cards that remain visible but cannot be selected. */
  disabled?: boolean;
  ariaLabel?: string;
  className?: string;
  testId?: string;
  /** Optional test hook for the clickable card header. */
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

  const { tooltip, handlers: tooltipHandlers } = useAdaptiveTooltip({ placement: 'top' });

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
