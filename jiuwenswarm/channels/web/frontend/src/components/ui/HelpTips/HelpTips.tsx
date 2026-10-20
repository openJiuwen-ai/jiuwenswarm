import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import './HelpTips.css';

export function HelpTips({ content }: { content: string }) {
  const { tooltip, handlers } = useAdaptiveTooltip();

  return (
    <>
      <button type="button" className="ui-help-tips" title={content} aria-label={content} data-tooltip={content} {...handlers}>
        ?
      </button>
      {tooltip}
    </>
  );
}
