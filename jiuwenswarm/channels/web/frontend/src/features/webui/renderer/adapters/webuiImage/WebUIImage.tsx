import { Image } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';
import { useGatewayResource, useGatewayResourceState } from '../../../client/GatewayResources.tsx';

export function WebUIImage({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as { src: string; alt?: string; width?: number | string; height?: number | string;
    fallback?: string; preview?: boolean | { src: string }; fit?: 'contain' | 'cover' };
  const resource = useGatewayResourceState(p.src);
  const fallback = useGatewayResource(p.fallback);
  const previewSrc = useGatewayResource(typeof p.preview === 'object' ? p.preview.src : undefined);
  if (resource.pending) return null;
  return <Image data-testid="webui-image" src={resource.url} alt={p.alt} width={p.width} height={p.height}
    className={appearance?.className} style={{ objectFit: p.fit, ...appearance?.style }}
    fallback={fallback} preview={typeof p.preview === 'object' ? { src: previewSrc } : p.preview} />;
}
