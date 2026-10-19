/** Text blocks of the diagnosis page: Markdown written by the model, and plain signal text. */

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MarkdownText } from '../primitives/index.ts';
import { splitLeadParagraph } from './diagnosisView';
import css from './DiagnosisSubpage.module.css';

function FoldToggle({ expanded, onToggle, testId }: { expanded: boolean; onToggle: () => void; testId: string }) {
  const { t } = useTranslation();
  return (
    <button
      type="button"
      className={css.linkButton}
      aria-expanded={expanded}
      onClick={onToggle}
      data-testid={testId}
    >
      {t(expanded ? 'trajectory.diagnosis.collapse' : 'trajectory.diagnosis.expandAll')}
    </button>
  );
}

/**
 * Markdown the model wrote (root cause, fix, Skill suggestion).
 *
 * Shows the first paragraph and folds the rest behind "show all"; a single
 * paragraph is shown whole.
 */
export function FoldedMarkdown({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const { lead, rest } = splitLeadParagraph(text);
  if (lead === '') return null;
  return (
    <div className={css.markdownBox} data-testid="trajectory-diagnosis-markdown">
      <MarkdownText text={expanded || rest === '' ? text.trim() : lead} />
      {rest === '' ? null : (
        <FoldToggle
          expanded={expanded}
          onToggle={() => setExpanded(value => !value)}
          testId="trajectory-diagnosis-markdown-toggle"
        />
      )}
    </div>
  );
}

/** Markdown shown whole, without folding. */
export function WholeMarkdown({ text }: { text: string }) {
  if (text.trim() === '') return null;
  return (
    <div className={css.markdownBox}>
      <MarkdownText text={text.trim()} />
    </div>
  );
}

const PLAIN_FOLD_LENGTH = 200;
const PLAIN_FOLD_LINES = 4;

/** Signal text as written, never rendered as Markdown; long text folds to a few lines. */
export function PlainFold({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const foldable = text.length > PLAIN_FOLD_LENGTH || text.split('\n').length > PLAIN_FOLD_LINES;
  if (text.trim() === '') return null;
  return (
    <div className={css.plainBox}>
      <p
        className={`${css.plainText} ${foldable && !expanded ? css.plainClamped : ''}`}
        data-testid="trajectory-diagnosis-plain-text"
      >
        {text}
      </p>
      {foldable ? (
        <FoldToggle
          expanded={expanded}
          onToggle={() => setExpanded(value => !value)}
          testId="trajectory-diagnosis-plain-toggle"
        />
      ) : null}
    </div>
  );
}
