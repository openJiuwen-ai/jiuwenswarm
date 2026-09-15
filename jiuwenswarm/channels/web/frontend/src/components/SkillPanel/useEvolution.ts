/**
 * 技能经验（evolutions）页签 hook
 *
 * 从 index.tsx 抽取，逻辑保持不变。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import type { EvolutionEntry, EvolutionGetResponse, LoadState, SkillDetail } from './types';
import type { WithSessionFn } from './useHubMarketplace';

interface UseEvolutionParams {
  selectedSkill: SkillDetail | null;
  detailTab: 'content' | 'files' | 'experience';
  withSession: WithSessionFn;
  fetchSkills: (refreshMarketplaces?: boolean) => Promise<void>;
}

export function useEvolution({ selectedSkill, detailTab, withSession, fetchSkills }: UseEvolutionParams) {
  const { t } = useTranslation();
  const [evolutionEntries, setEvolutionEntries] = useState<EvolutionEntry[]>([]);
  const [evolutionListState, setEvolutionListState] = useState<LoadState>('idle');
  const [evolutionMessage, setEvolutionMessage] = useState<string | null>(null);
  const [evolutionMessageType, setEvolutionMessageType] = useState<'success' | 'error' | null>(null);
  const [evolutionFormatError, setEvolutionFormatError] = useState<string | null>(null);
  const evolutionSaveTimerRef = useRef<number | null>(null);

  // ---- 技能经验（内联展示） ----
  const sortedEvolutionEntries = useMemo(
    () =>
      [...evolutionEntries].sort((a, b) => {
        const ta = a.timestamp || '';
        const tb = b.timestamp || '';
        return tb.localeCompare(ta);
      }),
    [evolutionEntries],
  );

  const fetchEvolutionEntries = useCallback(async () => {
    if (!selectedSkill) return;
    setEvolutionListState('loading');
    setEvolutionMessage(null);
    setEvolutionMessageType(null);
    setEvolutionFormatError(null);
    try {
      const data = await webRequest<EvolutionGetResponse>(
        'skills.evolution.get',
        withSession({ name: selectedSkill.name }),
      );
      if (!data.exists) {
        setEvolutionEntries([]);
        setEvolutionListState('success');
        return;
      }
      if (data.valid === false) {
        setEvolutionEntries([]);
        setEvolutionFormatError(data.detail || t('skills.evolution.errors.invalidFile'));
        setEvolutionListState('success');
        return;
      }
      setEvolutionEntries(data.entries || []);
      setEvolutionListState('success');
    } catch (error) {
      console.error(error);
      setEvolutionListState('error');
    }
  }, [selectedSkill, t, withSession]);

  useEffect(() => {
    if (detailTab === 'experience' && selectedSkill?.has_evolutions) {
      void fetchEvolutionEntries();
    }
  }, [detailTab, selectedSkill, fetchEvolutionEntries]);

  const handleEvolutionContentChange = useCallback((entryId: string, value: string) => {
    setEvolutionEntries((prev) =>
      prev.map((entry) => (entry.id === entryId ? { ...entry, change: { ...entry.change, content: value } } : entry)),
    );
  }, []);

  const handleEvolutionDeleteEntry = useCallback(
    (entryId: string) => {
      const confirmed = window.confirm(t('skills.evolution.deleteConfirm'));
      if (!confirmed) return;
      setEvolutionEntries((prev) => prev.filter((entry) => entry.id !== entryId));
    },
    [t],
  );

  // 自动保存（带防抖）
  const saveEvolutionEntries = useCallback(
    async (entries: EvolutionEntry[]) => {
      if (!selectedSkill) return;
      try {
        const data = await webRequest<{
          success: boolean;
          detail?: string;
          message?: string;
        }>('skills.evolution.save', withSession({ name: selectedSkill.name, entries }));
        if (!data.success) {
          throw new Error(data.detail || data.message || t('skills.evolution.errors.saveFailed'));
        }
        await fetchSkills();
      } catch (error) {
        console.error(error);
        setEvolutionMessage(t('skills.evolution.errors.saveFailed'));
        setEvolutionMessageType('error');
      }
    },
    [selectedSkill, t, withSession, fetchSkills],
  );

  // 防抖保存：监听 evolutionEntries 变化
  useEffect(() => {
    // 只在技能经验页签且有数据时触发
    if (detailTab !== 'experience' || !selectedSkill?.has_evolutions || evolutionEntries.length === 0) {
      return;
    }
    // 清除之前的计时器
    if (evolutionSaveTimerRef.current) {
      clearTimeout(evolutionSaveTimerRef.current);
    }
    // 设置新的防抖计时器
    evolutionSaveTimerRef.current = window.setTimeout(() => {
      saveEvolutionEntries(evolutionEntries);
    }, 500);
    // 清理函数
    return () => {
      if (evolutionSaveTimerRef.current) {
        clearTimeout(evolutionSaveTimerRef.current);
      }
    };
  }, [evolutionEntries, detailTab, selectedSkill, saveEvolutionEntries]);

  return {
    sortedEvolutionEntries,
    evolutionListState,
    evolutionMessage,
    evolutionMessageType,
    evolutionFormatError,
    handleEvolutionContentChange,
    handleEvolutionDeleteEntry,
  };
}
