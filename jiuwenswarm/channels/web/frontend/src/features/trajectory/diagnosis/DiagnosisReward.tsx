/** Reward evaluation: the overall comment and category scores, one category at a time opening onto its dimensions. */

import { useMemo, useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { RewardPayload } from './diagnosisTypes';
import { commentPoints, rewardOverview } from './diagnosisView';
import type { RewardCategoryScore, RewardDimensionScore } from './diagnosisView';
import css from './DiagnosisSubpage.module.css';

function categoryLabel(category: RewardCategoryScore): string {
  return category.score === null ? category.name : `${category.name} ${category.score}`;
}

/** The evaluator's comment as the points it makes: a bullet list when there are several. */
function CommentPoints({ text }: { text: string }) {
  const points = commentPoints(text);
  if (points.length === 0) return null;
  if (points.length === 1) return <p className={css.rewardDimComment}>{points[0]}</p>;
  return (
    <ul className={css.rewardDimPoints}>
      {points.map((point, index) => (
        <li key={`${index}:${point}`}>{point}</li>
      ))}
    </ul>
  );
}

function DimensionCard({ dimension }: { dimension: RewardDimensionScore }) {
  return (
    <div
      className={`${css.rewardDim} ${css[`rewardDim_${dimension.tier}`]}`}
      data-testid="trajectory-diagnosis-reward-dimension"
    >
      <div className={css.rewardDimHead}>
        <span className={`${css.tag} ${css[`tier_${dimension.tier}`]}`}>{dimension.score}</span>
        <p className={css.rewardDimRequirement}>{dimension.requirement}</p>
      </div>
      <CommentPoints text={dimension.comment} />
    </div>
  );
}

function CategoryDetail({ category }: { category: RewardCategoryScore }) {
  return (
    <div className={css.rewardDetail} data-testid="trajectory-diagnosis-reward-category-detail">
      {category.comment === '' ? null : <p className={css.rewardComment}>{category.comment}</p>}
      {category.dimensions.map((dimension, index) => (
        <DimensionCard key={`${index}:${dimension.requirement}`} dimension={dimension} />
      ))}
    </div>
  );
}

/**
 * The overall comment, the score of every category and how many dimensions were scored.
 *
 * One category opens at a time onto its comment and the requirement, score and comment of each of
 * its dimensions; the ones that fell short are also the signals behind the findings.
 */
export function RewardSummary({ reward }: { reward: RewardPayload }) {
  const { t } = useTranslation();
  const overview = useMemo(() => rewardOverview(reward), [reward]);
  const [openName, setOpenName] = useState<string | null>(null);
  if (overview.comment === '' && overview.categories.length === 0 && overview.dimensionCount === 0) return null;
  const opened = overview.categories.find(category => category.name === openName) ?? null;
  return (
    <div className={`${css.block} ${css.rewardSummary}`} data-testid="trajectory-diagnosis-reward-overall">
      <h4 className={css.blockTitle}>{t('trajectory.diagnosis.rewardResult')}</h4>
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
            const expanded = category.name === openName;
            const Chevron = expanded ? ChevronDown : ChevronRight;
            return (
              <button
                key={category.name}
                type="button"
                className={`${css.tag} ${css.tagButton} ${tone}`}
                aria-expanded={expanded}
                onClick={() => setOpenName(expanded ? null : category.name)}
                data-testid="trajectory-diagnosis-reward-category-score"
              >
                {categoryLabel(category)}
                <Chevron size={12} strokeWidth={2} aria-hidden />
              </button>
            );
          })}
        </div>
      )}
      {opened === null ? null : <CategoryDetail category={opened} />}
      {overview.dimensionCount === 0 ? null : (
        <p className={css.rewardComment} data-testid="trajectory-diagnosis-reward-dimensions">
          {t('trajectory.diagnosis.rewardDimensions', {
            count: overview.dimensionCount,
            perfect: overview.perfectCount,
          })}
        </p>
      )}
    </div>
  );
}
