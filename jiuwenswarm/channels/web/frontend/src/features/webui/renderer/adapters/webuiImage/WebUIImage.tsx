import { Image } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUIImage({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as { src: string; alt?: string; width?: number | string; height?: number | string;
    fallback?: string; preview?: boolean | { src: string } };
  return <Image data-testid="webui-image" src={p.src} alt={p.alt} width={p.width} height={p.height}
    className={appearance?.className} style={appearance?.style}
    fallback={p.fallback} preview={p.preview} />;
}
