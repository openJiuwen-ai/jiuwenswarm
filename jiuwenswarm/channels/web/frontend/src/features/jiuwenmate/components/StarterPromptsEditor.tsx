import { Plus, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { ActionMenu } from './ActionMenu';

/** The private-chat welcome shows at most this many prompts. */
export const MAX_STARTER_PROMPTS = 3;
const MAX_STARTER_PROMPT_LENGTH = 512;

/** Mirrors the server: trimmed, blanks dropped, first occurrence kept. */
export function normalizeStarterPrompts(prompts: readonly string[]): string[] {
  const result: string[] = [];
  for (const item of prompts) {
    const value = item.trim();
    if (value && !result.includes(value)) result.push(value);
  }
  return result;
}

function isDuplicate(prompts: readonly string[], index: number): boolean {
  const value = prompts[index].trim();
  return value.length > 0 && prompts.slice(0, index).some((item) => item.trim() === value);
}

interface StarterPromptsProps {
  prompts: readonly string[];
  onChange: (prompts: string[]) => void;
}

export function StarterPromptsAddButton({ prompts, onChange }: StarterPromptsProps) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      className="jiuwenmate-collapsible__add-button"
      disabled={prompts.length >= MAX_STARTER_PROMPTS}
      title={prompts.length >= MAX_STARTER_PROMPTS ? t('jiuwenmate.create.starterPromptsLimit', { count: MAX_STARTER_PROMPTS }) : undefined}
      onClick={() => onChange([...prompts, ''])}
      data-testid="jiuwenmate-starter-prompts-add"
    >
      <Plus aria-hidden />
      <span>{t('jiuwenmate.create.add.starterPrompts')}</span>
    </button>
  );
}

export function StarterPromptsEditor({ prompts, onChange }: StarterPromptsProps) {
  const { t } = useTranslation();
  return (
    <div className="jiuwenmate-starter-prompts" data-testid="jiuwenmate-starter-prompts">
      {prompts.map((prompt, index) => {
        const duplicate = isDuplicate(prompts, index);
        return (
          <div key={index} className="jiuwenmate-starter-prompts__item">
            <div>
              <input
                value={prompt}
                maxLength={MAX_STARTER_PROMPT_LENGTH}
                aria-invalid={duplicate || undefined}
                onChange={(event) => onChange(prompts.map((item, itemIndex) => (itemIndex === index ? event.target.value : item)))}
                data-testid="jiuwenmate-starter-prompt-input"
              />
              <ActionMenu
                label={t('jiuwenmate.workspace.more')}
                items={[
                  {
                    key: 'delete',
                    label: t('jiuwenmate.office.delete'),
                    icon: Trash2,
                    onSelect: () => onChange(prompts.filter((_, itemIndex) => itemIndex !== index)),
                  },
                ]}
              />
            </div>
            {duplicate ? (
              <small role="alert" data-testid="jiuwenmate-starter-prompt-duplicate">
                {t('jiuwenmate.create.starterPromptDuplicate')}
              </small>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
