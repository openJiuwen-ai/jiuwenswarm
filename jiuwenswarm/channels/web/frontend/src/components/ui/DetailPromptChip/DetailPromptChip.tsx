import PromptSendIcon from '../../../assets/agent-management/prompt-send.svg?react';

export interface DetailPromptChipProps {
  /** chip 文案（超长单行省略，由 .detail-chip--spread 保证） */
  text: string;
  /** 传入则渲染为可点 button（药丸底/图标/配色全部内聚，不接受样式透传）；不传渲染为纯展示 span */
  onClick?: () => void;
  disabled?: boolean;
  testId?: string;
  variant?: string | number;
}

// 详情页快捷输入/示例 chip（样式在 index.css 的 .detail-chip 家族）：文案 + 右侧动作图标
// 两端对齐、每项独占一行（容器用 .detail-prompt-list）。专家详情快捷输入与连接器详情
// "试试这样用"示例共用本组件，图标与配色收敛在组件内部，各调用方只传数据与回调。
export function DetailPromptChip({ text, onClick, disabled, testId, variant }: DetailPromptChipProps) {
  if (!onClick) {
    return (
      <span className="detail-chip" data-testid={testId} data-variant={variant}>
        {text}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="detail-chip detail-chip--weak detail-chip--link detail-chip--spread"
      data-testid={testId}
      data-variant={variant}
    >
      <span>{text}</span>
      <span className="detail-chip__icon" aria-hidden="true">
        <PromptSendIcon width={16} height={16} />
      </span>
    </button>
  );
}
