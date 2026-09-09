import { useEffect } from 'react';
import { AlertCircle, CheckCircle2, X } from 'lucide-react';

interface ToastProps {
  message: string;
  onClose: () => void;
  /**
   * 'success'（默认）：绿色+对勾，原有的操作成功提示。
   * 'error'：红色+警示图标，2026-08-11 新增——之前 connectorStore/pluginPackageStore 的
   * action 失败只会把 error 写进 store，没有任何地方读出来展示，用户点了操作没反应还不知道
   * 为什么（真实案例：连接飞书 MCP 时后端返回 CLI 版本检测失败，前端一声不吭）。停留时间比
   * success 长一倍（5200ms vs 2600ms）——错误信息通常比"已连接"这种短句长，需要更多时间读完。
   */
  variant?: 'success' | 'error';
}

export function Toast({ message, onClose, variant = 'success' }: ToastProps) {
  const isError = variant === 'error';

  useEffect(() => {
    const timer = setTimeout(onClose, isError ? 5200 : 2600);
    return () => clearTimeout(timer);
  }, [onClose, isError]);

  // 注意：hover 态的颜色不能用模板字符串动态拼（如 `hover:${colorClasses.text}`）——Tailwind 在
  // 编译期按完整类名字符串扫描生成 CSS，动态拼出来的 `hover:text-[#C0332B]` 扫不到、不会生成，
  // hover 永远不生效。所以两种 variant 各自的完整 className（含 hover:）都写成静态字面量。
  const palette = isError
    ? 'bg-[#FDECEC] text-[#C0332B] hover:bg-[#F7DADA]'
    : 'bg-[#EAF9EE] text-[#1A8A3D] hover:bg-[#D7F1DF]';

  return (
    <div className="fixed right-6 top-6 z-[60]">
      <div className={`flex max-w-md items-start gap-2 rounded-lg ${palette} px-3 py-2 text-[13px] shadow-md`} data-testid="connector-market-toast" data-variant={variant}>
        {isError ? <AlertCircle size={16} className="mt-0.5 shrink-0" /> : <CheckCircle2 size={16} className="mt-0.5 shrink-0" />}
        {/* min-w-0 + flex-1 是关键：flex item 默认 min-width:auto（=内容宽度），遇到超长不可断
            token（如 server_id='mcp_xxx_1234567890'）时 break-words 只能在 span 内部换行，span
            本身拒绝收缩、撑爆 max-w-md，把 shrink-0 的关闭按钮推出屏幕右外——按钮还在但点不到。
            min-w-0 让 span 能收缩到容器宽度内换行，按钮留在可视区。max-h + overflow 防止后端
            真实错误（动辄几百字符、含 server_config dump）把 Toast 撑成半屏高挡住界面。 */}
        <span className="min-w-0 flex-1 break-words max-h-60 overflow-y-auto">{message}</span>
        <button
          type="button"
          onClick={onClose}
          aria-label="close"
          className="ml-1 shrink-0 rounded p-0.5 opacity-70 hover:opacity-100"
          data-testid="connector-market-toast-close"
        >
          <X size={14} />
        </button>
      </div>
    </div>
  );
}
