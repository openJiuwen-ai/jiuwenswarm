import type { WebUIAdapterProps } from '../../types.ts';
import { useContentActivity } from '../../ContentActivity.tsx';
import { Iframe } from './Iframe.tsx';
import type { LocalizedText } from '../../../catalog/components/localizedText.ts';
import { useLocalizedText } from '../../useLocalizedText.ts';

export function WebUIIframe({ node, context, appearance }: WebUIAdapterProps) {
  const active = useContentActivity();
  const p = node.props as { src?: string; srcDoc?: LocalizedText; title: LocalizedText; minWidth?: number };
  const title = useLocalizedText(p.title)!;
  const srcDoc = useLocalizedText(p.srcDoc);
  return (
    <Iframe
      src={p.src}
      srcDoc={srcDoc}
      title={title}
      minWidth={p.minWidth}
      active={active && context.connected}
      className={appearance?.className}
      style={appearance?.style}
    />
  );
}
