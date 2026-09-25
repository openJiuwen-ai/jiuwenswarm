import {
  scheduleCatalogRefresh,
  catalogScope,
  withCatalogCache,
  type CatalogCacheMetadata,
} from '../../features/catalogCache';
/**
 * 技能广场（SkillHub 推荐 / 在线搜索 / 广场详情）数据 hook
 *
 * 首页按类型各拉 HUB_HOME_TOP_K；「更多」专页按需拉 HUB_MORE_TOP_K。
 * 离开页签保留首页/更多列表；再进入同分类时 stale-while-revalidate（先展示旧数据再静默刷新）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { webRequest } from '../../services/webClient';
import type { HubSkillDetail, LoadState, MarketplacePluginItem } from './types';

/** 技能广场首页：团队 / 普通技能各拉取条数 */
export const HUB_HOME_TOP_K = 6;
/** 技能广场「更多」专页：单类型最多拉取条数 */
export const HUB_MORE_TOP_K = 50;

type HubRecommendSkill = {
  asset_id: string;
  name: string;
  display_name?: string;
  summary?: string;
  short_desc?: string;
  version?: string;
  latest_version?: string;
  plugin_type?: string;
  /** 技能包标记（推荐条目允许返回 skillpack） */
  skill_type?: string;
  tags?: string[];
  publisher_name?: string;
  install_count?: number;
  like_count?: number;
  view_count?: number;
  icon_uri?: string;
};

function mapRecommendSkill(s: HubRecommendSkill): MarketplacePluginItem {
  return {
    asset_id: s.asset_id,
    name: s.name,
    display_name: s.display_name || s.name,
    short_desc: s.short_desc || s.summary || '',
    publisher_name: s.publisher_name || '',
    install_count: s.install_count ?? 0,
    like_count: s.like_count ?? 0,
    view_count: s.view_count ?? 0,
    plugin_type: s.plugin_type || null,
    skill_type: s.skill_type || null,
    tags: s.tags || null,
    latest_version: s.latest_version || s.version || null,
    icon_uri: s.icon_uri || null,
  };
}

export type WithSessionFn = <T extends Record<string, unknown> = Record<string, unknown>>(
  params?: T,
) => T & { session_id: string };

export type SkillPanelTab = 'my' | 'marketplace' | 'graph';
export type MarketplaceSubView = 'list' | 'team' | 'skill' | 'packs' | 'detail';

interface UseHubMarketplaceParams {
  activeTab: SkillPanelTab;
  searchKeyword: string;
  marketplaceCategory: string;
  setMarketplaceSubView: (view: MarketplaceSubView) => void;
  withSession: WithSessionFn;
}

export function useHubMarketplace({
  activeTab,
  searchKeyword,
  marketplaceCategory,
  setMarketplaceSubView,
  withSession,
}: UseHubMarketplaceParams) {
  /** 搜索结果（online_search） */
  const [hubSkills, setHubSkills] = useState<MarketplacePluginItem[]>([]);
  const [hubCache, setHubCache] = useState<CatalogCacheMetadata>();
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      hubFetchSeqRef.current += 1;
      hubMoreFetchSeqRef.current += 1;
    };
  }, []);
  /** 首页推荐：团队 / 普通技能各最多 HUB_HOME_TOP_K（不再单独请求技能包推荐，
      技能包区块仅展示两路推荐结果中捎带返回的 skillpack 条目） */
  const [hubTeamHome, setHubTeamHome] = useState<MarketplacePluginItem[]>([]);
  const [hubSkillHome, setHubSkillHome] = useState<MarketplacePluginItem[]>([]);
  /** 「更多」专页：单类型最多 HUB_MORE_TOP_K */
  const [hubTeamMore, setHubTeamMore] = useState<MarketplacePluginItem[]>([]);
  const [hubSkillMore, setHubSkillMore] = useState<MarketplacePluginItem[]>([]);
  const [hubLoading, setHubLoading] = useState(false);
  const [hubMoreLoading, setHubMoreLoading] = useState(false);
  /** 更多列表已加载的分类 + 类型，避免同分类重复请求 */
  const [hubMoreLoadedFor, setHubMoreLoadedFor] = useState<{
    category: string;
    team: boolean;
    skill: boolean;
  } | null>(null);
  /** 技能广场请求序号：防抖搜索/分类切换时丢弃过期响应 */
  const hubFetchSeqRef = useRef(0);
  const hubMoreFetchSeqRef = useRef(0);
  /** 当前首页列表对应的分类；与请求分类一致时可静默刷新 */
  const hubHomeLoadedCategoryRef = useRef<string | null>(null);
  const [selectedHubSkill, setSelectedHubSkill] = useState<MarketplacePluginItem | null>(null);
  const [hubDetail, setHubDetail] = useState<HubSkillDetail | null>(null);
  const [hubDetailState, setHubDetailState] = useState<LoadState>('idle');

  const fetchHubRecommendByType = useCallback(
    async (category: string, pluginType: 'swarmskill' | 'skill', topK: number) => {
      const params = withSession({
        top_k: topK,
        cache_mode: 'prefer_cache',
        plugin_type: pluginType,
        ...(category !== 'all' ? { category_id: category } : {}),
      });
      const data = await webRequest<{
        success: boolean;
        skills?: HubRecommendSkill[];
        cache?: CatalogCacheMetadata;
        detail?: string;
      }>('skills.swarmskillshub.recommend', params, { timeoutMs: 30000 });
      if (!data.success) throw new Error(data.detail || 'Recommend failed');
      return withCatalogCache((data.skills || []).map(mapRecommendSkill), data.cache);
    },
    [withSession],
  );

  const fetchHubHomeSkills = useCallback(
    async (category: string) => {
      const seq = ++hubFetchSeqRef.current;
      const requestScope = catalogScope();
      const silent = hubHomeLoadedCategoryRef.current === category;
      if (!silent) {
        setHubLoading(true);
        setHubTeamMore([]);
        setHubSkillMore([]);
        setHubMoreLoadedFor(null);
      }
      try {
        const [teamResult, skillResult] = await Promise.allSettled([
          fetchHubRecommendByType(category, 'swarmskill', HUB_HOME_TOP_K),
          fetchHubRecommendByType(category, 'skill', HUB_HOME_TOP_K),
        ]);
        if (seq !== hubFetchSeqRef.current) return;

        if (requestScope !== catalogScope()) return;
        const caches = [teamResult, skillResult].flatMap((result) =>
          result.status === 'fulfilled' && result.value.cache ? [result.value.cache] : [],
        );
        const cache = caches.find((value) => value.refreshing) || caches[0];
        setHubCache(cache);
        scheduleCatalogRefresh(
          'skill-recommend',
          cache,
          () => {
            void fetchHubHomeSkills(category);
          },
          () => mountedRef.current && seq === hubFetchSeqRef.current,
        );

        if (teamResult.status === 'fulfilled') {
          setHubTeamHome(teamResult.value);
        } else {
          console.error('Failed to fetch team SkillHub recommend:', teamResult.reason);
          if (!silent) setHubTeamHome([]);
        }

        if (skillResult.status === 'fulfilled') {
          setHubSkillHome(skillResult.value);
        } else {
          console.error('Failed to fetch skill SkillHub recommend:', skillResult.reason);
          if (!silent) setHubSkillHome([]);
        }

        if (teamResult.status === 'fulfilled' || skillResult.status === 'fulfilled') {
          hubHomeLoadedCategoryRef.current = category;
        } else if (!silent) {
          hubHomeLoadedCategoryRef.current = null;
        }
      } finally {
        if (seq === hubFetchSeqRef.current) setHubLoading(false);
      }
    },
    [fetchHubRecommendByType],
  );

  const fetchHubMoreSkills = useCallback(
    async (kind: 'swarmskill' | 'skill', category: string) => {
      const seq = ++hubMoreFetchSeqRef.current;
      const requestScope = catalogScope();
      setHubMoreLoading(true);
      try {
        const items = await fetchHubRecommendByType(category, kind, HUB_MORE_TOP_K);
        if (requestScope !== catalogScope() || seq !== hubMoreFetchSeqRef.current) return;
        setHubCache(items.cache);
        scheduleCatalogRefresh(
          'skill-more',
          items.cache,
          () => {
            void fetchHubMoreSkills(kind, category);
          },
          () => mountedRef.current && seq === hubMoreFetchSeqRef.current,
        );
        if (kind === 'swarmskill') {
          setHubTeamMore(items);
          setHubMoreLoadedFor((prev) =>
            prev && prev.category === category ? { ...prev, team: true } : { category, team: true, skill: false },
          );
        } else {
          setHubSkillMore(items);
          setHubMoreLoadedFor((prev) =>
            prev && prev.category === category ? { ...prev, skill: true } : { category, team: false, skill: true },
          );
        }
      } catch (error) {
        console.error(`Failed to fetch more ${kind} SkillHub recommend:`, error);
        if (requestScope !== catalogScope() || seq !== hubMoreFetchSeqRef.current) return;
        if (kind === 'swarmskill') setHubTeamMore([]);
        else setHubSkillMore([]);
      } finally {
        if (seq === hubMoreFetchSeqRef.current) setHubMoreLoading(false);
      }
    },
    [fetchHubRecommendByType],
  );

  const openHubMore = useCallback(
    (kind: 'swarmskill' | 'skill') => {
      setMarketplaceSubView(kind === 'swarmskill' ? 'team' : 'skill');
      const needFetch =
        !hubMoreLoadedFor ||
        hubMoreLoadedFor.category !== marketplaceCategory ||
        (kind === 'swarmskill' ? !hubMoreLoadedFor.team : !hubMoreLoadedFor.skill);
      if (needFetch) {
        void fetchHubMoreSkills(kind, marketplaceCategory);
      }
    },
    [fetchHubMoreSkills, hubMoreLoadedFor, marketplaceCategory, setMarketplaceSubView],
  );

  /** 进入"全部技能包"专页：不再单独请求技能包推荐，仅展示已有数据（首页两路推荐捎带 / 搜索结果） */
  const openHubAllPacks = useCallback(() => {
    setMarketplaceSubView('packs');
  }, [setMarketplaceSubView]);

  const fetchOnlineSearch = useCallback(
    async (query: string) => {
      const seq = ++hubFetchSeqRef.current;
      const requestScope = catalogScope();
      setHubLoading(true);
      try {
        const data = await webRequest<{
          success: boolean;
          cache?: CatalogCacheMetadata;
          partial?: boolean;
          items?: Array<{
            source: string;
            identifier: string;
            name: string;
            display_name: string;
            description: string;
            version: string;
            author: string;
            is_team_skill: boolean;
            /** 技能包标记（搜索结果允许返回 skillpack） */
            skill_type?: string;
            /** 技能包成员数量 */
            member_count?: number;
            native_score: number | null;
            category: string;
            updated_at: number;
            source_rank: number;
            fusion_score: number;
            exact_match: boolean;
            matched_source_count: number;
            owner_handle?: string;
          }>;
          sources?: Array<{
            source: string;
            status: 'success' | 'error' | 'skipped';
            count: number;
            detail?: string;
            detail_key?: string;
          }>;
          detail?: string;
        }>(
          'skills.online_search.search',
          withSession({
            q: query,
            cache_mode: 'prefer_cache',
            limit: 50,
          }),
          { timeoutMs: 45000 },
        );

        if (requestScope !== catalogScope() || seq !== hubFetchSeqRef.current) return;
        setHubCache(data.cache);
        scheduleCatalogRefresh(
          'skill-search',
          data.cache,
          () => {
            void fetchOnlineSearch(query);
          },
          () => mountedRef.current && seq === hubFetchSeqRef.current,
        );
        // success=false: 参数非法或所有来源均失败
        if (!data.success) {
          throw new Error(data.detail || 'Search failed');
        }

        if (data.partial) {
          const failedSources = (data.sources || []).filter((s) => s.status === 'error').map((s) => s.source);
          if (failedSources.length > 0) {
            console.warn('Partial search: sources failed:', failedSources);
          }
        }

        if (seq !== hubFetchSeqRef.current) return;
        const items: MarketplacePluginItem[] = (data.items || []).map((s) => ({
          asset_id: s.identifier,
          name: s.name,
          display_name: s.display_name || s.name,
          short_desc: s.description || '',
          publisher_name: s.author || '',
          install_count: s.native_score ?? 0,
          like_count: 0,
          view_count: 0,
          plugin_type: s.is_team_skill ? 'swarmskill' : 'skill',
          skill_type: s.skill_type || null,
          member_count: s.member_count ?? null,
          latest_version: s.version || null,
          source: s.source,
          identifier: s.identifier,
          owner_handle: s.owner_handle || null,
          native_score: s.native_score,
          category: s.category || null,
          updated_at: s.updated_at || null,
          exact_match: s.exact_match,
        }));
        setHubSkills(items);
      } catch (error) {
        console.error('Failed to fetch online search:', error);
        if (seq !== hubFetchSeqRef.current) return;
      } finally {
        if (seq === hubFetchSeqRef.current) setHubLoading(false);
      }
    },
    [withSession],
  );

  useEffect(() => {
    if (activeTab !== 'marketplace') return;

    if (!searchKeyword) {
      void fetchHubHomeSkills(marketplaceCategory);
      return;
    }

    const timer = window.setTimeout(() => {
      void fetchOnlineSearch(searchKeyword);
    }, 500);

    return () => window.clearTimeout(timer);
  }, [activeTab, marketplaceCategory, searchKeyword, fetchHubHomeSkills, fetchOnlineSearch]);

  // 分组：skillpack → 精选技能包，swarmskill → 精选团队技能，其余 → 精选技能
  // （搜索时取搜索结果；无搜索词时取首页两路推荐结果合并，保持互斥分桶；
  //   技能包不再单独请求，区块内容来自推荐/搜索结果中捎带的 skillpack 条目）
  const { teamSkills, featuredSkills, skillPacks } = useMemo(() => {
    const team: MarketplacePluginItem[] = [];
    const featured: MarketplacePluginItem[] = [];
    const packs: MarketplacePluginItem[] = [];
    const all = searchKeyword ? hubSkills : [...hubTeamHome, ...hubSkillHome];
    for (const skill of all) {
      if (skill.skill_type === 'skillpack' || skill.plugin_type === 'skillpack') {
        packs.push(skill);
      } else if (skill.plugin_type === 'swarmskill') {
        team.push(skill);
      } else {
        featured.push(skill);
      }
    }
    return { teamSkills: team, featuredSkills: featured, skillPacks: packs };
  }, [searchKeyword, hubSkills, hubTeamHome, hubSkillHome]);

  // 广场详情页签（内容详情 / 包含技能，仅技能包显示"包含技能"）
  const [hubDetailTab, setHubDetailTab] = useState<'content' | 'members'>('content');

  const fetchHubSkillDetail = useCallback(
    async (skill: MarketplacePluginItem) => {
      setHubDetailState('loading');
      setMarketplaceSubView('detail');
      // 每次进入详情 / 切换技能时重置回"内容详情"页签
      setHubDetailTab('content');
      try {
        if (skill.source === 'clawhub') {
          setHubDetail({
            success: true,
            asset_id: skill.asset_id,
            version: skill.latest_version || '',
            data: {
              short_desc: skill.short_desc,
              detail_desc: skill.short_desc || skill.detail_desc || '',
            },
          });
          setHubDetailState('success');
          return;
        }

        const data = await webRequest<HubSkillDetail>(
          'skills.swarmskillshub.detail',
          withSession({
            asset_id: skill.asset_id,
          }),
          { timeoutMs: 30000 },
        );
        setHubDetail(data);
        setHubDetailState('success');
      } catch (error) {
        console.error(error);
        setHubDetailState('error');
      }
    },
    [withSession, setMarketplaceSubView],
  );

  /** 分类切换 / 关键词变化：作废在途请求并置为加载中 */
  const invalidateHubFetch = useCallback(() => {
    hubFetchSeqRef.current += 1;
    hubMoreFetchSeqRef.current += 1;
    hubHomeLoadedCategoryRef.current = null;
    setHubSkills([]);
    setHubTeamHome([]);
    setHubSkillHome([]);
    setHubTeamMore([]);
    setHubSkillMore([]);
    setHubMoreLoadedFor(null);
    setHubLoading(true);
    setHubMoreLoading(false);
  }, []);

  /** 离开技能广场页签：作废在途请求，保留首页/更多列表供再次进入时 stale-while-revalidate */
  const pauseHubFetching = useCallback(() => {
    hubFetchSeqRef.current += 1;
    hubMoreFetchSeqRef.current += 1;
    setHubLoading(false);
    setHubMoreLoading(false);
    // 仅清搜索结果；首页与「更多」列表保留
    setHubSkills([]);
  }, []);

  return {
    hubSkills,
    hubLoading,
    hubCache,
    hubMoreLoading,
    hubTeamHome,
    hubSkillHome,
    hubTeamMore,
    hubSkillMore,
    /** 分组结果：skillpack → 技能包，swarmskill → 团队技能，其余 → 精选技能 */
    teamSkills,
    featuredSkills,
    skillPacks,
    selectedHubSkill,
    hubDetail,
    hubDetailState,
    hubDetailTab,
    setHubDetailTab,
    setSelectedHubSkill,
    setHubDetail,
    setHubDetailState,
    fetchHubSkillDetail,
    openHubMore,
    openHubAllPacks,
    invalidateHubFetch,
    pauseHubFetching,
  };
}
