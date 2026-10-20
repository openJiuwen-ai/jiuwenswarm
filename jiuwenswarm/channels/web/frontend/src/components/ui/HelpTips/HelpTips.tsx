import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import './HelpTips.css';

/**
 * Form-field "?" hint. Uses a portaled adaptive tooltip instead of the native
 * `title` attribute so tips stay visible near modal dialog edges / scroll areas
 * (see GitCode #5248).
 */
export function HelpTips({ content }: { content: string }) {
  const { tooltip, handlers } = useAdaptiveTooltip({
    placement: 'top',
    maxWidth: 320,
    multiline: true,
  });
  return (
    <>
      <button
        type="button"
        className="ui-help-tips"
        data-tooltip={content}
        data-testid="ui-help-tips"
        aria-label={content}
        {...handlers}
      >
        ?
      </button>
      {tooltip}
    </>
  );
}
