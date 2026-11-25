import type { WebUIAdapterProps } from '../../types.ts';

/** The host owns the entry title and selection; this adapter renders only the body. */
export function WebUIPanel({ children, appearance }: WebUIAdapterProps) {
  return <div className={appearance?.className} style={appearance?.style} data-testid="webui-panel">{children}</div>;
}
