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

interface MyMarketCardProps {
  title: string;
  description: string;
  avatar: AvatarStyle;
  /** 后端真实图标地址，有就优先展示，没有/加载失败回退成 avatar 生成的首字符色块。 */
  iconUrl?: string;
  /**
   * 卡片统一状态机（见 mcpState.ts）。idle→"+"联接；connecting→spinner 占位；connected→会话
   * 使用；error→红点+"连接失败"+可重试"+"。
   *
   * 2026-08-15：卡片列表这一级不再放"删除"按钮，也不再有全局启用/禁用开关——按钮矩阵新方案里
   * "我的"卡片列表右侧只有"会话使用"或"+"两选一，卸载操作统一挪到详情页（见
   * state-model-rectification-v2-remove-global-toggle.md）。
   */
  state: McpCardState;
  /** state==='connecting' 时用来选精确文案（连接中/解绑中/删除中），不传就统一显示"连接中"——
   * 见 MarketCard.tsx 同名 prop 的注释，两边道理一样。 */
  busyKind?: McpBusyKind;
  onOpenDetail: () => void;
  /**
   * 卡片能不能点进详情页。按钮矩阵：MCP 在"已安装+未连接"这个中间态也要能进详情页（展示断联
   * banner），只有真正"未安装"（从未连接过的 built-in）才拒绝——不再是旧版"不是 connected
   * 就整个拒绝"。插件不管可不可用都能进详情页（不可用态详情页要展示"安装"/断联 banner）。
   * 默认 true——调用方（MarketplacePage）只在渲染 MCP 卡片时按可达态传这个 prop，插件调用方
   * 不传，维持"永远能点"的旧行为。
   */
  canOpenDetail?: boolean;
  /** "会话使用"点击——跳新会话并顺带打开这个扩展的会话内启用开关（2026-08-18 接入，
   * 见 ConnectorMarket/index.tsx 的 onUseExtension）。不传时退化成"还没接入"提示。 */
  onUse?: () => void;
  /** 不可用态点击"+"——插件是安装，MCP 是打开联接弹窗。不传则不可用态不渲染"+"。 */
  onQuickInstall?: () => void;
  quickAction?: 'install' | 'connect';
}

// "我的扩展"卡片。2026-08-15 简化：卡片列表层级不再区分插件/MCP 的按钮组合（都只有"会话使用"
// 或"+"），删除/卸载操作统一挪到详情页（McpDetailPage.tsx/PluginDetailPage.tsx），这里不再需要
// enabled/onDelete 这类 per-实体差异化 prop。
export function MyMarketCard({
  title,
  description,
  avatar,
  iconUrl,
  state,
  busyKind,
  onOpenDetail,
  canOpenDetail = true,
  onUse,
  onQuickInstall,
  quickAction = 'install',
}: MyMarketCardProps) {
  const { t } = useTranslation();
  const showUse = state === 'connected';
  const showInstall = (state === 'idle' || state === 'error') && onQuickInstall !== undefined;
  const showConnecting = state === 'connecting';

  return (
    // h-full 让卡片撑满 grid 分给它的整行高度，同款处理见 MarketCard.tsx。
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
          <div className="flex shrink-0 items-center gap-2.5" onClick={(event) => event.stopPropagation()}>
            {showConnecting ? (
              <span className="flex items-center gap-1 text-[12px] text-text-muted">
                <Loader2 size={13} className="animate-spin" />
                {t(busyLabelKey(busyKind))}
              </span>
            ) : (
              <>
                {showUse && (
                  <button
                    type="button"
                    onClick={onUse}
                    data-tooltip={t('connectorMarket.card.use')}
                    className="connector-market-icon-btn flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-bg-muted/75 text-text-muted transition-colors hover:bg-connector-add-hover-surface hover:text-[color:var(--color-chat-accent)]"
                  >
                    <NewConversationIcon size={14} />
                  </button>
                )}
                {showInstall && (
                  <button
                    type="button"
                    data-tooltip={
                      state === 'error' ? t('connectorMarket.card.retry') : t(`connectorMarket.card.${quickAction}`)
                    }
                    onClick={onQuickInstall}
                    className="connector-market-icon-btn flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-bg-muted/75 text-[color:var(--color-text-placeholder)] transition-colors hover:bg-connector-add-hover-surface hover:text-[color:var(--color-chat-accent)]"
                  >
                    <Plus size={15} strokeWidth={2.5} />
                  </button>
                )}
              </>
            )}
          </div>
        </div>
        {/* 2026-08-10：min-h 锁死 2 行的高度（leading-[22px]*2），同款处理见 MarketCard.tsx——
            "我的扩展"这边同样会出现描述长短不一/为空导致卡片高度参差的问题。真被截断时鼠标
            悬停会弹出完整文案，见 TruncatedText.tsx。 */}
        <TruncatedText
          text={description}
          className="mt-1 line-clamp-2 min-h-[44px] text-[14px] leading-[22px] text-text-muted"
        />
      </div>
    </div>
  );
}
