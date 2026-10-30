import { Empty } from 'antd';
import type { WebUIAdapterProps } from '../../types.ts';

export function WebUIEmpty({ node, appearance }: WebUIAdapterProps) {
  const p = node.props as { image?: string; description?: string };
  const image = p.image === 'default' ? Empty.PRESENTED_IMAGE_DEFAULT : p.image === 'simple' ? Empty.PRESENTED_IMAGE_SIMPLE : p.image;
  return <Empty data-testid="webui-empty" image={image} description={p.description} className={appearance?.className} style={appearance?.style} />;
}
