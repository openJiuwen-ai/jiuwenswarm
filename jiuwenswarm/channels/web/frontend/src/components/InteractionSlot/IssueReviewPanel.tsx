import { useId, useState } from 'react';
import { ExternalLink } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { AskUserQuestionPayload, IssueReviewDraft, UserAnswer } from '../../types';
import { Button } from '../ui/Button/Button';
import { Tag } from '../ui/Tag/Tag';
import { buildAuthorizationAnswers, resolveAuthorizationActions, type AuthSemantic } from './promptRouting';

interface IssueReviewPanelProps {
  pending: AskUserQuestionPayload;
  draft: IssueReviewDraft;
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => Promise<boolean>;
}

export function IssueReviewPanel({ pending, draft, onSubmit }: IssueReviewPanelProps) {
  const { t } = useTranslation();
  const titleId = useId();
  const descriptionId = useId();
  const [submitting, setSubmitting] = useState<'allow-once' | 'reject' | null>(null);
  const [submitFailed, setSubmitFailed] = useState(false);
  const actions = resolveAuthorizationActions(pending.questions);
  const repo = new URL(draft.tracker_url).pathname.replace(/^\/|\/issues$/g, '');

  async function submit(semantic: 'allow-once' | 'reject') {
    const action = actions.find((option) => option.semantic === semantic);
    const answers = action && buildAuthorizationAnswers(pending.questions, action);
    if (submitting || !answers) return;
    setSubmitting(semantic);
    setSubmitFailed(false);
    try {
      setSubmitFailed(!(await onSubmit(pending.request_id, answers, pending.source)));
    } catch {
      setSubmitFailed(true);
    } finally {
      setSubmitting(null);
    }
  }

  function actionDisabled(semantic: AuthSemantic) {
    return submitting !== null || !actions.some((action) => action.semantic === semantic);
  }

  return (
    <section
      className="issue-review"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      aria-busy={submitting !== null}
      data-testid="interaction-slot-issue-review"
    >
      <header className="issue-review__header">
        <h2 id={titleId} data-testid="interaction-slot-issue-review-heading">{t('issueReview.heading')}</h2>
        <p id={descriptionId} data-testid="interaction-slot-issue-review-description">
          {t('issueReview.publicNotice')}
        </p>
      </header>
      <div
        className="issue-review__draft"
        data-testid="interaction-slot-issue-review-draft"
        tabIndex={0}
        aria-label={t('issueReview.draft')}
        role="region"
      >
        <dl className="issue-review__details" data-testid="interaction-slot-issue-review-details">
          <dt data-testid="interaction-slot-issue-review-destination-label">{t('issueReview.destination')}</dt>
          <dd>
            <a
              className="issue-review__destination"
              href={draft.tracker_url}
              target="_blank"
              rel="noreferrer"
              data-testid="interaction-slot-issue-review-destination"
            >
              {repo}
              <ExternalLink size={13} aria-hidden="true" />
            </a>
          </dd>
          <dt data-testid="interaction-slot-issue-review-title-label">{t('issueReview.title')}</dt>
          <dd className="issue-review__title" data-testid="interaction-slot-issue-review-title">{draft.title}</dd>
          <dt data-testid="interaction-slot-issue-review-labels-heading">{t('issueReview.labels')}</dt>
          <dd className="issue-review__labels" data-testid="interaction-slot-issue-review-labels">
            {draft.labels.length ? draft.labels.map((label, index) => (
              <Tag key={`${label}-${index}`} data-testid="interaction-slot-issue-review-label" data-variant={label}>
                {label}
              </Tag>
            )) : t('issueReview.noLabels')}
          </dd>
        </dl>
        <div
          className="issue-review__body prose prose-sm max-w-none dark:prose-invert"
          data-testid="interaction-slot-issue-review-body"
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ children, href }) => <a href={href} target="_blank" rel="noreferrer">{children}</a>,
              img: ({ alt, src }) => <span>{`![${alt || ''}](${src || ''})`}</span>,
              table: ({ children }) => <div className="issue-review__table"><table>{children}</table></div>,
            }}
          >
            {draft.body}
          </ReactMarkdown>
        </div>
        <details className="issue-review__source" data-testid="interaction-slot-issue-review-source">
          <summary data-testid="interaction-slot-issue-review-source-toggle">{t('issueReview.markdownSource')}</summary>
          <pre data-testid="interaction-slot-issue-review-source-text">{draft.body}</pre>
        </details>
      </div>
      <footer className="issue-review__footer" data-testid="interaction-slot-issue-review-actions">
        {submitFailed && (
          <p role="alert" data-testid="interaction-slot-issue-review-error">{t('issueReview.submitFailed')}</p>
        )}
        <Button
          size="sm"
          disabled={actionDisabled('reject')}
          loading={submitting === 'reject'}
          onClick={() => void submit('reject')}
          data-testid="interaction-slot-issue-review-cancel"
        >
          {t('issueReview.cancel')}
        </Button>
        <Button
          size="sm"
          variant="primary"
          disabled={actionDisabled('allow-once')}
          loading={submitting === 'allow-once'}
          onClick={() => void submit('allow-once')}
          data-testid="interaction-slot-issue-review-publish"
        >
          {t('issueReview.publish')}
        </Button>
      </footer>
    </section>
  );
}
