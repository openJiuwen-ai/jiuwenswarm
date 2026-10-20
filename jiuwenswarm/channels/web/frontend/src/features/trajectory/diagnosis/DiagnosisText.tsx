/** Text blocks of the diagnosis page: Markdown written by the model, and plain signal text. */

import { useLayoutEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MarkdownText } from '../primitives/index.ts';
import { splitLeadParagraph, stripLeadingHeading } from './diagnosisView';
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

/**
 * Markdown that may be long, held to a few lines with a fade. The toggle only appears once the
 * clamp really hides something, and nothing is cut at a paragraph break, so a leading heading never
 * stands in for the content.
 */
export function ClampedMarkdown({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = box.current;
    if (element !== null && !expanded) setOverflows(element.scrollHeight > element.clientHeight + 1);
  }, [text, expanded]);
  const body = stripLeadingHeading(text);
  if (body === '') return null;
  const clamped = !expanded;
  return (
    <div className={`${css.markdownBox} ${css.suggestionBody}`} data-testid="trajectory-diagnosis-markdown">
      <div
        ref={box}
        className={`${css.clampBox} ${clamped ? css.clampBoxClosed : ''} ${clamped && overflows ? css.clampBoxFade : ''}`}
      >
        <MarkdownText text={body} />
      </div>
      {overflows || expanded ? (
        <FoldToggle
          expanded={expanded}
          onToggle={() => setExpanded(value => !value)}
          testId="trajectory-diagnosis-markdown-toggle"
        />
      ) : null}
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

/** Text as written, never rendered as Markdown, shown whole. */
export function PlainText({ text }: { text: string }) {
  if (text.trim() === '') return null;
  return <p className={css.plainText} data-testid="trajectory-diagnosis-plain-text">{text}</p>;
}

/**
 * Text as written, never rendered as Markdown; text taller than a few lines folds, and the toggle
 * only appears once the clamp actually hides something.
 */
export function PlainFold({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const paragraph = useRef<HTMLParagraphElement>(null);
  useLayoutEffect(() => {
    const element = paragraph.current;
    if (element !== null && !expanded) setOverflows(element.scrollHeight > element.clientHeight + 1);
  }, [text, expanded]);
  if (text.trim() === '') return null;
  return (
    <div className={css.plainBox}>
      <p
        ref={paragraph}
        className={`${css.plainText} ${expanded ? '' : css.plainClamped}`}
        data-testid="trajectory-diagnosis-plain-text"
      >
        {text}
      </p>
      {overflows || expanded ? (
        <FoldToggle
          expanded={expanded}
          onToggle={() => setExpanded(value => !value)}
          testId="trajectory-diagnosis-plain-toggle"
        />
      ) : null}
    </div>
  );
}
