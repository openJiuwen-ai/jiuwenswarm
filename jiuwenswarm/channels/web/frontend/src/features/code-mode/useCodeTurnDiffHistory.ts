import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Message, ProjectInfo } from '../../types';
import { gitClient } from './gitClient';
import { gitWatchClient } from './gitWatchClient';
import { latestTurnDiffKeyForMessages, turnChangeErrorMessage, turnDiffKey, updateTurnChangeStatus } from './turnChangeState';
import type { GitDiscardTurnChangesResult, GitRedoTurnChangesResult, GitTurnChangeAction, GitTurnDiff } from './types';
import { bindTurnDiffsToMessages } from './codeTurnDiffBinding';
export { bindTurnDiffsToMessages } from './codeTurnDiffBinding';

interface UseCodeTurnDiffHistoryOptions {
  project: ProjectInfo | null;
  sessionId: string | null;
  isProcessing: boolean;
  messages: Message[];
}

export function useCodeTurnDiffHistory({ project, sessionId, isProcessing, messages }: UseCodeTurnDiffHistoryOptions) {
  const [turns, setTurns] = useState<GitTurnDiff[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [turnChangeOperation, setTurnChangeOperation] = useState<{
    action: GitTurnChangeAction;
    turnKey: string;
  } | null>(null);
  const [turnChangeError, setTurnChangeError] = useState<{ turnKey: string; message: string } | null>(null);
  const [turnChangeNotice, setTurnChangeNotice] = useState<string | null>(null);
  const requestSequenceRef = useRef(0);
  const operationSequenceRef = useRef(0);
  const previousProcessingRef = useRef(isProcessing);
  const projectId = project?.work_mode === 'code' && !project.is_default ? project.project_id : null;

  const loadHistory = useCallback(async () => {
    if (!projectId || !sessionId || sessionId === 'new') {
      setTurns([]);
      setError(null);
      return;
    }
    const requestSequence = requestSequenceRef.current + 1;
    requestSequenceRef.current = requestSequence;
    setLoading(true);
    setError(null);
    try {
      // limit=0 is explicitly defined by the backend as "return all turns".
      const response = await gitClient.turnDiffList(projectId, sessionId, { limit: 0 });
      if (requestSequenceRef.current !== requestSequence) return;
      setTurns(response.turns);
    } catch (nextError) {
      if (requestSequenceRef.current !== requestSequence) return;
      console.warn('[code-mode] Failed to load turn diff history', nextError);
      setError(nextError instanceof Error ? nextError.message : '加载逐轮修改历史失败');
    } finally {
      if (requestSequenceRef.current === requestSequence) setLoading(false);
    }
  }, [projectId, sessionId]);

  useEffect(() => {
    requestSequenceRef.current += 1;
    operationSequenceRef.current += 1;
    previousProcessingRef.current = isProcessing;
    setTurns([]);
    setError(null);
    setTurnChangeOperation(null);
    setTurnChangeError(null);
    setTurnChangeNotice(null);
  }, [projectId, sessionId]);

  useEffect(() => {
    const completed = previousProcessingRef.current && !isProcessing;
    previousProcessingRef.current = isProcessing;
    if (isProcessing) return;
    const timer = window.setTimeout(() => void loadHistory(), completed ? 350 : 0);
    return () => window.clearTimeout(timer);
  }, [isProcessing, loadHistory]);

  const turnsByMessageId = useMemo(() => bindTurnDiffsToMessages(messages, turns), [messages, turns]);
  const latestTurnKey = useMemo(() => latestTurnDiffKeyForMessages(messages, turns, turnsByMessageId), [messages, turns, turnsByMessageId]);

  useEffect(() => {
    if (!turnChangeNotice) return;
    const timer = window.setTimeout(() => setTurnChangeNotice(null), 3000);
    return () => window.clearTimeout(timer);
  }, [turnChangeNotice]);

  const changeLatestTurn = useCallback(
    async (action: GitTurnChangeAction) => {
      if (!projectId || !sessionId || sessionId === 'new' || isProcessing) return;
      const latestTurn = turns.reduce<GitTurnDiff | null>((current, turn) => (!current || turn.turn_index > current.turn_index ? turn : current), null);
      if (!latestTurn) return;

      const turnKey = turnDiffKey(latestTurn);
      if (turnKey !== latestTurnKey) return;
      if (action === 'redo' ? latestTurn.status !== 'discarded' : latestTurn.status === 'discarded') return;

      const operationSequence = operationSequenceRef.current + 1;
      operationSequenceRef.current = operationSequence;
      setTurnChangeOperation({ action, turnKey });
      setTurnChangeError(null);
      setTurnChangeNotice(null);

      try {
        const params = { project_id: projectId, session_id: sessionId };
        const result =
          action === 'discard'
            ? await gitWatchClient.request<GitDiscardTurnChangesResult>('project.git.discard_turn_changes', params, { timeoutMs: 60_000 })
            : await gitWatchClient.request<GitRedoTurnChangesResult>('project.git.redo_turn_changes', params, {
                timeoutMs: 60_000,
              });
        if (operationSequenceRef.current !== operationSequence) return;
        setTurns(previous => updateTurnChangeStatus(previous, result, action === 'discard' ? 'discarded' : 'completed'));
        setTurnChangeNotice(action === 'discard' ? '已撤销修改' : '已重新应用修改');
        void loadHistory();
      } catch (nextError) {
        if (operationSequenceRef.current !== operationSequence) return;
        setTurnChangeError({ turnKey, message: turnChangeErrorMessage(nextError, action) });
      } finally {
        if (operationSequenceRef.current === operationSequence) setTurnChangeOperation(null);
      }
    },
    [isProcessing, latestTurnKey, loadHistory, projectId, sessionId, turns],
  );
  const discardLatestTurn = useCallback(() => changeLatestTurn('discard'), [changeLatestTurn]);
  const redoLatestTurn = useCallback(() => changeLatestTurn('redo'), [changeLatestTurn]);

  return {
    turns,
    turnsByMessageId,
    loading,
    error,
    reload: loadHistory,
    latestTurnKey,
    turnChangeOperation,
    turnChangeError,
    turnChangeNotice,
    discardLatestTurn,
    redoLatestTurn,
  };
}
