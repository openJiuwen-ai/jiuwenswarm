import './HelpTips.css';
export function HelpTips({ content }: { content: string }) {
  return (
    <button type="button" className="ui-help-tips" title={content} aria-label={content}>
      ?
    </button>
  );
}
