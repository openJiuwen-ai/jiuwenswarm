import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { AgentGroupCatalogItem } from '../../features/agentManagement/types';
import { useExpertTeamBatchStore, EMPTY_EXPERT_BATCH } from '../../stores/expertTeamBatchStore';
import { useTeamSelectorStore } from '../../stores/teamSelectorStore';
import { Button } from '../ui';

interface Props {
  sessionId: string;
  groups: AgentGroupCatalogItem[];
  loading: boolean;
  loadError: boolean;
  ensureSession: () => Promise<string | null>;
  onCancel: () => void;
}

export function ExpertTeamBatchStatus({ sessionId, onOpen }: { sessionId: string; onOpen: () => void }) {
  const { t } = useTranslation();
  const runtime = useExpertTeamBatchStore((state) => state.runtimes[sessionId]);
  if (!runtime || (!runtime.submitting && !runtime.error && !runtime.results.length)) return null;
  const failed = runtime.results.filter((result) => result.status === 'failed').length;
  return (
    <button type="button" className="chat-agent-tag" data-testid="chat-panel-expert-batch-status"
      onClick={onOpen} aria-live="polite">
      {runtime.submitting ? t('chat.expertBatchCreating') : runtime.error
        ? t('chat.expertBatchRequestFailed')
        : t('chat.expertBatchSummary', { success: runtime.results.length - failed, failed })}
    </button>
  );
}

export function ExpertTeamBatchPicker({ sessionId, groups, loading, loadError, ensureSession, onCancel }: Props) {
  const { t } = useTranslation();
  const runtime = useExpertTeamBatchStore((state) => state.runtimes[sessionId] ?? EMPTY_EXPERT_BATCH);
  const teams = useTeamSelectorStore((state) => state.runtimes[sessionId]?.teams);
  const [failedAvatarUrls, setFailedAvatarUrls] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => {
    if (sessionId !== 'new') void useTeamSelectorStore.getState().fetchTeams(sessionId);
  }, [sessionId]);
  const added = new Set((teams ?? []).filter((team) => !team.is_owner).map((team) => team.agent_group_name));
  return (
    <div data-testid="chat-panel-expert-batch-picker" aria-busy={runtime.submitting}>
      {loading || loadError ? (
        <div role="status" data-testid="chat-panel-expert-batch-catalog-state">
          {t(loading ? 'common.loading' : 'agentManagement.group.states.loadError')}
        </div>
      ) : groups.length === 0 ? (
        <div className="chat-agent-picker__state" data-testid="chat-panel-expert-batch-empty">
          {t('chat.agentGroupNoMatches')}
        </div>
      ) : groups.map((group) => {
        const existing = added.has(group.name);
        const selected = existing || runtime.selected.includes(group.name);
        const avatarUrl = group.avatarUrl && !failedAvatarUrls.has(group.avatarUrl) ? group.avatarUrl : null;
        return (
          <button key={group.id} type="button" role="menuitemcheckbox" aria-checked={selected}
            disabled={runtime.submitting || existing}
            className={`chat-agent-picker__item${selected ? ' is-selected' : ''}`}
            data-testid="chat-panel-expert-batch-item" data-variant={group.id}
            onClick={() => useExpertTeamBatchStore.getState().toggle(sessionId, group.name)}>
            <span aria-hidden="true">{selected ? '☑' : '☐'}</span>
            <span className="chat-agent-picker__avatar" aria-hidden="true"
              data-testid="chat-panel-expert-batch-avatar" data-variant={group.id}>
              {avatarUrl ? (
                <img src={avatarUrl} alt="" data-testid="chat-panel-expert-batch-avatar-image" data-variant={group.id}
                  onError={() => setFailedAvatarUrls((current) => new Set([...current, avatarUrl]))} />
              ) : (
                Array.from(group.displayName.trim() || group.name)[0]?.toUpperCase() || '?'
              )}
            </span>
            <span className="chat-agent-picker__item-name">{group.displayName}</span>
            {existing && <span data-testid="chat-panel-expert-batch-existing">{t('chat.expertBatchExisting')}</span>}
          </button>
        );
      })}
      <div role="status" aria-live="polite" data-testid="chat-panel-expert-batch-results">
        {runtime.results.map((result) => (
          <div key={result.agent_group_name} data-testid="chat-panel-expert-batch-result" data-variant={result.agent_group_name}>
            {groups.find((group) => group.name === result.agent_group_name)?.displayName ?? result.agent_group_name}
            {' · '}{t(`chat.expertBatchStatus.${result.status}`)}
            {result.error && `: ${result.error}`}
          </div>
        ))}
        {runtime.error && <div role="alert" data-testid="chat-panel-expert-batch-error">{t('chat.expertBatchError', { error: runtime.error })}</div>}
      </div>
      <div className="flex items-center justify-between gap-2 p-2" data-testid="chat-panel-expert-batch-actions">
        <Button variant="secondary" disabled={runtime.submitting} data-testid="chat-panel-expert-batch-cancel"
          onClick={() => { useExpertTeamBatchStore.getState().clear(sessionId); onCancel(); }}>
          {t('common.cancel')}
        </Button>
        <Button variant="primary" disabled={loading || loadError || runtime.submitting || !runtime.selected.length}
          data-testid="chat-panel-expert-batch-confirm"
          onClick={() => void useExpertTeamBatchStore.getState().submit(sessionId, ensureSession)}>
          {runtime.submitting ? t('chat.expertBatchCreating') : t('chat.expertBatchConfirm', { count: runtime.selected.length })}
        </Button>
      </div>
    </div>
  );
}
