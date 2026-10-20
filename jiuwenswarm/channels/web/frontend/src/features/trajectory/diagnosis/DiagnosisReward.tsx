/** Reward evaluation: the overall comment and category scores, each category opening onto its dimensions. */

import { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { RewardPayload } from './diagnosisTypes';
import { rewardOverview } from './diagnosisView';
import type { RewardCategoryScore } from './diagnosisView';
import { PlainFold } from './DiagnosisText';
import css from './DiagnosisSubpage.module.css';

function categoryLabel(category: RewardCategoryScore): string {
  return category.score === null ? category.name : `${category.name} ${category.score}`;
}

function CategoryDetail({ category }: { category: RewardCategoryScore }) {
  return (
    <div className={css.rewardDetail} data-testid="trajectory-diagnosis-reward-category-detail">
      {category.comment === '' ? null : <p className={css.rewardComment}>{category.comment}</p>}
      {category.dimensions.map((dimension, index) => (
        <div key={`${index}:${dimension.requirement}`} className={css.rewardDim} data-testid="trajectory-diagnosis-reward-dimension">
          <div className={css.rewardDimHead}>
            <span className={`${css.tag} ${css[`tier_${dimension.tier}`]}`}>{dimension.score}</span>
            <p className={css.rewardDimRequirement}>{dimension.requirement}</p>
          </div>
          <PlainFold text={dimension.comment} />
        </div>
      ))}
    </div>
  );
}

/**
 * The overall comment, the score of every category and how many dimensions were scored.
 *
 * A category opens onto its comment and the requirement, score and comment of each of its
 * dimensions; the ones that fell short are also the signals behind the findings.
 */
export function RewardSummary({ reward }: { reward: RewardPayload }) {
  const { t } = useTranslation();
  const overview = useMemo(() => rewardOverview(reward), [reward]);
  const [openNames, setOpenNames] = useState<ReadonlySet<string>>(new Set());
  if (overview.comment === '' && overview.categories.length === 0 && overview.dimensionCount === 0) return null;
  const toggle = (name: string) => setOpenNames((current) => {
    const next = new Set(current);
    if (!next.delete(name)) next.add(name);
    return next;
  });
  return (
    <div className={css.rewardSummary} data-testid="trajectory-diagnosis-reward-overall">
      {overview.comment === '' ? null : <p className={css.rewardComment}>{overview.comment}</p>}
      {overview.categories.length === 0 ? null : (
        <div className={css.row}>
          {overview.categories.map((category) => {
            const tone = category.tier === null ? '' : css[`tier_${category.tier}`];
            const expandable = category.comment !== '' || category.dimensions.length > 0;
            if (!expandable) {
              return (
                <span
                  key={category.name}
                  className={`${css.tag} ${tone}`}
                  data-testid="trajectory-diagnosis-reward-category-score"
                >
                  {categoryLabel(category)}
                </span>
              );
            }
            const expanded = openNames.has(category.name);
            const Chevron = expanded ? ChevronDown : ChevronRight;
            return (
              <button
                key={category.name}
                type="button"
                className={`${css.tag} ${css.tagButton} ${tone}`}
                aria-expanded={expanded}
                onClick={() => toggle(category.name)}
                data-testid="trajectory-diagnosis-reward-category-score"
              >
                {categoryLabel(category)}
                <Chevron size={12} strokeWidth={2} aria-hidden />
              </button>
            );
          })}
        </div>
      )}
      {overview.categories.filter(category => openNames.has(category.name)).map(category => (
        <CategoryDetail key={category.name} category={category} />
      ))}
      {overview.dimensionCount === 0 ? null : (
        <p className={css.hint} data-testid="trajectory-diagnosis-reward-dimensions">
          {t('trajectory.diagnosis.rewardDimensions', {
            count: overview.dimensionCount,
            perfect: overview.perfectCount,
          })}
        </p>
      )}
    </div>
  );
}
