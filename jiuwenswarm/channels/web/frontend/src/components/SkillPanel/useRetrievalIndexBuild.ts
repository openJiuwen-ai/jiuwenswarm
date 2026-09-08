import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import { canBuildSkillRetrievalIndex, parseSkillRetrievalStatus } from './skillRetrievalStatus';
import type { WithSessionFn } from './useHubMarketplace';
import type { SkillToastShower } from './useSkillToasts';

interface UseRetrievalIndexBuildParams {
  isConnected: boolean;
  withSession: WithSessionFn;
  showMessage: SkillToastShower;
}

export function useRetrievalIndexBuild({ isConnected, withSession, showMessage }: UseRetrievalIndexBuildParams) {
  const { t } = useTranslation();
  const startRetrievalIndexBuild = useCallback(
    async (force: boolean) => {
      if (!isConnected) return false;
      try {
        const statusPayload = await webRequest<unknown>('skills.retrieval.status', withSession());
        const status = parseSkillRetrievalStatus(statusPayload);
        if (status.build_status === 'running') return true;
        if (!canBuildSkillRetrievalIndex(status)) return false;
        const payload = await webRequest<Record<string, unknown>>(
          'skills.retrieval.index_build',
          withSession({ force: force || status.index_exists, source: 'web' }),
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

  return { startRetrievalIndexBuild };
}
