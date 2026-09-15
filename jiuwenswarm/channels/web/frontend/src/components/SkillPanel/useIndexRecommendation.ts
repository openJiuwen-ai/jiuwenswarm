/**
 * 技能检索索引推荐 hook（图谱页签提示条）
 *
 * 从 index.tsx 抽取，逻辑保持不变。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { parseConfigBoolean } from '../../features/settings/services/settingsContract';
import { canBuildSkillRetrievalIndex, parseSkillRetrievalStatus } from './skillRetrievalStatus';
import type { WithSessionFn } from './useHubMarketplace';
import type { SkillToastShower } from './useSkillToasts';

interface UseIndexRecommendationParams {
  isActive: boolean;
  activeTab: 'my' | 'marketplace' | 'graph';
  isConnected: boolean;
  withSession: WithSessionFn;
  showMessage: SkillToastShower;
}

export function useIndexRecommendation({
  isActive,
  activeTab,
  isConnected,
  withSession,
  showMessage,
}: UseIndexRecommendationParams) {
  const { t } = useTranslation();
  const [indexRecommendationVisible, setIndexRecommendationVisible] = useState(false);
  const [indexRecommendationBuilding, setIndexRecommendationBuilding] = useState(false);
  const indexRecommendationRequestRef = useRef(0);

  const startRetrievalIndexBuild = useCallback(
    async (force: boolean, useDefaultProfile = false) => {
      if (!isConnected) return false;
      try {
        const statusPayload = await webRequest<unknown>(
          'skills.retrieval.status',
          useDefaultProfile ? {} : withSession(),
        );
        const status = parseSkillRetrievalStatus(statusPayload);
        if (status.build_status === 'running') return true;
        if (!canBuildSkillRetrievalIndex(status)) return false;
        const buildParams = {
          force: force || status.index_exists,
          source: 'web',
        };
        const payload = await webRequest<Record<string, unknown>>(
          'skills.retrieval.index_build',
          useDefaultProfile ? buildParams : withSession(buildParams),
          { timeoutMs: 30_000 },
        );
        if (payload.success !== true) {
          throw new Error(String(payload.detail || t('skills.retrieval.buildFailed')));
        }
        if (typeof payload.build_id !== 'string' || !payload.build_id) {
          throw new Error(t('skills.retrieval.statusIncompatible'));
        }
        showMessage('success', t('skills.retrieval.buildStarted'));
        return true;
      } catch (error) {
        console.error('Failed to start Skill taxonomy build:', error);
        showMessage('error', error instanceof Error ? error.message : t('skills.retrieval.buildFailed'));
        return false;
      }
    },
    [isConnected, showMessage, t, withSession],
  );

  useEffect(() => {
    const requestRevision = ++indexRecommendationRequestRef.current;
    if (!isActive || activeTab !== 'graph' || !isConnected) {
      setIndexRecommendationVisible(false);
      return;
    }

    setIndexRecommendationVisible(false);
    void (async () => {
      try {
        const config = await webRequest<Record<string, unknown>>('config.get');
        if (requestRevision !== indexRecommendationRequestRef.current) return;
        if (
          !parseConfigBoolean(config.skill_retrieval_enabled) ||
          parseConfigBoolean(config.skill_retrieval_index_enabled) ||
          parseConfigBoolean(config.skill_retrieval_index_recommendation_shown)
        ) {
          return;
        }

        const status = parseSkillRetrievalStatus(await webRequest<unknown>('skills.retrieval.status'));
        if (requestRevision !== indexRecommendationRequestRef.current || !status.index_recommended) return;

        await webRequest('config.set', {
          skill_retrieval_index_recommendation_shown: 'true',
        });
        if (requestRevision === indexRecommendationRequestRef.current) {
          setIndexRecommendationVisible(true);
        }
      } catch {
        // The recommendation is advisory and must not affect the Skill graph.
      }
    })();
  }, [activeTab, isActive, isConnected]);

  const buildRecommendedIndex = useCallback(async () => {
    if (indexRecommendationBuilding) return;
    setIndexRecommendationBuilding(true);
    try {
      await webRequest('config.set', {
        skill_retrieval_index_enabled: 'true',
      });
      const started = await startRetrievalIndexBuild(false, true);
      if (started) {
        setIndexRecommendationVisible(false);
      } else {
        await webRequest('config.set', {
          skill_retrieval_index_enabled: 'false',
        });
      }
    } catch (error) {
      console.error('Failed to enable Skill taxonomy index:', error);
      showMessage('error', t('skills.retrieval.buildFailed'));
    } finally {
      setIndexRecommendationBuilding(false);
    }
  }, [indexRecommendationBuilding, showMessage, startRetrievalIndexBuild, t]);

  return {
    indexRecommendationVisible,
    indexRecommendationBuilding,
    setIndexRecommendationVisible,
    startRetrievalIndexBuild,
    buildRecommendedIndex,
  };
}
