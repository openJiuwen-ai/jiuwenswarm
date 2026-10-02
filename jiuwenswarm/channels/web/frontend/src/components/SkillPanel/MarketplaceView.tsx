import { catalogAwaitingItems, type CatalogCacheMetadata } from '../../features/catalogCache';
/**
 * 技能广场视图（默认列表 / 「更多」专页）
 *
 * 广场技能详情页由 SkillDetailView（mode 'hub'）渲染。
 */
import { useTranslation } from 'react-i18next';
import { ChevronRight, Loader2 } from 'lucide-react';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import { CategoryTabs, type PageCardActionProps } from '../ui';
import { MARKETPLACE_CATEGORIES, hubMarketplaceItemKey } from './skillPanelUtils';
import { HubSkillCard } from './SkillPanelWidgets';
import type { MarketplaceSubView } from './useHubMarketplace';
import type { MarketplacePluginItem } from './types';

interface MarketplaceViewProps {
  marketplaceSubView: MarketplaceSubView;
  teamSkills: MarketplacePluginItem[];
  featuredSkills: MarketplacePluginItem[];
  skillPacks: MarketplacePluginItem[];
  hubTeamMore: MarketplacePluginItem[];
  hubSkillMore: MarketplacePluginItem[];
  hubSkills: MarketplacePluginItem[];
  hubLoading: boolean;
  hubCache?: CatalogCacheMetadata;
  hubMoreLoading: boolean;
  searchKeyword: string;
  marketplaceCategory: (typeof MARKETPLACE_CATEGORIES)[number];
  onSelectHubSkill: (skill: MarketplacePluginItem) => void;
  renderHubSkillAction: (skill: MarketplacePluginItem) => PageCardActionProps;
  onCategoryChange: (nextCategory: (typeof MARKETPLACE_CATEGORIES)[number]) => void;
  onOpenMore: (kind: 'swarmskill' | 'skill') => void;
  onOpenAllPacks: () => void;
  onBackFromMore: () => void;
}

export function MarketplaceView({
  marketplaceSubView,
  teamSkills,
  featuredSkills,
  skillPacks,
  hubTeamMore,
  hubSkillMore,
  hubSkills,
  hubLoading,
  hubCache,
  hubMoreLoading,
  searchKeyword,
  marketplaceCategory,
  onSelectHubSkill,
  renderHubSkillAction,
  onCategoryChange,
  onOpenMore,
  onOpenAllPacks,
  onBackFromMore,
}: MarketplaceViewProps) {
  const { t } = useTranslation();

  if (!searchKeyword && (marketplaceSubView === 'team' || marketplaceSubView === 'skill')) {
    const moreItems = marketplaceSubView === 'team' ? hubTeamMore : hubSkillMore;
    return (
      <div
        data-testid={
          marketplaceSubView === 'team' ? 'skill-panel-team-skills-page' : 'skill-panel-featured-skills-page'
        }
        className="mt-4 flex-1 flex flex-col min-h-0"
      >
        <button
          type="button"
          className="detail-back"
          onClick={onBackFromMore}
          data-testid={
            marketplaceSubView === 'team' ? 'skill-panel-team-skills-back-btn' : 'skill-panel-featured-skills-back-btn'
          }
        >
          <BackIcon aria-hidden="true" />
          {t('agentManagement.actions.back')}
        </button>

        <div className="page-scroll flex-1 min-h-0 overflow-y-auto">
          <div className="flex items-center justify-between mb-3">
            <span
              data-testid={
                marketplaceSubView === 'team'
                  ? 'skill-panel-team-skills-title'
                  : 'skill-panel-featured-skills-more-title'
              }
              className="font-bold text-text-strong"
              style={{ fontSize: '16px' }}
            >
              {marketplaceSubView === 'team' ? t('skills.featuredTeamSkills') : t('skills.featuredSkills')}
            </span>
          </div>
          {hubMoreLoading || catalogAwaitingItems(moreItems.length, hubCache) ? (
            <div
              className="flex flex-1 min-h-[200px] items-center justify-center"
              role="status"
              aria-label={t('common.loading')}
              data-testid="skill-panel-hub-more-loading"
            >
              <Loader2 size={28} className="animate-spin text-text-muted" aria-hidden="true" />
            </div>
          ) : moreItems.length === 0 ? (
            <div className="text-sm text-text-muted" data-testid="skill-panel-hub-more-empty">
              {t('skills.noMatches')}
            </div>
          ) : (
            <div className="card-grid-auto">
              {moreItems.map((skill) => (
                <HubSkillCard
                  key={skill.asset_id}
                  skill={skill}
                  onSelect={() => onSelectHubSkill(skill)}
                  action={renderHubSkillAction(skill)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-1 flex-col min-h-0">
      {!searchKeyword ? (
        <div className="page-shell">
          <CategoryTabs
            items={MARKETPLACE_CATEGORIES.map((cat) => ({
              value: cat,
              label: t(`skills.marketplaceCategories.${cat}`),
            }))}
            value={marketplaceCategory}
            onChange={onCategoryChange}
          />
        </div>
      ) : null}

      {hubLoading ||
      catalogAwaitingItems(
        searchKeyword ? hubSkills.length : teamSkills.length + featuredSkills.length + skillPacks.length,
        hubCache,
      ) ? (
        <div
          className="flex flex-1 min-h-0 items-center justify-center"
          role="status"
          aria-label={t('common.loading')}
          data-testid="skill-panel-hub-list-loading"
        >
          <Loader2 size={28} className="animate-spin text-text-muted" aria-hidden="true" />
        </div>
      ) : searchKeyword ? (
        hubSkills.length === 0 ? (
          <div className="page-shell mt-4 text-sm text-text-muted" data-testid="skill-panel-hub-list-empty">
            {t('skills.noMatches')}
          </div>
        ) : (
          <div className="page-scroll flex-1 min-h-0 overflow-y-auto">
            <div className="card-grid-auto">
              {hubSkills.map((skill) => (
                <HubSkillCard
                  key={hubMarketplaceItemKey(skill)}
                  skill={skill}
                  onSelect={() => onSelectHubSkill(skill)}
                  action={renderHubSkillAction(skill)}
                />
              ))}
            </div>
          </div>
        )
      ) : teamSkills.length === 0 && featuredSkills.length === 0 && skillPacks.length === 0 ? (
        <div className="page-shell mt-4 text-sm text-text-muted" data-testid="skill-panel-hub-list-empty">
          {t('skills.noMatches')}
        </div>
      ) : (
        <div className="page-scroll flex-1 min-h-0 overflow-y-auto">
          {skillPacks.length > 0 && (
            <>
              <div className="flex items-center justify-between mb-3">
                <span className="font-bold text-text-strong" style={{ fontSize: '16px' }}>
                  {t('skills.featuredSkillPacks')}
                </span>
                {skillPacks.length > 3 && (
                  <button
                    type="button"
                    onClick={onOpenAllPacks}
                    className="flex items-center gap-0.5 text-sm text-text"
                  >
                    {t('nav.more')}
                    <ChevronRight size={16} aria-hidden="true" />
                  </button>
                )}
              </div>
              <div className="card-grid-auto mb-6">
                {skillPacks.slice(0, 3).map((skill) => (
                  <HubSkillCard
                    key={skill.asset_id}
                    skill={skill}
                    onSelect={() => onSelectHubSkill(skill)}
                    action={renderHubSkillAction(skill)}
                  />
                ))}
              </div>
            </>
          )}
          {teamSkills.length > 0 && (
            <>
              <div className="flex items-center justify-between mb-3">
                <span
                  data-testid="skill-panel-featured-skills-title"
                  className="font-bold text-text-strong"
                  style={{ fontSize: '16px' }}
                >
                  {t('skills.featuredTeamSkills')}
                </span>
                <button
                  type="button"
                  onClick={() => onOpenMore('swarmskill')}
                  className="flex items-center gap-0.5 text-sm text-text"
                  data-testid="skill-panel-team-skills-more-btn"
                >
                  {t('nav.more')}
                  <ChevronRight size={16} aria-hidden="true" />
                </button>
              </div>
              <div className="card-grid-auto mb-6">
                {teamSkills.map((skill) => (
                  <HubSkillCard
                    key={skill.asset_id}
                    skill={skill}
                    onSelect={() => onSelectHubSkill(skill)}
                    action={renderHubSkillAction(skill)}
                  />
                ))}
              </div>
            </>
          )}

          {featuredSkills.length > 0 && (
            <>
              <div className="flex items-center justify-between mb-3">
                <span className="font-bold text-text-strong" style={{ fontSize: '16px' }}>
                  {t('skills.featuredSkills')}
                </span>
                <button
                  type="button"
                  onClick={() => onOpenMore('skill')}
                  className="flex items-center gap-0.5 text-sm text-text"
                  data-testid="skill-panel-featured-skills-more-btn"
                >
                  {t('nav.more')}
                  <ChevronRight size={16} aria-hidden="true" />
                </button>
              </div>
              <div className="card-grid-auto">
                {featuredSkills.map((skill) => (
                  <HubSkillCard
                    key={skill.asset_id}
                    skill={skill}
                    onSelect={() => onSelectHubSkill(skill)}
                    action={renderHubSkillAction(skill)}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** 全部技能包专页 */
export function SkillPacksView({
  skillPacks,
  onBack,
  onSelectHubSkill,
  renderHubSkillAction,
}: {
  skillPacks: MarketplacePluginItem[];
  onBack: () => void;
  onSelectHubSkill: (skill: MarketplacePluginItem) => void;
  renderHubSkillAction: (skill: MarketplacePluginItem) => PageCardActionProps;
}) {
  const { t } = useTranslation();
  return (
    <div className="mt-4 flex-1 flex flex-col min-h-0" data-testid="skill-panel-all-packs-page">
      <button type="button" className="detail-back" onClick={onBack}>
        <BackIcon aria-hidden="true" />
        {t('agentManagement.actions.back')}
      </button>
      <div className="page-scroll flex-1 min-h-0 overflow-y-auto">
        <div className="flex items-center justify-between mb-3">
          <span className="font-bold text-text-strong" style={{ fontSize: '16px' }}>
            {t('skills.allSkillPacks')}
          </span>
        </div>
        {skillPacks.length === 0 ? (
          <div className="text-sm text-text-muted">{t('skills.noMatches')}</div>
        ) : (
          <div className="card-grid-auto">
            {skillPacks.map((skill) => (
              <HubSkillCard
                key={skill.asset_id}
                skill={skill}
                onSelect={() => onSelectHubSkill(skill)}
                action={renderHubSkillAction(skill)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
