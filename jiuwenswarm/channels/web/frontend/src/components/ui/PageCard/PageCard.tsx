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

/** 右上角浮现的单个主操作按钮（.page-card__actions）。
 *  与 action / actionSlot 的取舍优先级：action > defaultButton > actionSlot，
 *  同时传多个时只渲染最高优先级的一个，其余静默忽略（不做告警）。 */
export interface PageCardDefaultButton {
  text: string;
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  busy?: boolean;
  className?: string;
  testId?: string;
  variant?: string;
}

export type PageCardAvatar = EntityHeaderAvatar;

export interface PageCardProps {
  avatar: PageCardAvatar;
  title: string;
  titleEnd?: ReactNode;
  label?: string[];
  action?: PageCardActionProps;
  actionSlot?: ReactNode;
  defaultButton?: PageCardDefaultButton;
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
  defaultButton,
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

  const { tooltip, handlers: tooltipHandlers } = useAdaptiveTooltip({ placement: 'top' });

  const hasLabel = Array.isArray(label) && label.length > 0;
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!interactive || disabled || !onClick || event.target !== event.currentTarget) return;
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    onClick();
  };

  const iconAction = action ? (
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
  ) : null;

  const defaultButtonAction = defaultButton ? (
    <div className="page-card__actions" onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        className={['page-card__default-button', defaultButton.className].filter(Boolean).join(' ')}
        data-testid={defaultButton.testId ?? 'page-card-default-button'}
        data-variant={defaultButton.variant ?? variant}
        disabled={defaultButton.disabled}
        aria-disabled={defaultButton.disabled || undefined}
        aria-busy={defaultButton.busy || undefined}
        onClick={(e) => {
          e.stopPropagation();
          defaultButton.onClick?.(e);
        }}
      >
        {defaultButton.text}
      </button>
    </div>
  ) : null;

  const actionNode = action ? iconAction : (defaultButtonAction ?? actionSlot);

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
        actions={actionsHover ? <div className="page-card-actions-hover">{actionNode}</div> : actionNode}
      />
      {description ? <div className="page-card__body">{description}</div> : null}
      {tooltip}
    </div>
  );
}
