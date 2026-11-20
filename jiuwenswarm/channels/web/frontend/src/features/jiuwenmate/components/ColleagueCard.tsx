import { useRef, type ReactNode } from 'react';
import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import { colleaguePortrait, customAvatarHue } from '../colleaguePortraits';
import type { JiuwenMateColleagueDraft } from '../types';

type ColleagueCardSubject = Pick<
  JiuwenMateColleagueDraft,
  'name' | 'description' | 'tags' | 'category' | 'avatarUrl' | 'avatarHue'
> & {
  id: string;
  isDefault?: boolean;
};

interface ColleagueCardProps<TSubject extends ColleagueCardSubject> {
  colleague: TSubject;
  selected?: boolean;
  onSelect?: (colleague: TSubject) => void;
  revealClippedDescription?: boolean;
  badge?: { label: string; tone: 'warning' | 'muted' };
  menu?: ReactNode;
  actions?: ReadonlyArray<{
    label: string;
    primary?: boolean;
    onClick: (colleague: TSubject) => void;
  }>;
}

export function ColleagueCard<TSubject extends ColleagueCardSubject>({
  colleague,
  selected = false,
  onSelect,
  revealClippedDescription = false,
  badge,
  menu,
  actions = [],
}: ColleagueCardProps<TSubject>) {
  const hue = customAvatarHue(colleague);
  const cardRef = useRef<HTMLElement>(null);
  const descriptionRef = useRef<HTMLSpanElement>(null);
  const { tooltip, handlers } = useAdaptiveTooltip({
    anchorRef: descriptionRef,
    topFallbackAnchorRef: cardRef,
    placement: 'bottom',
    align: 'left',
    offsetX: -22,
    offsetY: -3,
  });
  const tooltipText = revealClippedDescription ? colleague.description : undefined;
  const descriptionIsClipped = () => {
    const description = descriptionRef.current;
    return tooltipText && description && (
      description.scrollHeight > description.clientHeight || description.scrollWidth > description.clientWidth
    );
  };

  return (
    <article
      ref={cardRef}
      className={`jiuwenmate-colleague-card${selected ? ' jiuwenmate-colleague-card--selected' : ''}`}
      data-testid="jiuwenmate-colleague-card"
      data-variant={colleague.id}
      data-category={colleague.category}
      data-hue={hue ?? undefined}
      style={hue === null ? undefined : { ['--jm-avatar-hue' as string]: `${hue}deg` }}
    >
      <button
        type="button"
        className="jiuwenmate-colleague-card__select"
        aria-pressed={selected}
        data-tooltip={tooltipText}
        onFocus={(event) => {
          if (descriptionIsClipped()) handlers.onFocus(event);
        }}
        onBlur={revealClippedDescription ? handlers.onBlur : undefined}
        onClick={() => onSelect?.(colleague)}
      >
        <span className="jiuwenmate-colleague-card__title">
          <strong data-testid="jiuwenmate-colleague-name">{colleague.name}</strong>
          {badge ? <em data-tone={badge.tone}>{badge.label}</em> : null}
        </span>
        <span
          ref={descriptionRef}
          className="jiuwenmate-colleague-card__description"
          data-testid="jiuwenmate-colleague-description"
          data-tooltip={tooltipText}
          onMouseEnter={(event) => {
            if (descriptionIsClipped()) handlers.onMouseEnter(event);
          }}
          onMouseLeave={revealClippedDescription ? handlers.onMouseLeave : undefined}
        >
          {colleague.description}
        </span>
        <span className="jiuwenmate-colleague-card__tags">
          {colleague.tags.map((tag) => (
            <em key={tag}>{tag}</em>
          ))}
        </span>
        <span className="jiuwenmate-colleague-card__art" aria-hidden>
          <span className="jiuwenmate-colleague-card__halo" />
          <img
            src={colleaguePortrait(colleague)}
            alt=""
            data-testid="jiuwenmate-colleague-avatar"
            data-hue={hue ?? undefined}
            style={hue === null ? undefined : { ['--jm-avatar-hue' as string]: `${hue}deg` }}
          />
        </span>
      </button>
      {tooltip}
      {menu ? <div className="jiuwenmate-colleague-card__menu">{menu}</div> : null}
      {actions.length > 0 ? (
        <div className="jiuwenmate-colleague-card__actions">
          {actions.map((action) => (
            <button
              key={action.label}
              type="button"
              className={action.primary ? 'jiuwenmate-colleague-card__action--primary' : ''}
              onClick={() => action.onClick(colleague)}
            >
              {action.label}
            </button>
          ))}
        </div>
      ) : null}
    </article>
  );
}
