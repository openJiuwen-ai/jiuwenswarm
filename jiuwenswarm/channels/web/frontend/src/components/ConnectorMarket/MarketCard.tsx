import { Plus, Loader2, AlertCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { AvatarStyle } from '../../utils/skillAvatar';
import { EntityAvatar } from './EntityAvatar';
import { NewConversationIcon } from './icons';
import { TruncatedText } from './TruncatedText';
import type { McpCardState } from './mcpState';
import { busyLabelKey } from './mcpState';
import type { McpBusyKind } from '../../types/connector';
import './ConnectorMarket.css';

interface MarketCardProps {
  title: string;
  description: string;
  avatar: AvatarStyle;
  /** 后端真实图标地址，有就优先展示，没有/加载失败回退成 avatar 生成的首字符色块。 */
  iconUrl?: string;
  /**
   * 卡片统一状态机（见 mcpState.ts）。idle→"+"联接；connecting→spinner；connected→会话使用；
   * error→红点+"连接失败"+可重试"+"。
   *
   * 2026-08-15：广场卡片列表这一级不再放"卸载/解绑"按钮，也不再有全局启用/禁用开关——按钮矩阵
   * 新方案里卡片列表右侧只有"会话使用"或"+"两选一（见 state-model-rectification-v2-
   * remove-global-toggle.md），卸载操作统一挪到详情页（McpDetailPage.tsx/PluginDetailPage.tsx）。
   */
  state: McpCardState;
  /** state==='connecting' 时用来选精确文案（连接中/解绑中/删除中），不传就统一显示"连接中"
   * ——registerCustom 占位卡、后端真实推的 connecting 中间态都不经过 busyMap，没有具体种类可传，
   * 语义上仍然是"正在连接"，回退到默认文案是准确的，见 mcpState.ts busyLabelKey。 */
  busyKind?: McpBusyKind;
  canOpenDetail: boolean;
  onOpenDetail: () => void;
  onQuickAdd: () => void;
  quickAction?: 'install' | 'connect';
  /** "会话使用"图标点击——跳新会话并顺带打开这个扩展的会话内启用开关（2026-08-18 接入，
   * 见 ConnectorMarket/index.tsx 的 onUseExtension）。不传时（比如脱离 App.tsx 环境单独渲染）
   * 退化成"还没接入"提示，不要复用 onQuickAdd（那是安装/联接语义，已连接态点它什么都不会发生，
   * 会让用户以为按钮坏了）。 */
  onUse?: () => void;
}

// 广场卡片（插件/MCP 共用的展示壳，插件和MCP各自把 ConnectorSummary/PluginPackageSummary
// 归一化成 McpCardState + 这几个 primitive props 再传进来）。点击"+"是"安装/联接"语义。
// 卸载/删除只在详情页出现，见文件头注释。
export function MarketCard({
  title,
  description,
  avatar,
  iconUrl,
  state,
  busyKind,
  canOpenDetail,
  onOpenDetail,
  onQuickAdd,
  quickAction = 'install',
  onUse,
}: MarketCardProps) {
  const { t } = useTranslation();
  const showUseIcon = state === 'connected';
  const showInstall = state === 'idle' || state === 'error';
  const showConnecting = state === 'connecting';

  return (
    // h-full 让卡片撑满 grid 分给它的整行高度（grid 默认按行内最高的格子拉伸每一格，但格子里的
    // 内容不会自动填满，除非显式 h-full）——同一行内哪怕这张卡自身内容更矮，边框也要跟最高的
    // 那张对齐，不能各算各的高度。
    <div className="relative h-full min-h-[160px]">
      <div
        role={canOpenDetail ? 'button' : undefined}
        tabIndex={canOpenDetail ? 0 : undefined}
        onClick={canOpenDetail ? onOpenDetail : undefined}
        onKeyDown={
          canOpenDetail
            ? (event) => {
                if (event.key === 'Enter' || event.key === ' ') onOpenDetail();
              }
            : undefined
        }
        className={`flex h-full min-h-[160px] w-full flex-col gap-2 rounded-xl border border-border bg-card p-6 text-left transition-shadow ${
          canOpenDetail ? 'cursor-pointer hover:shadow-[0_4px_16px_rgba(0,0,0,0.08)]' : ''
        }`}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="flex min-w-0 items-center gap-2.5">
            <EntityAvatar
              iconUrl={iconUrl}
              avatar={avatar}
              className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg text-[16px] font-semibold"
            />
            <span className="truncate text-[18px] font-semibold leading-7 text-text">{title}</span>
            {state === 'error' && (
              <span
                data-tooltip={t('connectorMarket.card.stateError')}
                className="flex h-4 w-4 shrink-0 items-center justify-center text-danger"
                title={t('connectorMarket.card.stateError')}
              >
                <AlertCircle size={14} />
              </span>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2" onClick={(event) => event.stopPropagation()}>
            {showConnecting && (
              <span className="flex items-center gap-1 text-[12px] text-text-muted">
                <Loader2 size={13} className="animate-spin" />
                {t(busyLabelKey(busyKind))}
              </span>
            )}
            {showInstall && (
              <button
                type="button"
                onClick={onQuickAdd}
                data-tooltip={
                  state === 'error' ? t('connectorMarket.card.retry') : t(`connectorMarket.card.${quickAction}`)
                }
                aria-label={
                  state === 'error' ? t('connectorMarket.card.retry') : t(`connectorMarket.card.${quickAction}`)
                }
                className="connector-market-icon-btn flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-bg-muted/75 text-[color:var(--color-text-placeholder)] transition-colors hover:bg-connector-add-hover-surface hover:text-[color:var(--color-chat-accent)]"
              >
                <Plus size={16} strokeWidth={2.5} />
              </button>
            )}
            {showUseIcon && (
              <button
                type="button"
                onClick={onUse}
                data-tooltip={t('connectorMarket.card.use')}
                aria-label={t('connectorMarket.card.use')}
                className="connector-market-icon-btn flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-bg-muted/75 text-text transition-colors hover:bg-connector-add-hover-surface hover:text-[color:var(--color-chat-accent)]"
              >
                <NewConversationIcon size={15} />
              </button>
            )}
          </div>
        </div>
        {/* 2026-08-10：min-h 锁死 2 行的高度（leading-[22px]*2），不管描述是 0/1/2 行，这块
            占位永远一样高——不加这个的话，短描述/无描述的卡片会比 2 行描述的卡片矮一截，
            同一行内卡片高度参差不齐（line-clamp 只封顶行数，不会把矮的撑高）。真被截断时鼠标
            悬停会弹出完整文案，见 TruncatedText.tsx。 */}
        <TruncatedText
          text={description}
          className="mt-1 line-clamp-2 min-h-[44px] text-[14px] leading-[22px] text-text-muted"
        />
      </div>
    </div>
  );
}
