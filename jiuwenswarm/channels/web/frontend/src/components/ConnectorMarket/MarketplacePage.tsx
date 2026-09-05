import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus, ChevronDown, ChevronLeft, ChevronRight } from 'lucide-react';
import { useConnectorStore } from '../../stores/connectorStore';
import { usePluginPackageStore } from '../../stores/pluginPackageStore';
import { localizedText } from '../../types/pluginPackage';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { MarketCard } from './MarketCard';
import { MyMarketCard } from './MyMarketCard';
import { ConnectTokenModal } from './ConnectTokenModal';
import { CliAuthModal } from './CliAuthModal';
import type { ConnectorConnectResponse } from '../../types/connector';
import {
  canOpenMcpDetail,
  cardStateToStatusFilter,
  deriveCardState,
  derivePluginCardState,
} from './mcpState';
import { useClickOutside } from './useClickOutside';
import { usePendingConnectorFlow, PendingConnectorModals } from './usePendingConnectorFlow';
import { CategoryTabs, PageHeader, PageToolbarSearch } from '../ui';
import SimpleSelect from '../CronPanel/SimpleSelect';

export type MarketKind = 'plugin' | 'mcp';
export type TopTab = MarketKind | 'my';
// 状态筛选：'pending' 对应不可用态（插件未安装或MCP未连接/绑定MCP未连接），'available' 对应
// 可用态。2026-08-15 去除全局启用/禁用后不再有第三个"disabled"筛选项，见
// state-model-rectification-v2-remove-global-toggle.md。
type StatusFilter = 'all' | 'pending' | 'available';

interface MarketplacePageProps {
  topTab: TopTab;
  onTopTabChange: (tab: TopTab) => void;
  myKind: MarketKind;
  onMyKindChange: (kind: MarketKind) => void;
  onOpenConnectorDetail: (name: string) => void;
  onOpenPluginDetail: (id: string) => void;
  /** "会话使用"点击——跳新会话并顺带打开这个扩展的会话内启用开关，跟详情页顶部"使用"按钮
   * 是同一条 onUseExtension 通道（见 ConnectorMarket/index.tsx），这里需要知道点的是卡片列表
   * 里的哪一个，所以带上 kind/id。 */
  onUse: (payload: { kind: 'plugin' | 'mcp'; id: string }) => void;
  onCreateManual: () => void;
  onCreateWithSkill: () => void;
  onCreateWithUpload: () => void;
  onRegisterCustomMcp: () => void;
  onOpenApplicationPlugins: () => void;
}

// 2026-08-07：backend-requests.md 需求8 已解决——plugin_packages.list/show 现在真的下发单值
// category 字段（对齐专家与插件装备-前端接口(3).md v1.5）。插件的 category 是 manifest 里的
// 自由字符串，不是后端定死的固定枚举，没法用一份写死的 tab 列表。这里改成前端自己统计：拿当前
// 已加载的 packages，按 category 出现频次从高到低取前 N 个作为独立 tab，其余（含空 category）
// 一律归进"其他"——"其他"这个 tab 只在真的有条目落在它里面时才显示，避免空 tab。
//
// 2026-08-10：MCP 那边原来用的是一份写死的固定枚举（`CONNECTOR_CATEGORIES`），用户指出这不对——
// MCP 接口文档里 `category` 也是后端下发的自由字符串（"预留字段，前端可按此分组但不依赖"，预置
// 多为空串、自定义为 "custom"），跟插件的 category 是同一种"后端不保证固定取值"的字段，不该有
// 两套不一致的处理方式。改成和插件同一套统计逻辑（见下方 `mcpTopCategories`），`CONNECTOR_
// CATEGORIES` 这个写死的枚举已从 types/connector.ts 删除。
const CATEGORY_TOP_N = 6;

// 分页：插件/MCP 数量多了之后单页平铺会很长，这里借鉴 CronPanel/index.tsx 定时任务列表的
// 纯前端本地分页方案——connector.list / plugin_packages.list 目前也是一次性返回全部数据、
// 不支持 offset/limit，跟 cron.job.list 是同样的限制，所以分页逻辑（每页条数可选、页码越界
// 钳制、筛选变化重置到第1页）照抄那一份，不是重新设计。
// PAGE_SIZE_ALL 是"全部"选项的哨兵值——不用 Infinity，是因为 (currentPage-1)*pageSize 在
// currentPage=1 时会算出 0*Infinity=NaN（IEEE754），slice(NaN, NaN) 直接坏掉；用 -1
// 单独判断分支，不参与那套乘法，规避这个坑。
const PAGE_SIZE_ALL = -1;
const PAGE_SIZE_OPTIONS = [30, 50, PAGE_SIZE_ALL];
const DEFAULT_PAGE_SIZE = 30;

function buildPageList(current: number, total: number): (number | 'ellipsis')[] {
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1);
  }
  const pages: (number | 'ellipsis')[] = [1];
  const start = Math.max(2, current - 1);
  const end = Math.min(total - 1, current + 1);
  if (start > 2) pages.push('ellipsis');
  for (let p = start; p <= end; p++) pages.push(p);
  if (end < total - 1) pages.push('ellipsis');
  pages.push(total);
  return pages;
}

interface PaginationBarProps {
  currentPage: number;
  totalPages: number;
  pageSize: number;
  totalCount: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}

function PaginationBar({ currentPage, totalPages, pageSize, totalCount, onPageChange, onPageSizeChange }: PaginationBarProps) {
  const { t } = useTranslation();
  const pageSizeOptions = useMemo(
    () => PAGE_SIZE_OPTIONS.map((n) => ({ value: String(n), label: n === PAGE_SIZE_ALL ? t('connectorMarket.pagination.all') : String(n) })),
    [t],
  );
  const pages = useMemo(() => buildPageList(currentPage, totalPages), [currentPage, totalPages]);
  const showAll = pageSize === PAGE_SIZE_ALL;
  const rangeStart = totalCount === 0 ? 0 : showAll ? 1 : (currentPage - 1) * pageSize + 1;
  const rangeEnd = showAll ? totalCount : Math.min(currentPage * pageSize, totalCount);

  return (
    <div className="mt-4 flex flex-wrap items-center justify-between gap-3 text-[13px] text-text-muted" data-testid="connector-market-pagination">
      <div className="flex items-center gap-2">
        <span>{t('connectorMarket.pagination.pageSize')}</span>
        <SimpleSelect
          value={String(pageSize)}
          onChange={(v) => onPageSizeChange(Number(v))}
          options={pageSizeOptions}
          className="w-20"
          menuPlacement="up"
        />
        <span data-testid="connector-market-pagination-range-info">{t('connectorMarket.pagination.rangeInfo', { start: rangeStart, end: rangeEnd, total: totalCount })}</span>
      </div>
      {totalPages > 1 && (
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={currentPage <= 1}
            onClick={() => onPageChange(currentPage - 1)}
            aria-label={t('connectorMarket.pagination.prev') ?? undefined}
            className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            data-testid="connector-market-pagination-prev"
          >
            <ChevronLeft size={14} />
          </button>
          {pages.map((p, idx) =>
            p === 'ellipsis' ? (
              <span key={`ellipsis-${idx}`} className="px-1.5 text-text-muted">
                …
              </span>
            ) : (
              <button
                key={p}
                type="button"
                onClick={() => onPageChange(p)}
                data-testid="connector-market-pagination-page"
                data-variant={p}
                className={`flex h-7 min-w-7 items-center justify-center rounded-md px-1.5 text-[13px] ${
                  p === currentPage ? 'bg-text font-bold text-text-inverse' : 'text-text hover:bg-bg-hover'
                }`}
              >
                {p}
              </button>
            ),
          )}
          <button
            type="button"
            disabled={currentPage >= totalPages}
            onClick={() => onPageChange(currentPage + 1)}
            aria-label={t('connectorMarket.pagination.next') ?? undefined}
            className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            data-testid="connector-market-pagination-next"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      )}
    </div>
  );
}

export function MarketplacePage({
  topTab,
  onTopTabChange,
  myKind,
  onMyKindChange,
  onOpenConnectorDetail,
  onOpenPluginDetail,
  onUse,
  onCreateManual,
  onCreateWithSkill,
  onCreateWithUpload,
  onRegisterCustomMcp,
  onOpenApplicationPlugins,
}: MarketplacePageProps) {
  const { t, i18n } = useTranslation();
  const [category, setCategory] = useState<string>('all');
  const [pluginCategory, setPluginCategory] = useState<string>('all');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [query, setQuery] = useState('');
  const [createMenuOpen, setCreateMenuOpen] = useState(false);
  const createMenuRef = useRef<HTMLDivElement>(null);
  useClickOutside(createMenuRef, () => setCreateMenuOpen(false));
  const scrollRef = useRef<HTMLDivElement>(null);

  // 分页状态：只有一个网格在展示（四个渲染分支互斥），所以只需要一份 currentPage/pageSize，
  // 不用按 topTab/myKind 分别维护。
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [currentPage, setCurrentPage] = useState(1);

  const [tokenTarget, setTokenTarget] = useState<{ name: string; displayName: string; icon?: string; response: ConnectorConnectResponse } | null>(null);
  const [authTarget, setAuthTarget] = useState<{ name: string; response: ConnectorConnectResponse } | null>(null);

  // connectors：合并视图，按 name 查找/枚举全部已知 MCP 用（mcpCardStates、handleConnectorQuickAdd
  // 的 .find）；builtinConnectors/myConnectors：后端已经按 filter 分好的两份原始列表，"MCP广场"/
  // "我的MCP"两个 tab 分别直接渲染，不再需要前端按 source 二次过滤（见 connectorStore.ts 头注释，
  // 2026-08-17 按 MCP 接口文档 v2 改造，"我的 vs 广场只看 source"的旧结论已作废）。
  const connectors = useConnectorStore((s) => s.connectors);
  const builtinConnectors = useConnectorStore((s) => s.builtinConnectors);
  const myConnectors = useConnectorStore((s) => s.myConnectors);
  const connectAction = useConnectorStore((s) => s.connect);
  const busyMap = useConnectorStore((s) => s.busyMap);
  const connectorIsLoading = useConnectorStore((s) => s.isLoading);

  // packages/localPackages：现在跟 MCP 侧一样由后端 filter 参数分好（builtin/local，见
  // ConnectorMarket/index.tsx 的 load() 和 pluginPackageStore.ts 头注释），"插件广场"用
  // packages（filter='builtin'），"我的插件"用 localPackages（filter='local'）——之前"插件广场"
  // 传 undefined 拿混合列表、靠前端 pkg.source 二次过滤是遗留写法，2026-08-19 对齐 MCP 侧改掉。
  const packages = usePluginPackageStore((s) => s.packages);
  const localPackages = usePluginPackageStore((s) => s.localPackages);
  const installed = usePluginPackageStore((s) => s.installed);
  const pluginConnectionStateMap = usePluginPackageStore((s) => s.connectionStateMap);
  const installPlugin = usePluginPackageStore((s) => s.install);
  const pluginIsLoading = usePluginPackageStore((s) => s.isLoading);
  const pluginInstallPendingMap = usePluginPackageStore((s) => s.installPendingMap);
  const clearPluginInstallPending = usePluginPackageStore((s) => s.clearInstallPending);

  // 派生 MCP 卡片态（见 mcpState.ts）。busy 用 busyMap[name]。这份 map 供下方 MCP 卡片渲染 +
  // statusFilter 共用，避免每个渲染点各自内联判断。
  const mcpCardStates = useMemo(() => {
    const map: Record<string, ReturnType<typeof deriveCardState>> = {};
    for (const c of connectors) {
      map[c.name] = deriveCardState({ connectionState: c.connectionState, busy: busyMap[c.name] });
    }
    return map;
  }, [connectors, busyMap]);

  async function handleConnectorQuickAdd(name: string) {
    const connector = connectors.find((c) => c.name === name);
    // 已连接不重复 connect；connecting 态也跳过（正在连）。
    if (!connector) return;
    const cs = mcpCardStates[name];
    if (cs === 'connected' || cs === 'connecting') return;
    const response = await connectAction(name);
    if (!response) return;
    if (response.credentialsRequired) {
      setTokenTarget({ name, displayName: connector.displayName, icon: connector.icon ?? undefined, response });
    } else if (response.type === 'auth_required') {
      setAuthTarget({ name, response });
    }
    // type === 'connected'：connect() 内部已经 set 了 successMessage（2026-08-21 起提升为 store
    // 全局机制，见 connectorStore.ts），顶层 ConnectorMarket/index.tsx 统一订阅弹 Toast，这里不用
    // 再自己维护一份本地 toast。
    // D 类（loopback OAuth）响应的 sentinel 后端还没定（backend-requests.md 需求6），
    // 这里没有对应分支——遇到未知 type 时安全地什么都不做，等后端定下来再接。
  }

  // §1.6.3 卡片网格快速安装的连接续跑：之前 onQuickInstall/onQuickAdd 直接调
  // pluginPackageStore.install()，若返回 pending_connectors 只会静默记进 installPendingMap，
  // 卡片本身没有任何 UI 订阅它，看起来像"点了没反应"、依赖的 MCP 也就永远不会被自动连上
  // （用户 2026-08-19 反馈）。这里补上跟 PluginDetailPage.tsx/ExtensionPickerPanel.tsx 同款的
  // usePendingConnectorFlow 接线：整页共用一个 flow 实例（卡片网格一次只会有一个"进行中"的
  // quick install，串行连接本身也是单队列），用 ref（不用 state）记录这次连接续跑是为哪个插件
  // id 起的——原因同 PluginDetailPage.tsx 的 pendingUseAfterConnectRef：onAllConnected 里要读到
  // 的是"发起连接续跑那一刻"的 id，用 state 会有同一渲染周期内 setState 未生效的陈旧闭包问题。
  const pendingInstallIdRef = useRef<string | null>(null);
  const pluginInstallFlow = usePendingConnectorFlow(
    () => {
      const id = pendingInstallIdRef.current;
      if (!id) return;
      pendingInstallIdRef.current = null;
      clearPluginInstallPending(id);
      void installPlugin(id);
    },
    () => {
      // 依赖 connector 自动连接失败/被取消：清掉这条 pending 记录。不清的话下面这个 effect 会
      // 立刻再匹配到它、又 start() 一遍——尤其是以前 deps 里带着 pluginInstallFlow.active 时，
      // flow 一结束 active 变 false 就重新触发，直接死循环狂打 mcp.connect（2026-08-31 修）。
      const id = pendingInstallIdRef.current;
      pendingInstallIdRef.current = null;
      if (id) clearPluginInstallPending(id);
    },
  );

  useEffect(() => {
    if (pluginInstallFlow.active) return;
    const entry = Object.entries(pluginInstallPendingMap).find(([, names]) => names && names.length > 0);
    if (!entry) return;
    const [id, names] = entry;
    pendingInstallIdRef.current = id;
    pluginInstallFlow.start(names!);
    // deps 不放 pluginInstallFlow.active：flow 成功/失败收尾都会改写 pluginInstallPendingMap
    // （onAllConnected→clearPluginInstallPending、onAborted→clearPluginInstallPending），靠 map
    // 变化重新驱动就够了；放 active 反而会在 flow 中止时立刻重启，造成死循环。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pluginInstallPendingMap]);

  // 分类 tab 只在"MCP广场"用，统计源直接是 builtinConnectors（filter=builtin 的结果，恒为
  // built_in），不再需要按 source 过滤——后端已经按 filter 分好了。
  const mcpTopCategories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const connector of builtinConnectors) {
      const cat = connector.category.trim();
      if (!cat) continue;
      counts.set(cat, (counts.get(cat) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([key]) => key).slice(0, CATEGORY_TOP_N);
  }, [builtinConnectors]);

  const mcpTopCategorySet = useMemo(() => new Set(mcpTopCategories), [mcpTopCategories]);

  const categoryTabs = useMemo(() => {
    const hasOther = builtinConnectors.some((connector) => !mcpTopCategorySet.has(connector.category.trim()));
    const tabs = [
      { key: 'all', label: t('connectorMarket.categories.all') },
      ...mcpTopCategories.map((key) => ({ key, label: key })),
    ];
    if (hasOther) tabs.push({ key: 'other', label: t('connectorMarket.categories.other') });
    return tabs;
  }, [builtinConnectors, mcpTopCategories, mcpTopCategorySet, t]);

  const pluginTopCategories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const pkg of packages) {
      const cat = pkg.category.trim();
      if (!cat) continue;
      counts.set(cat, (counts.get(cat) ?? 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([key]) => key).slice(0, CATEGORY_TOP_N);
  }, [packages]);

  const pluginTopCategorySet = useMemo(() => new Set(pluginTopCategories), [pluginTopCategories]);

  const pluginCategoryTabs = useMemo(() => {
    const hasOther = packages.some((pkg) => !pluginTopCategorySet.has(pkg.category.trim()));
    const tabs = [
      { key: 'all', label: t('connectorMarket.categories.all') },
      ...pluginTopCategories.map((key) => ({ key, label: key })),
    ];
    if (hasOther) tabs.push({ key: 'other', label: t('connectorMarket.categories.other') });
    return tabs;
  }, [packages, pluginTopCategories, pluginTopCategorySet, t]);

  const filteredConnectors = useMemo(() => {
    if (topTab === 'my' ? myKind !== 'mcp' : topTab !== 'mcp') return [];
    const q = query.trim().toLowerCase();
    // "我的MCP"vs"MCP广场"现在由后端 filter 参数分好（builtin/local，见 connectorStore.ts
    // 头注释），前端不用再按 source 二次过滤——2026-08-17 用户已确认按 MCP 接口文档 v2 实现，
    // 2026-08-10"我的 vs 广场只看 source、与连接状态无关"的旧结论作废：一个已连接的预置 MCP
    // 现在会同时出现在两个 tab 里。分类筛选（category）只在"广场"视角适用，"我的"没有分类 tab。
    const base = topTab === 'my' ? myConnectors : builtinConnectors;
    return base.filter((connector) => {
      if (topTab !== 'my' && category !== 'all') {
        const cat = connector.category.trim();
        if (category === 'other' ? mcpTopCategorySet.has(cat) : cat !== category) return false;
      }
      if (statusFilter !== 'all') {
        // 卡片态 → 筛选态：connected→available、idle/connecting/error→pending（连接中/连失败
        // 都尚未可用）。见 mcpState.ts cardStateToStatusFilter。
        const cs = mcpCardStates[connector.name];
        if (cardStateToStatusFilter(cs) !== statusFilter) return false;
      }
      if (q && !connector.displayName.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [builtinConnectors, myConnectors, topTab, myKind, category, mcpTopCategorySet, statusFilter, query, mcpCardStates]);

  const filteredPlugins = useMemo(() => {
    if (topTab === 'my' ? myKind !== 'plugin' : topTab !== 'plugin') return [];
    const q = query.trim().toLowerCase();
    // "我的插件"vs"插件广场"现在也由后端 filter 参数分好（builtin/local，见上面 packages/
    // localPackages 的注释），跟 filteredConnectors 同款，不用再按 pkg.source 二次过滤。分类
    // 筛选（category）只在"广场"视角适用，"我的"没有分类 tab。
    const base = topTab === 'my' ? localPackages : packages;
    return base.filter((pkg) => {
      if (topTab !== 'my' && pluginCategory !== 'all') {
        const cat = pkg.category.trim();
        if (pluginCategory === 'other' ? pluginTopCategorySet.has(cat) : cat !== pluginCategory) return false;
      }
      if (statusFilter !== 'all') {
        // 插件侧用 derivePluginCardState 派生（installed&&connected→connected，否则→idle），
        // 再用 cardStateToStatusFilter 归到筛选态，和 MCP 侧统一。插件恒不会进 connecting/error。
        const cs = derivePluginCardState(!!installed[pkg.id], (pluginConnectionStateMap[pkg.id] ?? 'disconnected') === 'connected');
        if (cardStateToStatusFilter(cs) !== statusFilter) return false;
      }
      const title = localizedText(pkg.displayName, i18n.language);
      if (q && !title.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [packages, localPackages, topTab, myKind, pluginCategory, pluginTopCategorySet, statusFilter, installed, pluginConnectionStateMap, query, i18n.language]);

  // 当前实际展示的那份列表（四个渲染分支互斥，取其一即可），分页条/总数/isEmpty 都基于它算。
  const activeKindForEmpty: MarketKind = topTab === 'my' ? myKind : topTab === 'mcp' ? 'mcp' : 'plugin';
  const activeList = topTab === 'my' ? (myKind === 'mcp' ? filteredConnectors : filteredPlugins) : topTab === 'mcp' ? filteredConnectors : filteredPlugins;
  const isEmpty = activeList.length === 0;
  // 列表为空时要分清"数据还没回来"和"回来了但真的没有"——首次/切 tab 的非静默 loadList() 会把
  // 对应 store 的 isLoading 短暂置 true，10s 静默轮询不影响它（见两个 store 的 loadList 实现），
  // 用它区分空态文案该显示"加载中"还是"没有找到匹配的结果"。
  const activeIsLoading = activeKindForEmpty === 'mcp' ? connectorIsLoading : pluginIsLoading;
  // 空状态文案分两种：有搜索词/状态筛选/分类筛选时列表为空 = "没有找到匹配的结果"；什么都没筛
  // 却为空才是真的 "这里还没有内容"（"我的"下 = 还没创建/添加过，与安装/连接状态无关；"广场"
  // 下 = 后端没返回可用项）。加载态分支同理按 topTab/myKind 区分，两者逻辑保持一致。
  const activeCategoryForEmpty = activeKindForEmpty === 'mcp' ? category : pluginCategory;
  const hasEmptyNarrowing =
    query.trim() !== '' ||
    statusFilter !== 'all' ||
    (topTab !== 'my' && activeCategoryForEmpty !== 'all');

  // 切换 tab/子筛选/分类/状态筛选/搜索词都会让 activeList 变成一份新列表，统一重置回第1页，
  // 避免停留在一个对新列表来说已经越界的页码上看到空白（同款处理见 CronPanel/index.tsx 的
  // "搜索内容变化时重置回第 1 页"）。
  useEffect(() => {
    setCurrentPage(1);
  }, [topTab, myKind, category, pluginCategory, statusFilter, query]);

  const totalPages = pageSize === PAGE_SIZE_ALL ? 1 : Math.max(1, Math.ceil(activeList.length / pageSize));

  // 列表变短（比如删除/卸载后仍留在原页）导致当前页码越界时钳制回合法范围
  useEffect(() => {
    setCurrentPage((p) => (p > totalPages ? totalPages : p));
  }, [totalPages]);

  const pageStart = pageSize === PAGE_SIZE_ALL ? 0 : (currentPage - 1) * pageSize;
  const pageEnd = pageSize === PAGE_SIZE_ALL ? undefined : pageStart + pageSize;
  const paginatedConnectors = filteredConnectors.slice(pageStart, pageEnd);
  const paginatedPlugins = filteredPlugins.slice(pageStart, pageEnd);

  function goToPage(page: number) {
    setCurrentPage(page);
    scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function changePageSize(size: number) {
    setPageSize(size);
    setCurrentPage(1);
  }

  return (
    <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden" data-testid="connector-market-marketplace">
      <PageHeader title={t('connectorMarket.title')} subtitle={t('connectorMarket.subtitle')} />

      <div className="page-toolbar" data-testid="page-toolbar">
        <div className="chat-picker-panel__tabs">
          {(['plugin', 'mcp', 'my'] as const).map((tab) => {
            const active = topTab === tab;
            return (
              <button
                key={tab}
                type="button"
                onClick={() => onTopTabChange(tab)}
                aria-pressed={active}
                data-testid="connector-market-tab"
                data-variant={tab}
                className={active ? 'is-active' : ''}
              >
                {t(tab === 'my' ? 'connectorMarket.tabs.my' : `connectorMarket.tabs.${tab}Market`)}
              </button>
            );
          })}
          <button
            type="button"
            onClick={onOpenApplicationPlugins}
            className="relative pb-2 text-[14px] font-normal leading-[22px] text-text"
          >
            {t('connectorMarket.tabs.applicationPlugins')}
          </button>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1">
            {(['all', 'available', 'pending'] as const).map((key) => {
              const active = statusFilter === key;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setStatusFilter(key)}
                  aria-pressed={active}
                  data-testid="connector-market-status-filter"
                  data-variant={key}
                  className={`h-8 shrink-0 whitespace-nowrap px-3 text-[13px] font-medium ${
                    active ? 'rounded-[8px] bg-secondary text-text' : 'text-text-muted hover:text-text'
                  }`}
                >
                  {t(`connectorMarket.statusFilter.${key}`)}
                </button>
              );
            })}
          </div>
          <PageToolbarSearch
            wrapperTestId="connector-market-search"
            inputTestId="connector-market-search-input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t(`connectorMarket.search.${topTab}`)}
          />

          {topTab === 'my' && (
            <div className="relative" ref={createMenuRef}>
              <button
                type="button"
                onClick={() => setCreateMenuOpen((v) => !v)}
                data-testid="connector-market-create-menu"
                className="flex h-8 shrink-0 items-center gap-1 rounded-lg bg-text px-3 text-[12px] text-text-inverse"
              >
                <Plus size={13} />
                {t('connectorMarket.create.menuLabel')}
                <ChevronDown size={13} />
              </button>
              {createMenuOpen && (
                <div className="absolute right-0 top-9 z-20 w-40 rounded-lg border border-border bg-card py-1 shadow-lg" data-testid="connector-market-create-menu-popover">
                  {myKind === 'plugin' ? (
                    <>
                      <MenuItem label={t('connectorMarket.create.manual')} onClick={() => { setCreateMenuOpen(false); onCreateManual(); }} />
                      <MenuItem label={t('connectorMarket.create.withSkill')} onClick={() => { setCreateMenuOpen(false); onCreateWithSkill(); }} />
                      <MenuItem label={t('connectorMarket.create.withUpload')} onClick={() => { setCreateMenuOpen(false); onCreateWithUpload(); }} />
                    </>
                  ) : (
                    <MenuItem label={t('connectorMarket.create.registerMcp')} onClick={() => { setCreateMenuOpen(false); onRegisterCustomMcp(); }} />
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 2026-08-29 MCP 广场目前都不带 tag（category 多为空），分类 tab 行先隐藏——去掉 false 即恢复 */}
      {false && topTab === 'mcp' && categoryTabs.length > 1 && (
        <div className="mb-5">
          <CategoryTabs
            items={categoryTabs.map((tab) => ({ value: tab.key, label: tab.label }))}
            value={category}
            onChange={setCategory}
          />
        </div>
      )}

      {topTab === 'plugin' && pluginCategoryTabs.length > 1 && (
        <div className="mb-5">
          <CategoryTabs
            items={pluginCategoryTabs.map((tab) => ({ value: tab.key, label: tab.label }))}
            value={pluginCategory}
            onChange={setPluginCategory}
          />
        </div>
      )}

      {topTab === 'my' && (
        <div className="mb-5">
          <CategoryTabs
            items={(['plugin', 'mcp'] as const).map((kind) => ({ value: kind, label: t(`connectorMarket.tabs.${kind}`) }))}
            value={myKind}
            onChange={onMyKindChange}
          />
        </div>
      )}

      <div ref={scrollRef} className="card-grid-auto min-h-0 overflow-y-auto" data-testid="connector-market-card-list">
        {topTab === 'my' ? (
          myKind === 'mcp' ? (
            paginatedConnectors.map((connector) => {
              const cs = mcpCardStates[connector.name];
              // 可达性由安装状态和连接状态共同决定；连接中的卡片不能打开详情页。
              const canOpenDetail = canOpenMcpDetail(connector.source, cs);
              return (
                <MyMarketCard
                  key={connector.name}
                  title={connector.displayName}
                  description={connector.description ?? ''}
                  avatar={getSkillAvatar(connector.displayName)}
                  iconUrl={connector.icon ?? undefined}
                  state={cs}
                  busyKind={busyMap[connector.name]}
                  onOpenDetail={() => onOpenConnectorDetail(connector.name)}
                  canOpenDetail={canOpenDetail}
                  onUse={() => onUse({ kind: 'mcp', id: connector.name })}
                  onQuickInstall={cs === 'connected' ? undefined : () => handleConnectorQuickAdd(connector.name)}
                />
              );
            })
          ) : (
            paginatedPlugins.map((pkg) => {
              const pluginInstalled = !!installed[pkg.id];
              const pluginConnected = (pluginConnectionStateMap[pkg.id] ?? 'disconnected') === 'connected';
              // plugin_packages.list 不下发 avatar（只有 show 详情才有），卡片层级没有真实图标
              // 可用，不传 iconUrl，EntityAvatar 会直接走生成的首字符色块。
              return (
                <MyMarketCard
                  key={pkg.id}
                  title={localizedText(pkg.displayName, i18n.language)}
                  description={localizedText(pkg.displayDescription, i18n.language)}
                  avatar={getSkillAvatar(localizedText(pkg.displayName, i18n.language))}
                  state={derivePluginCardState(pluginInstalled, pluginConnected)}
                  onOpenDetail={() => onOpenPluginDetail(pkg.id)}
                  onUse={() => onUse({ kind: 'plugin', id: pkg.id })}
                  // 触发条件要跟着卡片态走，不能只看 installed——"已安装但 MCP 未连接"这个
                  // 组合下 derivePluginCardState 也会算出 'idle'（见该函数注释），"+"按钮同样
                  // 要能点（点击后重新触发安装，对应按钮矩阵"已安装+未连接：+号"这一格）。
                  onQuickInstall={derivePluginCardState(pluginInstalled, pluginConnected) === 'connected' ? undefined : () => installPlugin(pkg.id)}
                />
              );
            })
          )
        ) : topTab === 'mcp' ? (
          paginatedConnectors.map((connector) => {
            const cs = mcpCardStates[connector.name];
            const canOpenDetail = canOpenMcpDetail(connector.source, cs);
            return (
              <MarketCard
                key={connector.name}
                title={connector.displayName}
                description={connector.description ?? ''}
                avatar={getSkillAvatar(connector.displayName)}
                iconUrl={connector.icon ?? undefined}
                state={cs}
                busyKind={busyMap[connector.name]}
                canOpenDetail={canOpenDetail}
                onOpenDetail={() => onOpenConnectorDetail(connector.name)}
                onQuickAdd={() => handleConnectorQuickAdd(connector.name)}
                onUse={() => onUse({ kind: 'mcp', id: connector.name })}
              />
            );
          })
        ) : (
          paginatedPlugins.map((pkg) => {
            const pluginInstalled = !!installed[pkg.id];
            const pluginConnected = (pluginConnectionStateMap[pkg.id] ?? 'disconnected') === 'connected';
            return (
              <MarketCard
                key={pkg.id}
                title={localizedText(pkg.displayName, i18n.language)}
                description={localizedText(pkg.displayDescription, i18n.language)}
                avatar={getSkillAvatar(localizedText(pkg.displayName, i18n.language))}
                state={derivePluginCardState(pluginInstalled, pluginConnected)}
                canOpenDetail
                onOpenDetail={() => onOpenPluginDetail(pkg.id)}
                onUse={() => onUse({ kind: 'plugin', id: pkg.id })}
                // 同款修正：不能只看 installed，见上面"我的插件"卡片同名 prop 的注释。
                onQuickAdd={() => {
                  if (derivePluginCardState(pluginInstalled, pluginConnected) !== 'connected') installPlugin(pkg.id);
                }}
              />
            );
          })
        )}
        {isEmpty && (
          <div className="col-span-full flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-border py-16 text-[13px] text-text-muted" data-testid="connector-market-empty">
            {activeIsLoading
              ? t(
                  topTab === 'my'
                    ? myKind === 'mcp'
                      ? 'connectorMarket.empty.loadingMyMcp'
                      : 'connectorMarket.empty.loadingMyPlugin'
                    : topTab === 'mcp'
                      ? 'connectorMarket.empty.loadingMcp'
                      : 'connectorMarket.empty.loadingPlugin',
                )
              : hasEmptyNarrowing
                ? t('connectorMarket.empty.searchNoResult')
                : t(
                    topTab === 'my'
                      ? myKind === 'mcp'
                        ? 'connectorMarket.empty.myMcp'
                        : 'connectorMarket.empty.myPlugin'
                      : topTab === 'mcp'
                        ? 'connectorMarket.empty.mcp'
                        : 'connectorMarket.empty.plugin',
                  )}
          </div>
        )}
      </div>

      {!isEmpty && (
        <PaginationBar
          currentPage={currentPage}
          totalPages={totalPages}
          pageSize={pageSize}
          totalCount={activeList.length}
          onPageChange={goToPage}
          onPageSizeChange={changePageSize}
        />
      )}

      {tokenTarget && (
        <ConnectTokenModal
          name={tokenTarget.name}
          displayName={tokenTarget.displayName}
          iconUrl={tokenTarget.icon}
          response={tokenTarget.response}
          onCancel={() => setTokenTarget(null)}
          onConnected={() => setTokenTarget(null)}
        />
      )}

      {authTarget && (
        <CliAuthModal
          name={authTarget.name}
          initial={authTarget.response}
          onCancel={() => setAuthTarget(null)}
          onConnected={() => setAuthTarget(null)}
        />
      )}

      <PendingConnectorModals flow={pluginInstallFlow} />
    </div>
  );
}

function MenuItem({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="block w-full px-3 py-1.5 text-left text-[13px] text-text hover:bg-bg-muted">
      {label}
    </button>
  );
}
