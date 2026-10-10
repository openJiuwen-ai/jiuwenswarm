import type { ReactNode } from 'react';
import { colleaguePortrait } from '../colleaguePortraits';
import type { JiuwenMateColleagueDraft } from '../types';

type ColleagueCardSubject = Pick<JiuwenMateColleagueDraft, 'name' | 'description' | 'tags' | 'category' | 'avatarUrl'> & {
  id: string;
};

interface ColleagueCardProps<TSubject extends ColleagueCardSubject> {
  colleague: TSubject;
  selected?: boolean;
  onSelect?: (colleague: TSubject) => void;
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
  badge,
  menu,
  actions = [],
}: ColleagueCardProps<TSubject>) {
  return (
    <article
      className={`jiuwenmate-colleague-card${selected ? ' jiuwenmate-colleague-card--selected' : ''}`}
      data-testid="jiuwenmate-colleague-card"
      data-variant={colleague.id}
      data-category={colleague.category}
    >
      <button
        type="button"
        className="jiuwenmate-colleague-card__select"
        aria-pressed={selected}
        onClick={() => onSelect?.(colleague)}
      >
        <span className="jiuwenmate-colleague-card__title">
          <strong data-testid="jiuwenmate-colleague-name">{colleague.name}</strong>
          {badge ? <em data-tone={badge.tone}>{badge.label}</em> : null}
        </span>
        <span className="jiuwenmate-colleague-card__description">{colleague.description}</span>
        <span className="jiuwenmate-colleague-card__tags">
          {colleague.tags.map((tag) => (
            <em key={tag}>{tag}</em>
          ))}
        </span>
        <span className="jiuwenmate-colleague-card__art" aria-hidden>
          <span className="jiuwenmate-colleague-card__halo" />
          <img src={colleaguePortrait(colleague)} alt="" data-testid="jiuwenmate-colleague-avatar" />
        </span>
      </button>
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
