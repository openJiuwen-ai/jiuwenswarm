import { create } from 'zustand';
import { extractRpcErrorMessage } from '../features/agentManagement/upload';
import { PluginInstallPendingError, pluginPackagesApi } from '../services/pluginPackagesApi';
import type { PluginConnectionState, PluginPackageDetail, PluginPackageSummary } from '../types/pluginPackage';

// 2026-08-07：installed/enabledMap 已经改成以后端 list/show 真实下发的字段为准（对齐
// 专家与插件装备-前端接口(3).md v1.5）——loadList() 每次都会用 pkg.installed/pkg.enabled
// 覆盖本地状态。localStorage 兜底从"唯一数据源"降级成"乐观更新缓存"：install/toggle 调用
// 成功之间的空档，靠本地先翻转一次状态给用户即时反馈，下次 loadList() 会被后端真实值覆盖掉，
// 不会永久跑偏。
//
// create 目前后端仍缺，保持"如实报错"策略，不做本地模拟成功——理由见
// state-model-rectification.md §5：这个操作改变"实体存不存在"，假装成功会导致刷新后
// 诡异地又出现。install/toggle/uninstall 后端已有真实接口，正常走接口结果即可。
//
// deletePackage 没有对应的 plugin_packages.delete（backend-requests.md 需求20，通篇文档
// 没有这个方法）——产品结论是"我的插件"的删除直接复用 uninstall，见该 action 自己的注释。
//
// 2026-08-10 去掉 myPluginIds 这份 localStorage"曾经安装过"名单：用户明确纠正过"我的插件"
// 归属模型——只看 source 字段（local 永远在"我的"，built-in 永远在广场），从条目"出生"起就固定，
// 跟 installed/enabled 状态完全无关。旧版靠 myPluginIds 模拟"卸载后仍留在我的列表"是错的产品
// 理解（同款错误也出现在 connectorStore.ts 的 myConnectorNames，一并删除，见该文件头注释）。
// 归属判断现在直接在 MarketplacePage.tsx 按 pkg.source==='local' 过滤，这个 store 不再需要
// 维护任何"曾经xx过"的历史记录。
//
// 2026-08-15 去除全局启用/禁用（状态C）：enabledMap/toggle 整个删除，插件不再有这个维度，见
// state-model-rectification-v2-remove-global-toggle.md。
//
// 2026-08-17 对齐 专家与插件装备-前端接口_v2.md §1.6：
// - connectedMap（占位布尔）→ connectionStateMap（真实 connection_state 三态），来源
//   list/show 真实字段，见 types/pluginPackage.ts 头注释。
// - 新增 installPendingMap：install 失败且带 pending_connectors（§1.6.3）时，把待连名单存
//   在这里，不再当成普通 error 丢给用户看一句话——组件层（PluginDetailPage.tsx /
//   ExtensionPickerPanel.tsx）据此驱动 usePendingConnectorFlow 走连接续跑，连完再调一次
//   install（幂等）。§1.6.4"已装重连"（installed=true 但 connectionState≠connected）走的是
//   另一条路：组件直接 show() 拿 detail.pendingConnectors 连，不经过这个 map、也不再调 install
//   （文档强调了两次不能调）。
// - 当前后端实例这批接口还没实现（unknown method，预期内），install 的两阶段流程暂时无法端到
//   端验证，按文档实现，等后端 ready 后联调。
//
// 2026-08-18 新增 localPackages（照抄 connectorStore.ts 的 builtinConnectors/myConnectors 拆分）：
// 之前 packages 是唯一一份列表，谁调 loadList() 传什么 filter 都写进同一个字段——ConnectorMarket/
// index.tsx 的市场页轮询固定传 undefined（全量），如果 ExtensionPickerPanel.tsx（会话内"扩展"
// 面板，只需要 local）也共用 packages，两边互相覆盖：市场页每 10s 静默轮询一次会把面板刚拉到的
// local-only 结果整个替换成全量，反之亦然。拆开后 installed/connectionStateMap 这两个按 id 查的
// map 本来就该是两份列表的并集，因此改成合并写入（保留上一次其他来源写入的 id），不再整份覆盖
// 丢失另一侧的数据。
//
// 2026-08-19 更新：packages 的用途从"市场页全量场景"收窄成跟 MCP 侧 builtinConnectors 对称的
// "插件广场"专用桶（filter='builtin'）——之前"插件广场"tab 传 undefined 拿全量再靠前端
// pkg.source 二次过滤，用户反馈"为什么不像 MCP 一样直接用后端 filter 分开"，`loadList()` 现在
// 按 seqKey 分桶的规则不变（filter==='local' 才写 localPackages，其余含 undefined/'builtin' 都写
// packages），但调用方（ConnectorMarket/index.tsx）不再传 undefined，"插件广场"/"我的插件"两个
// tab 各自显式传 'builtin'/'local'，语义上和 MCP 的 builtinConnectors/myConnectors 完全对称。

const LOCAL_STORAGE_KEY = 'jiuwenswarm_plugin_package_local_state';

interface PersistedLocalState {
  installed: Record<string, boolean>;
}

function loadPersistedLocalState(): PersistedLocalState {
  try {
    const raw = localStorage.getItem(LOCAL_STORAGE_KEY);
    if (!raw) return { installed: {} };
    const parsed = JSON.parse(raw);
    return {
      installed: typeof parsed?.installed === 'object' && parsed.installed !== null ? parsed.installed : {},
    };
  } catch {
    return { installed: {} };
  }
}

function persistLocalState(state: PersistedLocalState) {
  queueMicrotask(() => {
    try {
      localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(state));
    } catch {
      /* ignore：配额满/隐私模式 */
    }
  });
}

interface PluginPackageState {
  packages: PluginPackageSummary[];
  /** filter='local' 的独立结果，供 ExtensionPickerPanel.tsx 用（见文件头 2026-08-18 注释）。 */
  localPackages: PluginPackageSummary[];
  detailCache: Record<string, PluginPackageDetail>;
  installed: Record<string, boolean>;
  // 插件依赖的 connector 是否就绪，未加载视为 'disconnected'（见文件头注释——数据缺失时宁可
  // 让 UI 走"需要连接"分支）。
  connectionStateMap: Record<string, PluginConnectionState>;
  // install 失败且带 pending_connectors 时记在这里，key 不存在/值为 undefined 表示没有待处理
  // 的续连（见文件头注释 §1.6.3）。
  installPendingMap: Record<string, string[] | undefined>;
  isLoading: boolean;
  error: string | null;
  /** v2 §3.6：卸载成功但包声明了 connector 依赖时，后端带的引导文案（原文透传，不是 i18n key）。 */
  noticeMessage: string | null;
  /** install 真正落盘成功（非半途 pending_connectors）时的提示 i18n key——照抄 connectorStore.ts
   * 的 successMessage/successKey 机制，2026-08-21 用户要求卡片网格+详情页两个入口都要有安装
   * 成功提示，统一在 install() 里 set 一次即可覆盖，不用调用方各自维护。 */
  successMessage: string | null;
  busyId: string | null;

  loadList: (filter?: 'builtin+hub' | 'mine', options?: { silent?: boolean }) => Promise<void>;
  // 返回是否成功——PluginDetailPage.tsx 卸载后要重新 show() 探测这个插件还在不在（新方案
  // "我的插件"卸载后的收尾逻辑：还能读到就留在详情页，读不到才退出到列表页），需要知道结果。
  loadDetail: (id: string) => Promise<boolean>;
  /** 跟 loadDetail 几乎一样，唯一区别是失败时不 set 全局 error——2026-08-21 用户反馈根因确认：
   * 卸载插件（uninstall_plugin_package）后端会把整个包目录删掉（不是只翻 installed 标记），
   * 卸载后探测"这个包还在不在"时 show() 404 是预期中的正常结果（走 onDeleted 退出到列表页），
   * 不该弹一条吓人的红色错误提示——真正的卸载结果反馈已经由 uninstall()/deletePackage() 自己的
   * successMessage/error 负责，这个探测只是导航判断用。 */
  probeExists: (id: string) => Promise<boolean>;
  create: (params: {
    id: string;
    name: string;
    description: string;
    skills: string[];
    mcps: string[];
  }) => Promise<boolean>;
  importLocal: (
    params: { path: string },
  ) => Promise<{ ok: true } | { ok: false; error: string }>;
  install: (id: string) => Promise<void>;
  uninstall: (id: string) => Promise<void>;
  deletePackage: (id: string) => Promise<boolean>;
  /** 组件层的连接续跑走完/用户取消后调用，清掉 installPendingMap[id]，不留残留状态。 */
  clearInstallPending: (id: string) => void;
  clearError: () => void;
  clearNotice: () => void;
  clearSuccess: () => void;
}

const persisted = loadPersistedLocalState();
const persistedInstalled = { ...persisted.installed };
const initialConnectionStates: Record<string, PluginConnectionState> = {};

// 2026-08-18：同 connectorStore.ts 的 listRequestSeq——ExtensionPickerPanel 每次打开都无条件
// 重新 loadList，同一个 filter 桶（'local' 或 packages 那半，undefined/'builtin' 共用）可能有
// 多次调用同时在途，没有序号保护时旧请求姗姗来迟会把新请求已经写入的数据冲掉。key 按实际会
// 写入的 state 字段分桶（跟下面 `filter === 'local' ? localPackages : packages` 的判断保持一致），
// 不是按 filter 原始取值分——undefined 和 'builtin' 本来就写同一个 packages 字段，理应共用同一个
// 序号桶，否则这两者各自的序号互不感知，挡不住彼此的旧请求覆盖。
const listRequestSeq: Record<'mine' | 'packages', number> = { mine: 0, packages: 0 };

// 照抄 connectorStore.ts 的 scheduleQuickRefresh：install() 成功后本地乐观 patch
// connectionStateMap 只是让 UI 立刻可信，仍需要一次真实的 loadList('local') 兜底校准，防止
// 乐观值和后端真实状态长期不同步（比如乐观 patch 后紧接着这个插件在别处又被断开）。防抖到 1.5s
// 后只跑一次，避免连续装好几个插件时打出一串重复请求。
let quickRefreshTimer: number | null = null;
const QUICK_REFRESH_DELAY_MS = 1500;

function scheduleQuickRefresh(): void {
  if (quickRefreshTimer !== null) window.clearTimeout(quickRefreshTimer);
  quickRefreshTimer = window.setTimeout(() => {
    quickRefreshTimer = null;
    void usePluginPackageStore.getState().loadList('mine', { silent: true });
  }, QUICK_REFRESH_DELAY_MS);
}

export const usePluginPackageStore = create<PluginPackageState>((set) => ({
  packages: [],
  localPackages: [],
  detailCache: {},
  installed: persistedInstalled,
  connectionStateMap: initialConnectionStates,
  installPendingMap: {},
  isLoading: false,
  error: null,
  noticeMessage: null,
  successMessage: null,
  busyId: null,

  // silent=true 仅用于安装/卸载后的快速校准，不改变页面的加载状态。
  loadList: async (filter, options) => {
    const silent = options?.silent ?? false;
    const seqKey = filter === 'mine' ? 'mine' : 'packages';
    const mySeq = ++listRequestSeq[seqKey];
    if (!silent) set({ isLoading: true, error: null });
    try {
      const freshPackages = await pluginPackagesApi.list(filter);
      if (listRequestSeq[seqKey] !== mySeq) return; // 已有更新的同桶调用发起过，这次结果作废
      const packages = freshPackages;
      set((state) => {
        // installed/connectionStateMap 是 packages+localPackages 两份列表的并集，合并写入而不是
        // 整份覆盖——否则市场页（filter=undefined）和会话面板（filter='local'）交替 loadList 时，
        // 后写入的一方会把对方那批 id 的记录冲掉（见文件头 2026-08-18 注释）。
        const nextInstalled = { ...state.installed };
        const nextConnectionStateMap = { ...state.connectionStateMap };
        for (const pkg of packages) {
          nextInstalled[pkg.id] = pkg.installed;
          nextConnectionStateMap[pkg.id] = pkg.connectionState;
        }
        persistLocalState({ installed: nextInstalled });
        return {
          ...(filter === 'mine' ? { localPackages: packages } : { packages }),
          isLoading: false,
          installed: nextInstalled,
          connectionStateMap: nextConnectionStateMap,
        };
      });
    } catch (error) {
      if (listRequestSeq[seqKey] !== mySeq) return;
      if (silent) return;
      set({
        ...(filter === 'mine' ? { localPackages: [] } : { packages: [] }),
        isLoading: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  },

  loadDetail: async (id: string) => {
    try {
      const detail = await pluginPackagesApi.show(id);
      set((state) => ({
        detailCache: { ...state.detailCache, [id]: detail },
        connectionStateMap: { ...state.connectionStateMap, [id]: detail.connectionState },
      }));
      return true;
    } catch (error) {
      set({ error: error instanceof Error ? error.message : String(error) });
      return false;
    }
  },

  probeExists: async (id: string) => {
    try {
      const detail = await pluginPackagesApi.show(id);
      set((state) => ({
        detailCache: { ...state.detailCache, [id]: detail },
        connectionStateMap: { ...state.connectionStateMap, [id]: detail.connectionState },
      }));
      return true;
    } catch {
      return false;
    }
  },

  // 和 install 不同：create 产出的是一个全新实体，后续 show() 还要能读到它，前端没法安全地
  // "假装成功"——后端没实现这个接口时（backend-requests.md 需求2），这里如实失败，让调用方给
  // 用户看错误提示，而不是伪造一条本地数据后刷新就消失。
  //
  // 2026-08-21：create_plugin_package 落盘时固定 installed=False，手动创建的插件永远是"已创建
  // 但未安装"。这里一度改成创建成功后自动串联调用 install(id)（照抄 MCP 侧 registerCustom 自动
  // connect 的模式），但用户跟同事对齐产品方案后明确要求撤回——创建这一步只管创建，不自动安装，
  // 用户需要自己再点一次安装。
  create: async (params) => {
    try {
      await pluginPackagesApi.create(params);
      // 新建的包必然是 source==='local'，刷新 localPackages（'我的插件'桶）即可；2026-08-19
      // loadList() 的 filter 语义改成跟 MCP 侧对齐后，裸调 loadList()（等价于 filter='builtin'）
      // 会用只含 builtin 的结果覆盖 packages，刷不出刚创建的这条、还会短暂污染"插件广场"数据。
      await usePluginPackageStore.getState().loadList('mine');
      return true;
    } catch (error) {
      set({ error: error instanceof Error ? error.message : String(error) });
      return false;
    }
  },

  // 上传文件创建插件（plugin_packages.import_local，见 pluginPackagesApi.ts 头注释）：跟 create
  // 一样是产出全新实体，没法安全地本地模拟成功，如实报错。成功后刷新 localPackages（'我的插件'
  // 桶，导入的包必然是 source==='local'）。
  importLocal: async (params) => {
    try {
      await pluginPackagesApi.importLocal(params);
      await usePluginPackageStore.getState().loadList('mine');
      return { ok: true };
    } catch (error) {
      return {
        ok: false,
        error: extractRpcErrorMessage(error, 'plugin package import failed'),
      };
    }
  },

  // install 成功后后端会把 installed 置 true（v2 §1.4 状态机），本地乐观更新同步，等下次
  // loadList() 用真实值覆盖。失败区分两种（§1.6.3）：带 pending_connectors 的半途失败不算
  // "出错"给用户看一句 error 文案，而是记进 installPendingMap，交给组件层驱动连接续跑后幂等
  // 重试本方法；不带 pending_connectors 的才是普通硬失败，走原来的 error 展示路径。
  //
  // 2026-08-21 用户反馈根因排查确认：install() 之前只 patch installed，从不碰
  // connectionStateMap——装完那一刻卡片/详情页读到的还是安装前缓存的旧值（对一个之前没装过的
  // 插件，这个旧值基本就是后端在广场列表里给的初始态，大概率是 'disconnected'），只有等下一次
  // loadList/loadDetail 真正打后端才会被覆盖成真实值，期间会有一段"明明装好了却显示未连接"的
  // 误导窗口（用户实测：离开面板重新进详情页触发一次新 loadDetail 后确认会自动变成已连接，
  // 证实是纯前端 timing 问题，不是后端语义有问题）。
  // §1.6.3 状态机含义：install() 不抛异常就代表这次请求确认所有依赖 connector 都已就绪——真正的
  // 半途失败会抛 PluginInstallPendingError，走不到这个分支——所以这里可以照抄 MCP connect() 的
  // 做法，成功就乐观 patch connectionStateMap 为 'connected'，不用干等下一次拉取；随后再
  // scheduleQuickRefresh 一次真实 loadList('local') 兜底校准（同 connectorStore.ts 的
  // scheduleQuickRefresh，避免乐观值和后端真实状态长期不同步）。
  install: async (id: string) => {
    set((state) => ({
      busyId: id,
      error: null,
      successMessage: null,
      installPendingMap: { ...state.installPendingMap, [id]: undefined },
    }));
    try {
      await pluginPackagesApi.install(id);
      set((state) => {
        const nextInstalled = { ...state.installed, [id]: true };
        persistLocalState({ installed: nextInstalled });
        return {
          installed: nextInstalled,
          busyId: null,
          successMessage: successKey.pluginInstalled,
          connectionStateMap: { ...state.connectionStateMap, [id]: 'connected' },
        };
      });
      scheduleQuickRefresh();
    } catch (error) {
      if (error instanceof PluginInstallPendingError) {
        set((state) => ({
          busyId: null,
          installPendingMap: { ...state.installPendingMap, [id]: error.pendingConnectors },
        }));
        return;
      }
      set({ busyId: null, error: error instanceof Error ? error.message : String(error) });
    }
  },

  // 2026-08-21 用户反馈根因确认：这条注释原来写的是"卸载只让 installed 变 false，不影响是否
  // 还留在'我的'里"——实测发现是错的，后端 uninstall_plugin_package 会把整个包目录 rmtree 掉
  // 并从 marketplace 名单里移除条目（不是只翻 installed 标记），卸载后这个包在 list/show 里
  // 就是真的查不到了。PluginDetailPage.tsx 的"我的"视角卸载收尾（探测还在不在，不在就退出到
  // 列表页）原来就是按这个真实行为写的，只是探测用的 loadDetail 会在探测失败时顺带弹一条红色
  // 错误 Toast，把"预期内的 404"和"真错误"混在一起了，已经改成用不弹 error 的 probeExists。
  //
  // 之前这里从来没有默认的"卸载成功"提示——只有后端返回 notice（该插件依赖的 connector 仍
  // 保持连接）时才会弹一条绿色 Toast，没有 notice 就什么反馈都没有，用户看不出卸载到底成没成功。
  // 现在补上：没有 notice 时 set 一个默认的"卸载成功" successMessage；有 notice 时沿用原来的
  // noticeMessage（那条本来就是"卸载成功但有件事要提醒"，同一个绿色 Toast 语义已经包含成功
  // 信息），两者不会同时 set，不会抢同一个 Toast 展示位。
  //
  // 2026-08-15：新方案里插件的"卸载"按钮在广场/我的两处都统一叫"卸载"（不再单独区分"删除"），
  // 都是这一个 action——见 deletePackage 的注释，两者本来就是同一个后端调用。
  uninstall: async (id: string) => {
    set({ busyId: id, error: null, successMessage: null });
    try {
      const { notice } = await pluginPackagesApi.uninstall(id);
      set((state) => {
        const nextInstalled = { ...state.installed, [id]: false };
        persistLocalState({ installed: nextInstalled });
        return {
          installed: nextInstalled,
          busyId: null,
          noticeMessage: notice ?? null,
          successMessage: notice ? null : successKey.pluginUninstalled,
        };
      });
    } catch (error) {
      set({ busyId: null, error: error instanceof Error ? error.message : String(error) });
    }
  },

  // 插件没有真正的"删除"接口（backend-requests.md 需求20，全文档没有 plugin_packages.delete），
  // 复用 plugin_packages.uninstall——和上面的 uninstall action 调用的是同一个后端方法，只是
  // "我的插件"详情页调用这个入口，方便调用方（PluginDetailPage.tsx）在卸载后按需要做
  // 探测收尾（见该组件注释）。等后端真的给出独立的删除接口再拆开。
  deletePackage: async (id: string) => {
    set({ busyId: id, error: null, successMessage: null });
    try {
      const { notice } = await pluginPackagesApi.uninstall(id);
      set((state) => {
        const nextInstalled = { ...state.installed, [id]: false };
        persistLocalState({ installed: nextInstalled });
        return {
          installed: nextInstalled,
          busyId: null,
          noticeMessage: notice ?? null,
          successMessage: notice ? null : successKey.pluginUninstalled,
        };
      });
      return true;
    } catch (error) {
      set({ busyId: null, error: error instanceof Error ? error.message : String(error) });
      return false;
    }
  },

  clearInstallPending: (id: string) =>
    set((state) => ({ installPendingMap: { ...state.installPendingMap, [id]: undefined } })),

  clearError: () => set({ error: null }),
  clearNotice: () => set({ noticeMessage: null }),
  clearSuccess: () => set({ successMessage: null }),
}));

// success Toast 文案 key——照抄 connectorStore.ts 同名机制，放顶层而不是组件内联：调用方
// （MarketplacePage.tsx 卡片网格/PluginDetailPage.tsx 安装按钮）都只管调 install()，提示统一在
// 这个 store 里 set，顶层订阅者（ConnectorMarket/index.tsx）负责翻译成绿色 Toast。
const successKey = {
  pluginInstalled: 'connectorMarket.toast.pluginInstalled',
  pluginUninstalled: 'connectorMarket.toast.pluginUninstalled',
};
