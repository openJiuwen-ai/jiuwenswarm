import { useCallback, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Package } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { AskUserQuestionPayload, QuestionOption, UserAnswer } from '../../types';

interface ExperiencePackagePromptProps {
  pending: AskUserQuestionPayload;
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => Promise<boolean>;
}

function optionValue(option: QuestionOption): string {
  return option.value || option.label;
}

export function ExperiencePackagePrompt({ pending, onSubmit }: ExperiencePackagePromptProps) {
  const { t } = useTranslation();
  const [submittingAction, setSubmittingAction] = useState<string | null>(null);
  const question = pending.questions?.[0];
  const actions = useMemo(() => {
    const options = question?.options ?? [];
    return {
      create: options.find((option) => optionValue(option) === 'install') ?? options[0],
      defer: options.find((option) => optionValue(option) === 'defer') ?? options[1],
    };
  }, [question]);

  const submit = useCallback(
    async (option: QuestionOption | undefined) => {
      if (!question || !option || submittingAction) return;
      const value = optionValue(option);
      setSubmittingAction(value);
      try {
        await onSubmit(
          pending.request_id,
          [{ question: question.question, selected_options: [value] }],
          pending.source,
        );
      } finally {
        setSubmittingAction(null);
      }
    },
    [onSubmit, pending, question, submittingAction],
  );

  if (!question) return null;

  return (
    <div
      className="experience-package-prompt"
      role="alertdialog"
      aria-label={question.header}
      data-testid="interaction-slot-experience-prompt"
      data-request-id={pending.request_id}
    >
      <div className="experience-package-prompt__head" data-testid="interaction-slot-experience-head">
        <Package className="experience-package-prompt__icon" size={18} strokeWidth={2} aria-hidden="true" />
        <span className="experience-package-prompt__title" data-testid="interaction-slot-experience-title">
          {question.header}
        </span>
      </div>

      <div className="experience-package-prompt__body chat-text" data-testid="interaction-slot-experience-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{question.question}</ReactMarkdown>
      </div>

      <div className="experience-package-prompt__actions" data-testid="interaction-slot-experience-actions">
        {actions.defer && (
          <button
            type="button"
            className="experience-package-prompt__button experience-package-prompt__button--secondary"
            disabled={submittingAction !== null}
            onClick={() => void submit(actions.defer)}
            data-testid="interaction-slot-experience-defer-button"
          >
            {actions.defer.label}
          </button>
        )}
        {actions.create && (
          <button
            type="button"
            className="experience-package-prompt__button experience-package-prompt__button--primary"
            disabled={submittingAction !== null}
            onClick={() => void submit(actions.create)}
            data-testid="interaction-slot-experience-create-button"
            data-variant={submittingAction === 'install' ? 'loading' : 'idle'}
          >
            {submittingAction === 'install' ? t('experiencePackagePrompt.creating') : actions.create.label}
          </button>
        )}
      </div>
    </div>
  );
}
