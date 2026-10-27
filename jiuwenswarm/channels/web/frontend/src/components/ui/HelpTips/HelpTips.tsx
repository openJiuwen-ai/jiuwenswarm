import { useLayoutEffect, useRef } from 'react';
import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import './HelpTips.css';

export function HelpTips({ content, size = 'default' }: { content: string; size?: 'default' | 'large' }) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  // 表单可能位于 showModal 打开的 <dialog>（top layer）内，提示必须挂进 dialog 自身，
  // portal 到 body 会被弹窗整体盖住；非弹窗场景回退 document.body，与 hook 默认行为一致。
  const containerRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    containerRef.current = buttonRef.current?.closest('dialog') ?? document.body;
  }, []);
  // 不再依赖浏览器原生 title 提示（约 1 秒延迟、鼠标轻微移动即消失，表现为"有的区域能显示有的不能"），
  // 统一走 useAdaptiveTooltip：hover 立即显示并自动避让视口边缘。
  const { tooltip, handlers } = useAdaptiveTooltip({ placement: 'top', containerRef });
  return (
    <button
      ref={buttonRef}
      type="button"
      className={`ui-help-tips${size === 'large' ? ' ui-help-tips--large' : ''}`}
      data-tooltip={content}
      aria-label={content}
      data-testid="ui-help-tips-trigger"
      data-variant={size}
      {...handlers}
    >
      ?
      {tooltip}
    </button>
  );
}
