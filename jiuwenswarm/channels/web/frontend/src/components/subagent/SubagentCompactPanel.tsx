import { useTranslation } from 'react-i18next';
import { useSubagentStore, selectSubagents } from '../../stores/subagentStore';
import { getSubagentStatusLabelKey } from '../../features/subagent/subagentStatusPresentation';
import CollapseIcon from '../../assets/subagent/collapse.svg?react';
import TeamMembersIcon from '../../assets/subagent/team-members.svg?react';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { SubagentStatusIcon } from './SubagentStatusIcon';
import './Subagent.css';

export function SubagentCompactPanel({ sessionId, onExpand }: { sessionId: string; onExpand: () => void }) {
  const { t } = useTranslation();
  const runtime = useSubagentStore(state => state.runtimes[sessionId]);
  const setSelectedSubagent = useSubagentStore(state => state.setSelectedSubagent);
  const subagents = selectSubagents(runtime);

  if (!runtime || subagents.length === 0) return null;

  return (
    <section className="subagent-compact-panel" aria-label={t('subagent.title')} data-testid="subagent-compact-panel">
      <div className="subagent-compact-panel__header">
        <div className="flex min-w-0 items-center gap-2">
          <TeamMembersIcon className="h-4 w-4 shrink-0 text-text-muted" aria-hidden="true" />
          <h2 className="truncate text-sm font-semibold text-text" data-testid="subagent-compact-title">{t('subagent.compactTitle', { count: subagents.length })}</h2>
        </div>
        <button type="button" className="subagent-icon-button" onClick={onExpand} aria-label={t('subagent.expand')} title={t('subagent.expand')} data-testid="subagent-compact-expand-button">
          <CollapseIcon className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>

      <div className="subagent-compact-panel__list" data-testid="subagent-compact-list">
        {subagents.map(subagent => {
          const statusLabel = t(getSubagentStatusLabelKey(subagent.status, subagent.closed_reason, subagent.turn_outcome));
          return (
            <button
              key={subagent.subagent_id}
              type="button"
              className="subagent-compact-row"
              onClick={() => {
                setSelectedSubagent(sessionId, subagent.subagent_id);
                onExpand();
              }}
              aria-label={t('subagent.selectWithStatus', { name: subagent.display_name, status: statusLabel })}
              data-testid="subagent-compact-row"
              data-variant={subagent.subagent_id}
            >
              <TeamMemberAvatar member={subagent.subagent_id} alt={subagent.display_name} className="h-6 w-6 rounded-lg" imageClassName="rounded-lg" />
              <span className="subagent-compact-row__copy">
                <span className="subagent-compact-row__name truncate text-sm font-semibold text-text" data-testid="subagent-compact-row-name">{subagent.display_name}</span>
                {(subagent.role || subagent.task_description) ? (
                  <span className="subagent-compact-row__role truncate text-sm text-text-muted" data-testid="subagent-compact-row-role"> | {subagent.role || subagent.task_description}</span>
                ) : null}
              </span>
              <SubagentStatusIcon status={subagent.status} closedReason={subagent.closed_reason} turnOutcome={subagent.turn_outcome} />
            </button>
          );
        })}
      </div>
    </section>
  );
}
