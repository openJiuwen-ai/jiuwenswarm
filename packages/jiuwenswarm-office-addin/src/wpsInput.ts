/**
 * WPS delivers Command/Ctrl+V to both its task pane and the presentation. While a text field of the pane has the
 * focus, the presentation's paste is cancelled; the field's own paste still runs.
 */
import type { WpsObject } from './wpsPowerPoint';

export function isolateTaskPaneInput(application: WpsObject, root: Pick<Document, 'activeElement' | 'hasFocus'>): void {
  application.ApiEvent?.AddApiEventListener?.('DocumentBeforePaste', (...args: unknown[]) => {
    const field = root.activeElement as HTMLElement | null;
    const typing = root.hasFocus() && (field?.tagName === 'INPUT' || field?.tagName === 'TEXTAREA'
      || field?.isContentEditable === true);
    const cancel = args.at(-1) as { Value?: boolean } | undefined;
    if (typing && cancel && typeof cancel === 'object') cancel.Value = true;
  });
}
