/**
 * 技能总谱页签：交响编排开关 + 图谱读取状态 hook
 *
 * 从 index.tsx 抽取，逻辑保持不变。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';
import type { SkillGraphPanelHandle } from '../SkillGraphPanel';
import { coordinateSymphonyEnabledChange } from './symphonyGraphAction';
import { GRAPH_READING_MIN_VISIBLE_MS } from './skillPanelUtils';

interface UseSymphonyGraphParams {
  isConnected: boolean;
  symphonyEnabled: boolean;
  onSymphonyEnabledChange: (enabled: boolean) => Promise<boolean>;
}

export function useSymphonyGraph({ isConnected, symphonyEnabled, onSymphonyEnabledChange }: UseSymphonyGraphParams) {
  const { t } = useTranslation();
  const skillGraphPanelRef = useRef<SkillGraphPanelHandle | null>(null);
  const graphReadingStartedAtRef = useRef<number | null>(null);
  const graphReadingTimerRef = useRef<number | null>(null);
  const [graphReading, setGraphReading] = useState(false);
  const [symphonyEnabledDraft, setSymphonyEnabledDraft] = useState(symphonyEnabled);
  const [symphonySaving, setSymphonySaving] = useState(false);
  const [symphonySaveError, setSymphonySaveError] = useState<string | null>(null);
  const [graphActionError, setGraphActionError] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      if (graphReadingTimerRef.current !== null) {
        window.clearTimeout(graphReadingTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!symphonySaving) {
      setSymphonyEnabledDraft(symphonyEnabled);
    }
  }, [symphonyEnabled, symphonySaving]);

  const clearGraphActionError = useCallback(() => {
    setGraphActionError(null);
  }, []);

  const updateSymphonyEnabled = useCallback(
    async (enabled: boolean) => {
      if (!isConnected || symphonySaving || enabled === symphonyEnabledDraft) return;
      setSymphonyEnabledDraft(enabled);
      setSymphonySaving(true);
      setSymphonySaveError(null);
      const result = await coordinateSymphonyEnabledChange({
        enabled,
        save: onSymphonyEnabledChange,
        getGraphPanel: () => skillGraphPanelRef.current,
        request: webRequest,
        refreshFailedMessage: t('skills.graph.errors.refreshFailed'),
        cancelFailedMessage: t('skills.graph.errors.cancelFailed'),
        onGraphActionStart: clearGraphActionError,
      });
      if (result.configSaveFailed) {
        setSymphonyEnabledDraft(symphonyEnabled);
        setSymphonySaveError(t('skills.graph.orchestration.saveFailed'));
        setSymphonySaving(false);
        return;
      }
      if (!result.appliedWithoutRestart) {
        setSymphonySaving(false);
        return;
      }
      if (result.graphActionError) {
        setGraphActionError(result.graphActionError);
      }
      setSymphonySaving(false);
    },
    [
      clearGraphActionError,
      isConnected,
      onSymphonyEnabledChange,
      symphonyEnabled,
      symphonyEnabledDraft,
      symphonySaving,
      t,
    ],
  );

  const updateGraphReading = useCallback((reading: boolean) => {
    if (graphReadingTimerRef.current !== null) {
      window.clearTimeout(graphReadingTimerRef.current);
      graphReadingTimerRef.current = null;
    }
    if (reading) {
      graphReadingStartedAtRef.current = Date.now();
      setGraphReading(true);
      return;
    }
    const startedAt = graphReadingStartedAtRef.current;
    graphReadingStartedAtRef.current = null;
    const elapsed = startedAt == null ? GRAPH_READING_MIN_VISIBLE_MS : Date.now() - startedAt;
    const delay = Math.max(0, GRAPH_READING_MIN_VISIBLE_MS - elapsed);
    if (delay === 0) {
      setGraphReading(false);
      return;
    }
    graphReadingTimerRef.current = window.setTimeout(() => {
      graphReadingTimerRef.current = null;
      setGraphReading(false);
    }, delay);
  }, []);

  return {
    skillGraphPanelRef,
    graphReading,
    symphonyEnabledDraft,
    symphonySaving,
    symphonySaveError,
    graphActionError,
    clearGraphActionError,
    updateSymphonyEnabled,
    updateGraphReading,
  };
}
