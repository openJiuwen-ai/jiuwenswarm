import { BrainCircuit, Loader2, Wrench } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { designerActivityItems, designerActivityLines } from '../designerActivity';
import type { DesignerNodeState } from '../executionGraphTypes';

type DesignerActivityPeekProps = {
  state?: Pick<DesignerNodeState, 'activity' | 'activity_tail' | 'activity_log'> | null;
  testId?: string;
  variant?: 'node' | 'leader';
};

export function DesignerActivityPeek({
  state,
  testId = 'designer-activity-peek',
  variant = 'node',
}: DesignerActivityPeekProps) {
  const { t } = useTranslation();
  const lines = useMemo(() => designerActivityLines(state), [state]);
  const items = useMemo(() => designerActivityItems(state).slice(-3), [state]);
  const [index, setIndex] = useState(0);

  useEffect(() => {
    setIndex(Math.max(0, lines.length - 1));
  }, [lines]);

  useEffect(() => {
    if (lines.length < 2) return undefined;
    const timer = window.setInterval(() => {
      setIndex((current) => (current + 1) % lines.length);
    }, 1400);
    return () => window.clearInterval(timer);
  }, [lines]);

  if (lines.length === 0) return null;

  if (variant === 'node') {
    return (
      <ol
        className="designer-node-activity"
        aria-label={t('designer.activity.progress')}
        data-testid={testId}
        data-variant={variant}
      >
        {items.map((item, itemIndex) => {
          const latest = itemIndex === items.length - 1;
          const isTool = item.kind === 'tool_call';
          const isThinking = item.kind === 'thinking';
          const Icon = isTool ? Wrench : isThinking ? BrainCircuit : Loader2;
          const label = isTool
            ? t('designer.activity.toolCall', { tool: item.tool || 'tool' })
            : isThinking
              ? t('designer.activity.thinking')
              : t('designer.activity.stage');
          const text = item.text || item.tool;
          return (
            <li
              key={`${item.at ?? itemIndex}:${item.kind}:${item.tool}:${text}`}
              className={`designer-node-activity__item${latest ? ' is-current' : ''}`}
              data-kind={item.kind}
              data-current={latest ? 'true' : 'false'}
            >
              <Icon
                className={`designer-node-activity__icon${latest && !isThinking && !isTool ? ' is-spinning' : ''}`}
                size={12}
                aria-hidden
              />
              <span className="designer-node-activity__copy">
                <span className="designer-node-activity__label">{label}</span>
                <strong title={text}>{text}</strong>
              </span>
            </li>
          );
        })}
      </ol>
    );
  }

  const current = lines[Math.min(index, lines.length - 1)] || lines[0];
  const previous = lines.length > 1 ? lines[(index - 1 + lines.length) % lines.length] : '';

  return (
    <span
      className={`designer-activity-peek designer-activity-peek--${variant}`}
      data-testid={testId}
      data-variant={variant}
    >
      {previous ? <em>{previous}</em> : null}
      <strong>{current}</strong>
    </span>
  );
}
