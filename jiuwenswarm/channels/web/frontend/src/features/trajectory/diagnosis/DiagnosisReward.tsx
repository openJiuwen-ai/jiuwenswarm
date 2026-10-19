/** Reward evaluation: the score summary of the conclusion, and the folded details behind it. */

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import type { RewardPayload } from './diagnosisTypes';
import { rewardDimensionGroups, rewardOverview, type RewardDimensionView } from './diagnosisView';
import css from './DiagnosisSubpage.module.css';

/** The overall comment and the score of every category; the total is part of the headline. */
export function RewardSummary({ reward }: { reward: RewardPayload }) {
  const overview = useMemo(() => rewardOverview(reward), [reward]);
  if (overview.comment === '' && overview.categories.length === 0) return null;
  return (
    <div className={css.rewardSummary} data-testid="trajectory-diagnosis-reward-overall">
      {overview.comment === '' ? null : <p className={css.rewardComment}>{overview.comment}</p>}
      {overview.categories.length === 0 ? null : (
        <div className={css.row}>
          {overview.categories.map(category => (
            <span
              key={category.name}
              className={`${css.tag} ${css[`tier_${category.tier}`]}`}
              data-testid="trajectory-diagnosis-reward-category-score"
            >
              {category.name} {category.score}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function DimensionRow({ dimension }: { dimension: RewardDimensionView }) {
  const { t } = useTranslation();
  return (
    <li className={css.dimension} data-testid="trajectory-diagnosis-reward-dimension" data-tier={dimension.tier}>
      <div className={css.row}>
        <span className={`${css.tag} ${css[`tier_${dimension.tier}`]}`}>
          {t('trajectory.diagnosis.signalScore', { score: dimension.score })}
        </span>
        <strong>{dimension.requirement}</strong>
      </div>
      {dimension.comment === '' ? null : <p className={css.hint}>{dimension.comment}</p>}
    </li>
  );
}

/** Every category with its dimensions, folded: the findings that need attention are already root causes. */
export function RewardDetails({ reward }: { reward: RewardPayload }) {
  const { t } = useTranslation();
  const overview = useMemo(() => rewardOverview(reward), [reward]);
  const groups = useMemo(() => {
    const result = rewardDimensionGroups(reward);
    // A category the evaluator scored without listing dimensions still shows its score and comment.
    for (const category of reward.categories) {
      if (!result.some(group => group.category === category.name)) {
        result.push({ category: category.name, dimensions: [] });
      }
    }
    return result;
  }, [reward]);
  return (
    <details className={css.sectionFold} data-testid="trajectory-diagnosis-reward">
      <summary data-testid="trajectory-diagnosis-reward-toggle">
        {t('trajectory.diagnosis.rewardDetails', {
          categories: overview.categoryCount,
          dimensions: overview.dimensionCount,
        })}
      </summary>
      <div className={css.foldBody}>
        {groups.map((group) => {
          const category = reward.categories.find(item => item.name === group.category);
          return (
            <section className={css.rewardGroup} key={group.category} data-testid="trajectory-diagnosis-reward-category">
              <header className={css.row}>
                <strong>{group.category}</strong>
                {category?.score === undefined ? null : (
                  <span className={css.hint}>{t('trajectory.diagnosis.signalScore', { score: category.score })}</span>
                )}
              </header>
              {category?.comment === undefined || category.comment === '' ? null : (
                <p className={css.hint}>{category.comment}</p>
              )}
              {group.dimensions.length === 0 ? null : (
                <ul className={css.dimensions}>
                  {group.dimensions.map((dimension, index) => (
                    <DimensionRow key={`${dimension.requirement}-${index}`} dimension={dimension} />
                  ))}
                </ul>
              )}
            </section>
          );
        })}
        <details className={css.rewardRaw}>
          <summary data-testid="trajectory-diagnosis-reward-raw-toggle">{t('trajectory.diagnosis.rewardRaw')}</summary>
          <pre className={css.output} data-testid="trajectory-diagnosis-reward-raw">
            {JSON.stringify(reward, null, 2)}
          </pre>
        </details>
      </div>
    </details>
  );
}
