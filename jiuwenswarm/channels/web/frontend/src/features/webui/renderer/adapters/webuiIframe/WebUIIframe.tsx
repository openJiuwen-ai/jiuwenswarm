import type { WebUIAdapterProps } from '../../types.ts';
import { useContentActivity } from '../../ContentActivity.tsx';
import { Iframe } from './Iframe.tsx';

export function WebUIIframe({ node, context, appearance }: WebUIAdapterProps) {
  const active = useContentActivity();
  const p = node.props as { src: string; title: string };
  return <Iframe src={p.src} title={p.title} active={active && context.connected}
    className={appearance?.className} style={appearance?.style} />;
}
