import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Search, X, Plus, Check } from 'lucide-react';

export interface PickerItem {
  id: string;
  name: string;
  description: string;
  render: (checked: boolean) => React.ReactNode;
}

interface PickerModalProps {
  title: string;
  items: PickerItem[];
  initialSelectedIds: string[];
  /** 数据是否正在拉取——2026-08-19 新增，调用方在打开弹窗时触发一次 list 请求，这段时间用它
   * 避免列表短暂闪一下"暂无结果"。 */
  loading?: boolean;
  onCancel: () => void;
  onConfirm: (ids: string[]) => void;
}

// 对应高保真 3.2 选择MCP / 3.3 选择技能——两列卡片网格，选中态是整卡蓝色描边，
// 名称右侧直接跟一个 +/✓ 图标。右侧边缘弹出的抽屉，紧贴上下右三边。
// 2026-08-21 用户明确要求去掉"我的/广场"两个 tab，只展示"我的"这一份数据（技能走
// utils/mySkills.ts 的 computeMySkills，跟技能管理页"我的技能"同一套口径；MCP 直接是
// connectorStore.myConnectors）——手动创建插件本来就是给自己的插件挂已经在用的技能/MCP，
// "广场"里那些还没装/没连的条目挂上去也不能真正生效，之前的 tab 是当时"简化实现"遗留的，
// 见该 prop 曾经的 myItems/plazaItems 双份设计。
export function PickerModal({ title, items, initialSelectedIds, loading, onCancel, onConfirm }: PickerModalProps) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<string[]>(initialSelectedIds);
  const [query, setQuery] = useState('');

  const visible = items.filter((item) => {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return item.name.toLowerCase().includes(q) || item.description.toLowerCase().includes(q);
  });

  function toggle(id: string) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  return (
    <div
      className="fixed inset-0 z-50 bg-overlay-cron-drawer"
      data-testid="connector-market-picker-modal"
      onClick={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      {/* 2026-08-19：用户明确要求左侧两个角改直角（原 rounded-l-2xl 去掉）+ 抽屉整体加宽。 */}
      <div className="absolute inset-y-0 right-0 flex w-[760px] flex-col bg-card p-6 shadow-xl" data-testid="connector-market-picker-drawer">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-[16px] font-semibold leading-6 text-text">{title}</h2>
          <button type="button" onClick={onCancel} className="text-text-muted hover:text-text" data-testid="connector-market-picker-close">
            <X size={18} />
          </button>
        </div>

        <div className="relative mb-3 shrink-0">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[color:var(--color-text-placeholder)]" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('connectorMarket.common.search')}
            className="h-8 w-full rounded-lg border border-border bg-bg pl-8 pr-3 text-[12px] leading-[18px] text-text outline-none focus:border-border-hover"
            data-testid="connector-market-picker-search"
          />
        </div>

        <div className="flex-1 overflow-y-auto">
          {/* loading 时不渲染旧数据——调用方在打开弹窗那一刻才发起 list 请求（见 CreatePluginPage
              的按钮 onClick），请求还在路上时 visible 可能还是空数组，不加这个分支会闪一下
              "暂无结果"再跳到真实列表，体验很怪。 */}
          {loading ? (
            <div className="py-10 text-center text-[13px] text-text-muted" data-testid="connector-market-picker-loading">{t('common.loading')}</div>
          ) : (
          <div className="grid grid-cols-2 gap-2" data-testid="connector-market-picker-list">
            {visible.map((item) => {
              const checked = selected.includes(item.id);
              return (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => toggle(item.id)}
                  data-testid="connector-market-picker-item"
                  data-variant={item.id}
                  className={`flex flex-col gap-2 rounded-lg border p-4 text-left transition-colors ${
                    checked ? 'border-[color:var(--color-chat-accent)] bg-accent-subtle' : 'border-border hover:border-border-hover'
                  }`}
                >
                  <div className="flex items-center gap-2.5">
                    {item.render(checked)}
                    <span className="min-w-0 flex-1 truncate text-[13px] font-bold text-text">{item.name}</span>
                    {checked ? <Check size={14} className="shrink-0 text-[color:var(--color-chat-accent)]" /> : <Plus size={14} className="shrink-0 text-text-muted" />}
                  </div>
                  {/* 描述超长截断后靠原生 title 属性做悬浮提示，不引入额外 tooltip 组件依赖。
                      min-h 锁死 2 行的高度（leading-4=16px*2），不管描述是 0/1/2 行，卡片高度都
                      一样，不会有的高有的矮——同款处理见 MarketCard.tsx/MyMarketCard.tsx 的
                      TruncatedText min-h-[44px]（那边 leading-[22px] 更大，这里字号更小换算不同，
                      但道理一样）。 */}
                  <p title={item.description} className="line-clamp-2 min-h-[32px] text-[12px] leading-4 text-text-muted">
                    {item.description}
                  </p>
                </button>
              );
            })}
            {visible.length === 0 && <div className="col-span-full py-10 text-center text-[13px] text-text-muted" data-testid="connector-market-picker-empty">{t('connectorMarket.common.noResult')}</div>}
          </div>
          )}
        </div>

        <div className="mt-4 flex shrink-0 justify-end gap-2 border-t border-border pt-4">
          <button type="button" onClick={onCancel} className="rounded-lg border border-border px-4 py-1.5 text-[13px] text-text hover:border-border-hover" data-testid="connector-market-picker-cancel">
            {t('connectorMarket.common.cancel')}
          </button>
          <button type="button" onClick={() => onConfirm(selected)} className="rounded-lg bg-text px-4 py-1.5 text-[13px] text-text-inverse" data-testid="connector-market-picker-ok">
            {t('connectorMarket.common.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}
