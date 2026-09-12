/**
 * 技能广场（SkillHub 推荐 / 在线搜索 / 广场详情）数据 hook
 *
 * 从 index.tsx 抽取，逻辑保持不变。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { webRequest } from '../../services/webClient';
import type { HubSkillDetail, LoadState, MarketplacePluginItem } from './types';

export type WithSessionFn = <T extends Record<string, unknown> = Record<string, unknown>>(
  params?: T,
) => T & { session_id: string };

export type SkillPanelTab = 'my' | 'marketplace' | 'graph';
export type MarketplaceSubView = 'list' | 'detail';

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
  const [hubSkills, setHubSkills] = useState<MarketplacePluginItem[]>([]);
  const [hubLoading, setHubLoading] = useState(false);
  /** 技能广场请求序号：防抖搜索/分类切换时丢弃过期响应 */
  const hubFetchSeqRef = useRef(0);
  const [selectedHubSkill, setSelectedHubSkill] = useState<MarketplacePluginItem | null>(null);
  const [hubDetail, setHubDetail] = useState<HubSkillDetail | null>(null);
  const [hubDetailState, setHubDetailState] = useState<LoadState>('idle');

  const fetchHubSkills = useCallback(
    async (category: string) => {
      const seq = ++hubFetchSeqRef.current;
      setHubLoading(true);
      try {
        // Hub 地址由后端统一配置，确保推荐列表与安装使用同一个 Hub。
        const params = withSession({
          top_k: 50,
          ...(category !== 'all' ? { category_id: category } : {}),
        });
        const data = await webRequest<{
          success: boolean;
          skills?: Array<{
            asset_id: string;
            name: string;
            display_name?: string;
            summary?: string;
            version?: string;
            plugin_type?: string;
            tags?: string[];
          }>;
          detail?: string;
        }>('skills.swarmskillshub.recommend', params, { timeoutMs: 30000 });

        if (!data.success) throw new Error(data.detail || 'Recommend failed');
        if (seq !== hubFetchSeqRef.current) return;
        const items: MarketplacePluginItem[] = (data.skills || []).map((s) => ({
          asset_id: s.asset_id,
          name: s.name,
          display_name: s.display_name || s.name,
          short_desc: s.summary || '',
          publisher_name: '',
          install_count: 0,
          like_count: 0,
          view_count: 0,
          plugin_type: s.plugin_type || null,
          tags: s.tags || null,
          latest_version: s.version || null,
        }));
        setHubSkills(items);
      } catch (error) {
        console.error('Failed to fetch SkillHub recommend:', error);
        if (seq !== hubFetchSeqRef.current) return;
        setHubSkills([]);
      } finally {
        if (seq === hubFetchSeqRef.current) setHubLoading(false);
      }
    },
    [withSession],
  );

  const fetchOnlineSearch = useCallback(
    async (query: string) => {
      const seq = ++hubFetchSeqRef.current;
      setHubLoading(true);
      try {
        // 文档 2~4: skills.online_search.search
        const data = await webRequest<{
          success: boolean;
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
            limit: 50,
          }),
          { timeoutMs: 45000 },
        );

        // success=false: 参数非法或所有来源均失败
        if (!data.success) {
          throw new Error(data.detail || 'Search failed');
        }

        // partial=true: 部分来源失败，仍展示已有结果
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
        setHubSkills([]);
      } finally {
        if (seq === hubFetchSeqRef.current) setHubLoading(false);
      }
    },
    [withSession],
  );

  useEffect(() => {
    if (activeTab !== 'marketplace') return;

    if (!searchKeyword) {
      void fetchHubSkills(marketplaceCategory);
      return;
    }

    const timer = window.setTimeout(() => {
      void fetchOnlineSearch(searchKeyword);
    }, 500);

    return () => window.clearTimeout(timer);
  }, [activeTab, marketplaceCategory, searchKeyword, fetchHubSkills, fetchOnlineSearch]);

  // 按 plugin_type 分组：swarmskill → 精选团队技能，其余 → 精选技能
  const { teamSkills, featuredSkills } = useMemo(() => {
    const team: MarketplacePluginItem[] = [];
    const featured: MarketplacePluginItem[] = [];
    for (const skill of hubSkills) {
      if (skill.plugin_type === 'swarmskill') {
        team.push(skill);
      } else {
        featured.push(skill);
      }
    }
    return { teamSkills: team, featuredSkills: featured };
  }, [hubSkills]);

  const fetchHubSkillDetail = useCallback(
    async (skill: MarketplacePluginItem) => {
      setHubDetailState('loading');
      setMarketplaceSubView('detail');
      try {
        // ClawHub 条目没有 detail RPC，直接用搜索结果中的描述
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

        // SwarmSkillHub 条目：通过 asset_id 查询详情
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
    setHubSkills([]);
    setHubLoading(true);
  }, []);

  /** 离开技能广场页签：作废在途请求并停止加载 */
  const pauseHubFetching = useCallback(() => {
    hubFetchSeqRef.current += 1;
    setHubLoading(false);
    setHubSkills([]);
  }, []);

  return {
    hubSkills,
    hubLoading,
    teamSkills,
    featuredSkills,
    selectedHubSkill,
    hubDetail,
    hubDetailState,
    setSelectedHubSkill,
    setHubDetail,
    setHubDetailState,
    fetchHubSkills,
    fetchHubSkillDetail,
    invalidateHubFetch,
    pauseHubFetching,
  };
}
