/**
 * 技能广场视图（默认列表）
 *
 * 广场技能详情页由 SkillDetailView（mode 'hub'）渲染。
 */
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { CategoryTabs, type PageCardActionProps } from '../ui';
import { MARKETPLACE_CATEGORIES } from './skillPanelUtils';
import { ExpandableCardSection, HubSkillCard } from './SkillPanelWidgets';
import type { MarketplacePluginItem } from './types';

interface MarketplaceViewProps {
  teamSkills: MarketplacePluginItem[];
  featuredSkills: MarketplacePluginItem[];
  hubSkills: MarketplacePluginItem[];
  hubLoading: boolean;
  searchKeyword: string;
  marketplaceCategory: (typeof MARKETPLACE_CATEGORIES)[number];
  onSelectHubSkill: (skill: MarketplacePluginItem) => void;
  renderHubSkillAction: (skill: MarketplacePluginItem) => PageCardActionProps;
  onCategoryChange: (nextCategory: (typeof MARKETPLACE_CATEGORIES)[number]) => void;
}

export function MarketplaceView({
  teamSkills,
  featuredSkills,
  hubSkills,
  hubLoading,
  searchKeyword,
  marketplaceCategory,
  onSelectHubSkill,
  renderHubSkillAction,
  onCategoryChange,
}: MarketplaceViewProps) {
  const { t } = useTranslation();
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

      {hubLoading ? (
        <div
          className="flex flex-1 min-h-0 items-center justify-center"
          role="status"
          aria-label={t('common.loading')}
          data-testid="skill-panel-hub-list-loading"
        >
          <Loader2 size={28} className="animate-spin text-text-muted" aria-hidden="true" />
        </div>
      ) : hubSkills.length === 0 ? (
        <div className="page-shell mt-4 text-sm text-text-muted" data-testid="skill-panel-hub-list-empty">
          {t('skills.noMatches')}
        </div>
      ) : searchKeyword ? (
        /* 搜索结果：全部罗列 */
        <div className="page-scroll mt-4 flex-1 min-h-0 overflow-y-auto">
          <div className="card-grid-auto">
            {hubSkills.map((skill) => (
              <HubSkillCard
                key={skill.asset_id}
                skill={skill}
                onSelect={() => onSelectHubSkill(skill)}
                action={renderHubSkillAction(skill)}
              />
            ))}
          </div>
        </div>
      ) : (
        /* 无搜索词：按 plugin_type 分组展示 */
        <div className="page-scroll mt-4 flex-1 min-h-0 overflow-y-auto">
          {/* 精选团队技能：默认 6 张，超出可"更多/收起"展开 */}
          {teamSkills.length > 0 && (
            <ExpandableCardSection
              title={t('skills.featuredTeamSkills')}
              items={teamSkills}
              titleTestId="skill-panel-featured-skills-title"
              toggleTestId="skill-panel-team-skills-more-btn"
              gridClassName="card-grid-auto mb-6"
              renderItem={(skill) => (
                <HubSkillCard
                  key={skill.asset_id}
                  skill={skill}
                  onSelect={() => onSelectHubSkill(skill)}
                  action={renderHubSkillAction(skill)}
                />
              )}
            />
          )}

          {/* 精选技能：默认 6 张，超出可"更多/收起"展开 */}
          {featuredSkills.length > 0 && (
            <ExpandableCardSection
              title={t('skills.featuredSkills')}
              items={featuredSkills}
              toggleTestId="skill-panel-featured-skills-more-btn"
              renderItem={(skill) => (
                <HubSkillCard
                  key={skill.asset_id}
                  skill={skill}
                  onSelect={() => onSelectHubSkill(skill)}
                  action={renderHubSkillAction(skill)}
                />
              )}
            />
          )}
        </div>
      )}
    </div>
  );
}
