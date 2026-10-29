import { useTranslation } from 'react-i18next';
import { Button } from '../../../components/ui/Button/Button';
import { ColleagueAvatar } from './ColleagueAvatar';
import {
  canDecideSharing,
  sharingStatus,
  sharingTime,
  SHARING_CONVERSATION_ID,
  type GroupSharingApi,
  type SharingApproval,
  type SharingQuery,
  type SharingTarget,
  type SharingTimestamp,
} from '../groupSharingApi';
import type { JiuwenMateColleague } from '../types';
import { useGroupSharing } from '../useGroupSharing';
import './GroupSharingInbox.css';

interface GroupSharingInboxProps {
  api: GroupSharingApi;
  configId: string;
  colleague: JiuwenMateColleague;
  conversationId: string;
  connectionEpoch: number;
  onOpen: () => void;
}

export function GroupSharingInbox({ api, configId, colleague, conversationId, connectionEpoch, onOpen }: GroupSharingInboxProps) {
  const { t, i18n } = useTranslation();
  const inbox = useGroupSharing(api, configId, connectionEpoch);
  const approvals = inbox.data?.approvals ?? [];
  const count = approvals.filter((item) => sharingStatus(item, inbox.now) === 'pending').length;
  const blocked = inbox.loading || inbox.loadError || Boolean(inbox.busy);
  const date = (value: SharingTimestamp) => {
    const time = sharingTime(value);
    return Number.isFinite(time) ? new Date(time).toLocaleString(i18n.language) : t('jiuwenmate.sharing.unknownTime');
  };
  const destination = (target: SharingTarget) =>
    [target.name || target.id, target.channel, target.app_id, target.kind, target.id].filter(Boolean).join(' · ');
  const querySummary = (query: SharingQuery) => {
    const seen = new Set<string>();
    const keywords = [query.query, ...(query.keywords ?? [])].filter((value): value is string => {
      if (!value) return false;
      const normalized = value.toLowerCase();
      if (seen.has(normalized)) return false;
      seen.add(normalized);
      return true;
    });
    const keywordSummary = keywords.length
      ? keywords.join(i18n.language.toLowerCase().startsWith('zh') ? ' 或 ' : ' OR ')
      : null;
    const criteria: [string, string | null][] = [
      ['keyword', keywordSummary],
      ['since', query.since],
      ['until', query.until],
      ['sender', query.sender_id],
      ['cursor', query.cursor],
      ['groupIds', query.group_ids.length ? query.group_ids.join(', ') : null],
      ['groupNames', query.group_names.length ? query.group_names.join(', ') : null],
      ['limit', String(query.limit)],
    ];
    return criteria.flatMap(([key, value]) =>
      value === null ? [] : [t(`jiuwenmate.sharing.queryScope.${key}`, { value })]).join('\n');
  };
  const decisionMessage = (approval: SharingApproval) => {
    const status = sharingStatus(approval, inbox.now);
    if (status === 'approved_once' || status === 'approved_permanent' || status === 'rejected') {
      return t(`jiuwenmate.sharing.decisionMessage.${status}`);
    }
    return null;
  };

  if (conversationId !== SHARING_CONVERSATION_ID) {
    return (
      <aside className="jiuwenmate-sharing-notice" data-testid="jiuwenmate-sharing-notice">
        <span data-testid="jiuwenmate-sharing-pending">{t('jiuwenmate.sharing.pending', { count })}</span>
        {inbox.loadError ? (
          <span role="alert" data-testid="jiuwenmate-sharing-load-error">{t('jiuwenmate.sharing.loadError')}</span>
        ) : null}
        <Button type="button" onClick={onOpen} data-testid="jiuwenmate-sharing-open">
          {t('jiuwenmate.sharing.open')}
        </Button>
      </aside>
    );
  }

  return (
    <section
      className="jiuwenmate-sharing-inbox"
      data-testid="jiuwenmate-sharing-inbox"
      aria-busy={inbox.loading}
    >
      <header className="jiuwenmate-sharing-toolbar">
        <h2 data-testid="jiuwenmate-sharing-title">{t('jiuwenmate.sharing.title')}</h2>
        <Button
          type="button"
          disabled={inbox.loading || Boolean(inbox.busy)}
          onClick={inbox.refresh}
          data-testid="jiuwenmate-sharing-refresh"
        >
          {t(inbox.loading ? 'common.loading' : 'common.refresh')}
        </Button>
      </header>
      <p data-testid="jiuwenmate-sharing-private-note">{t('jiuwenmate.sharing.privateNote')}</p>
      {inbox.loadError ? (
        <p role="alert" data-testid="jiuwenmate-sharing-load-error">{t('jiuwenmate.sharing.loadError')}</p>
      ) : null}
      {inbox.actionError ? (
        <p role="alert" data-testid="jiuwenmate-sharing-action-error">{t('jiuwenmate.sharing.actionError')}</p>
      ) : null}
      {!inbox.loading && !inbox.loadError && approvals.length === 0 ? (
        <p data-testid="jiuwenmate-sharing-empty">{t('jiuwenmate.sharing.empty')}</p>
      ) : null}
      <ol className="jiuwenmate-sharing-cards" data-testid="jiuwenmate-sharing-approvals">
        {approvals.map((approval) => {
          const status = sharingStatus(approval, inbox.now);
          const actionable = canDecideSharing(approval, inbox.now);
          const disabled = blocked || !actionable;
          const decision = decisionMessage(approval);
          return (
            <li key={approval.approval_id} data-testid="jiuwenmate-sharing-thread">
              <article
                className="jiuwenmate-chat-message jiuwenmate-chat-message--colleague jiuwenmate-sharing-agent-message"
                data-testid="jiuwenmate-sharing-agent-message"
              >
                <ColleagueAvatar colleague={colleague} size="small" />
                <div className="jiuwenmate-sharing-agent-message__content">
                  <div className="jiuwenmate-sharing-agent-message__author">{colleague.name}</div>
                  <div
                    className="jiuwenmate-sharing-card"
                    data-testid="jiuwenmate-sharing-card"
                    data-variant={approval.approval_id}
                    aria-busy={inbox.busy === `approval:${approval.approval_id}`}
                  >
                    <strong data-testid="jiuwenmate-sharing-status" data-variant={status}>
                      {t(`jiuwenmate.sharing.status.${status}`)}
                    </strong>
                    <dl className="jiuwenmate-sharing-fields">
                      <div>
                        <dt data-testid="jiuwenmate-sharing-source-label">{t('jiuwenmate.sharing.source')}</dt>
                        <dd data-testid="jiuwenmate-sharing-sources">
                          {approval.source_groups.map((group) => `${group.name || group.group_id} (${group.group_id})`).join(', ')}
                        </dd>
                      </div>
                      <div>
                        <dt data-testid="jiuwenmate-sharing-requester-label">{t('jiuwenmate.sharing.requester')}</dt>
                        <dd data-testid="jiuwenmate-sharing-requester">
                          {approval.requester_name || t('jiuwenmate.sharing.unknownRequester')}
                        </dd>
                      </div>
                      <div>
                        <dt data-testid="jiuwenmate-sharing-target-label">{t('jiuwenmate.sharing.target')}</dt>
                        <dd data-testid="jiuwenmate-sharing-target">{destination(approval.target)}</dd>
                      </div>
                      <div>
                        <dt data-testid="jiuwenmate-sharing-query-label">{t('jiuwenmate.sharing.query')}</dt>
                        <dd data-testid="jiuwenmate-sharing-query">{querySummary(approval.query)}</dd>
                      </div>
                      <div>
                        <dt data-testid="jiuwenmate-sharing-summary-label">{t('jiuwenmate.sharing.summary')}</dt>
                        <dd data-testid="jiuwenmate-sharing-summary">{approval.summary}</dd>
                      </div>
                    </dl>
                    <p data-testid="jiuwenmate-sharing-audience">
                      {t(approval.target.kind === 'group' ? 'jiuwenmate.sharing.groupAudience' : 'jiuwenmate.sharing.audience')}
                    </p>
                    <details data-testid="jiuwenmate-sharing-messages">
                      <summary data-testid="jiuwenmate-sharing-expand">
                        {t('jiuwenmate.sharing.exactMessages', { count: approval.messages.length })}
                      </summary>
                      <ol className="jiuwenmate-sharing-message-list" data-testid="jiuwenmate-sharing-message-list">
                        {approval.messages.map((message) => (
                          <li
                            key={`${message.group_id}:${message.message_id}`}
                            data-testid="jiuwenmate-sharing-message"
                            data-variant={`${message.group_id}:${message.message_id}`}
                          >
                            <strong data-testid="jiuwenmate-sharing-message-sender">
                              {message.group_name || message.group_id} · {message.sender_name} · {message.message_id}
                            </strong>
                            <pre data-testid="jiuwenmate-sharing-message-text">
                              {message.text ?? t('jiuwenmate.sharing.noMessageText')}
                            </pre>
                          </li>
                        ))}
                      </ol>
                    </details>
                    <p data-testid="jiuwenmate-sharing-created">{t('jiuwenmate.sharing.created', { time: date(approval.created_at) })}</p>
                    <p data-testid="jiuwenmate-sharing-expires">{t('jiuwenmate.sharing.expires', { time: date(approval.expires_at) })}</p>
                    {approval.decided_at != null ? (
                      <p data-testid="jiuwenmate-sharing-decided">
                        {t('jiuwenmate.sharing.decided', { time: date(approval.decided_at) })}
                      </p>
                    ) : null}
                    {status === 'pending' && !approval.can_decide ? (
                      <p data-testid="jiuwenmate-sharing-disabled">{t('jiuwenmate.sharing.cannotDecide')}</p>
                    ) : null}
                    {actionable ? (
                      <div className="jiuwenmate-sharing-actions" data-testid="jiuwenmate-sharing-actions">
                        <Button
                          type="button"
                          disabled={disabled}
                          onClick={() => inbox.decide(approval.approval_id, 'once')}
                          data-testid="jiuwenmate-sharing-once"
                        >{t('jiuwenmate.sharing.once')}</Button>
                        <Button
                          type="button"
                          disabled={disabled}
                          onClick={() => inbox.decide(approval.approval_id, 'permanent')}
                          data-testid="jiuwenmate-sharing-permanent"
                        >{t('jiuwenmate.sharing.permanent')}</Button>
                        <Button
                          type="button"
                          disabled={disabled}
                          onClick={() => inbox.decide(approval.approval_id, 'reject')}
                          data-testid="jiuwenmate-sharing-reject"
                        >{t('jiuwenmate.sharing.reject')}</Button>
                      </div>
                    ) : null}
                  </div>
                </div>
              </article>
              {decision ? (
                <p
                  className="jiuwenmate-chat-message jiuwenmate-chat-message--user jiuwenmate-sharing-owner-decision"
                  data-testid="jiuwenmate-sharing-owner-decision"
                  data-variant={status}
                >
                  {decision}
                </p>
              ) : null}
              {/* The decision bubble is an audit record from persisted approval state; it never enters chat.send. */}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
