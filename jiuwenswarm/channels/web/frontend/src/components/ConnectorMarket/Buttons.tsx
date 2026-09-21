// 详情页共用的视觉部件，PluginDetailPage.tsx / McpDetailPage.tsx 都要用——之前是各自内联
// 一份几乎一样的 JSX，容易改一处漏一处（见 state-model-rectification.md §5 的教训），提出来共用。
//
// 2026-08-15：DetailToggleSwitch（全局启用/禁用开关）已删除，插件/MCP 都不再有这个状态维度，
// 见 state-model-rectification-v2-remove-global-toggle.md。
// 2026-09-11：CapabilityGrid（能力卡片网格）已删除——技能/工具/Rail/MCP 卡片统一改用
// 共享组件 ui/PageCard（与列表页卡片同款），不再保留本模块的私有网格实现。

export function PillButton({
  icon,
  label,
  onClick,
  disabled,
}: {
  icon?: React.ReactNode;
  label: string;
  onClick?: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="flex h-8 items-center justify-center gap-1 rounded-full border border-[color:var(--color-chat-supporting-text)] bg-card px-4 text-[13px] text-text disabled:opacity-60"
    >
      {icon}
      {label}
    </button>
  );
}

export function DetailLinkButton({
  icon,
  label,
  onClick,
  danger,
  disabled,
}: {
  icon?: React.ReactNode;
  label: string;
  onClick: () => void;
  danger?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={`flex items-center gap-1 text-[13px] text-text disabled:opacity-60 ${danger ? 'hover:text-danger' : 'hover:text-[color:var(--color-chat-accent)]'}`}
    >
      {icon}
      {label}
    </button>
  );
}

// 详情页能力卡片（PageCard avatar 位）的图标底座：铺满 48px 头像容器（宽高 100%，与
// .entity-header__avatar-letter 的铺满方式一致）、圆角 10px 对齐容器裁切，图标 21px、
// 颜色 text-text（浅色主题 #191919，跟随主题 token）。
export function IconAvatar({ icon }: { icon: React.ReactNode }) {
  return (
    <span className="flex h-full w-full shrink-0 items-center justify-center rounded-[10px] border border-connector-tool-icon-border bg-connector-tool-icon-surface text-text">
      {icon}
    </span>
  );
}
