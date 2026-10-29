/**
 * 仅显式开发诊断使用：不参与生产默认降级路径，也不展示子节点。
 * 生产路径中缺失适配器会由 SchemaRenderer 直接抛错，整棵实例树进入失败态。
 */

export interface WebUIUnknownComponentProps {
  component: string;
}

export function WebUIUnknownComponent({ component }: WebUIUnknownComponentProps) {
  return (
    <div
      className="webui-unknown-component"
      data-testid="webui-renderer-unknown"
      data-variant={component}
      role="presentation"
    />
  );
}
