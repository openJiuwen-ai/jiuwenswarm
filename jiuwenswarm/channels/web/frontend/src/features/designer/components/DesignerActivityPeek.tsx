import { BrainCircuit, Loader2, Wrench } from 'lucide-react';
import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { designerActivityItems } from '../designerActivity';
import type { DesignerNodeState } from '../executionGraphTypes';

type DesignerActivityPeekProps = {
  state?: Pick<DesignerNodeState, 'activity' | 'activity_tail' | 'activity_log'> | null;
  testId?: string;
};

export function DesignerActivityPeek({ state, testId = 'designer-node-activity' }: DesignerActivityPeekProps) {
  const { t } = useTranslation();
  const items = useMemo(() => designerActivityItems(state).slice(-3), [state]);

  if (items.length === 0) return null;

  return (
    <ol className="designer-node-activity" aria-label={t('designer.activity.progress')} data-testid={testId}>
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
