import { type MouseEvent, type ReactNode, useLayoutEffect, useRef, useState } from 'react';
import type { AvatarStyle } from '../../../utils/skillAvatar';
import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import './PageCard.css';

export interface PageCardActionProps {
  icon: ReactNode;
  onClick?: (e: MouseEvent<HTMLButtonElement>) => void;
  disabled?: boolean;
  tooltip?: string;
}

export type PageCardAvatar = AvatarStyle | ReactNode;

function isAvatarStyle(v: PageCardAvatar): v is AvatarStyle {
  return v !== null && typeof v === 'object' && 'firstChar' in v && 'style' in v && typeof (v as AvatarStyle).style === 'object';
}

export interface PageCardProps {
  avatar: PageCardAvatar;
  title: string;
  titleEnd?: ReactNode;
  label?: string[];
  action?: PageCardActionProps;
  actionSlot?: ReactNode;
  description?: string;
  singleLine?: boolean;
  onClick?: () => void;
  className?: string;
  testId?: string;
  variant?: string;
}

function renderAvatarInner(avatar: PageCardAvatar): ReactNode {
  if (isAvatarStyle(avatar)) {
    return <span className="page-card__avatar-letter" style={avatar.style}>{avatar.firstChar}</span>;
  }
  return avatar;
}

function PageCardTagList({ tags }: { tags: string[] }) {
  const metaRef = useRef<HTMLSpanElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [visibleCount, setVisibleCount] = useState(tags.length);

  useLayoutEffect(() => {
    if (tags.length <= 2) {
      setVisibleCount(tags.length);
      return;
    }

    const meta = metaRef.current;
    const measure = measureRef.current;
    if (!meta || !measure) return;

    const update = () => {
      const available = meta.getBoundingClientRect().width;
      if (available <= 0) return;

      const styles = window.getComputedStyle(meta);
      const gap = Number.parseFloat(styles.columnGap) || 0;
      const overflowW = Number.parseFloat(styles.getPropertyValue('--page-card-tag-overflow-width')) || 20;
      const tagWidths = Array.from(measure.children).map(child => child.getBoundingClientRect().width);
      const totalW = tagWidths.reduce((sum, w) => sum + w, 0) + gap * (tagWidths.length - 1);

      if (totalW <= available + 0.5) {
        setVisibleCount(tags.length);
        return;
      }

      let acc = 0;
      let count = 0;
      for (const w of tagWidths) {
        const next = acc + (count > 0 ? gap : 0) + w;
        if (next + gap + overflowW > available + 0.5) break;
        acc = next;
        count += 1;
      }
      setVisibleCount(Math.max(1, count));
    };

    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(meta);
    return () => observer.disconnect();
  }, [tags.length, tags.join('')]);

  const hasOverflow = visibleCount < tags.length;
  const allTags = tags.join(' · ');
  const visible = tags.slice(0, visibleCount);

  return (
    <span ref={metaRef} className="page-card__tags">
      {visible.map((tag, i) => (
        <span key={`${i}-${tag}`} className="page-card__tag">{tag}</span>
      ))}
      {hasOverflow ? (
        <span className="page-card__tag-overflow" title={allTags} aria-label={allTags}>
          <svg viewBox="0 0 16 16" fill="currentColor" width={16} height={16} aria-hidden="true">
            <circle cx="4" cy="8" r="1.5" />
            <circle cx="8" cy="8" r="1.5" />
            <circle cx="12" cy="8" r="1.5" />
          </svg>
        </span>
      ) : null}
      {tags.length > 2 ? (
        <span ref={measureRef} className="page-card__tags-measure" aria-hidden="true">
          {tags.map((tag, i) => (
            <span key={i} className="page-card__tag">{tag}</span>
          ))}
        </span>
      ) : null}
    </span>
  );
}

export function PageCard({
  avatar,
  title,
  titleEnd,
  label,
  action,
  actionSlot,
  description,
  singleLine = false,
  onClick,
  className,
  testId,
  variant,
}: PageCardProps) {
  const classNames = ['page-card'];
  if (singleLine) classNames.push('page-card--single-line');
  if (className) classNames.push(className);

  const { tooltip, handlers: tooltipHandlers } = useAdaptiveTooltip({ placement: 'top' });

  const hasLabel = Array.isArray(label) && label.length > 0;

  return (
    <div
      onClick={onClick}
      className={classNames.join(' ')}
      data-testid={testId}
      data-variant={variant}
    >
      <div className="page-card__header">
        <div className="page-card__avatar">{renderAvatarInner(avatar)}</div>
        <div className="page-card__title-area">
          <div className="page-card__title-row">
            <div className="page-card__title">{title}</div>
            {titleEnd ? (
              <div className="page-card__title-end" onClick={(e) => e.stopPropagation()}>
                {titleEnd}
              </div>
            ) : null}
          </div>
          {hasLabel ? (
            <div className="page-card__label">
              <PageCardTagList tags={label} />
            </div>
          ) : null}
        </div>
        {action ? (
          <div className="page-card__action">
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
          </div>
        ) : actionSlot ? (
          <div className="page-card__action">{actionSlot}</div>
        ) : null}
      </div>
      {description ? <div className="page-card__body">{description}</div> : null}
      {tooltip}
    </div>
  );
}
