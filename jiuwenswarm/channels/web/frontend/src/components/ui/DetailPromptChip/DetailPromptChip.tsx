import type { ReactNode } from 'react';

export interface DetailPromptChipProps {
  /** chip 文案（超长单行省略，由 .detail-chip--spread 保证） */
  text: string;
  /** 右侧动作图标（纯装饰 span，颜色随行文字 hover 变 accent）；纯展示态不渲染 */
  icon?: ReactNode;
  /** 传入则渲染为可点 button（.detail-chip--link 整行可点），不传渲染为纯展示 span */
  onClick?: () => void;
  disabled?: boolean;
  testId?: string;
  variant?: string | number;
  className?: string;
}

// 详情页快捷输入/示例 chip（样式在 index.css 的 .detail-chip 家族）：文案 + 右侧动作图标
// 两端对齐、每项独占一行（容器用 .detail-prompt-list）。专家详情快捷输入与连接器详情
// "试试这样用"示例共用本组件，保证图标/文案位置与规格不再各自内联漂移。
export function DetailPromptChip({ text, icon, onClick, disabled, testId, variant, className }: DetailPromptChipProps) {
  const classes = ['detail-chip', className].filter(Boolean).join(' ');
  if (!onClick) {
    return (
      <span className={classes} data-testid={testId} data-variant={variant}>
        {text}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`${classes} detail-chip--link detail-chip--spread`}
      data-testid={testId}
      data-variant={variant}
    >
      <span>{text}</span>
      {icon != null ? (
        <span className="detail-chip__icon" aria-hidden="true">
          {icon}
        </span>
      ) : null}
    </button>
  );
}
