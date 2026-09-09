import { create } from 'zustand';
import i18n from '../i18n';
import { connectorApi } from '../services/connectorApi';
import type { ConnectorConnectResponse, ConnectorDetail, ConnectorSummary, McpBusyKind } from '../types/connector';
import type { WebError } from '../types/websocket';

// 命名/组织风格照抄 cronStore.ts：inline action、无独立 actions 对象。
// connect/disconnect/registerCustom 悲观更新——这几个是重操作，且有 busyMap/
// 占位卡遮盖"进行中"，等后端返回真实态再 set 更稳妥。catch 静默降级（清空/复位，不抛出）。
//
// 2026-08-15 去除全局启用/禁用（状态C）：enabledMap/enable/disable 整个删除，MCP 不再有这个
// 维度，见 state-model-rectification-v2-remove-global-toggle.md。原来 enable/disable 那套
// "乐观更新+失败回滚"模式不再需要了。
//
// 2026-08-10 按新 MCP 接口文档整体改造，两处关键简化：
// 1. 不再有 myConnectorNames 这份 localStorage 名单。旧版用它模拟"曾经连接过的 MCP"来判断
//    "我的MCP"归属，是因为旧接口没有 source 字段。（2026-08-17 更新：这条结论对 built_in 不再
//    完全成立，见下方"广场/我的两份列表"说明——但 customize 恒在"我的"这半依然对。）
// 2. enabledMap 不再是纯前端乐观维护：loadList/loadDetail 现在直接用后端下发的真实 enabled 字段
//    做种子，页面刷新后不会再把"其实是禁用"的 MCP 误显示成"已启用"（旧版的兜底 `?? true` 只在
//    没连接过 enable/disable 时生效，是真实 bug，不是无害简化）。
// 3. detail/tools 合并成一个 cache——mcp.show 现在一次性带回 skills+tools，不再要单独的
//    connector.list_tools 请求；且按文档 §5.2.1 缓存策略，connect/disconnect/enable/disable/
//    registerCustom 成功后要清掉对应 name 的 detail 缓存（旧版完全没有这层失效逻辑）。
//
// 2026-08-10 状态机重构：
// - busyName（单值，全代码库 0 处读取）改成 busyMap（per-name Record，和 enabledMap 对称）。
//   connect/disconnect/deleteConnector/saveCredentialsAndConnect 操作前置 busyMap[name]=true、
//   完成/失败清 false，让 mcpState.ts 的 deriveCardState 能基于 busy 判断"连接中"占位，统一了
//   旧版"connect 走 busyName、registerCustom 走 connectionState='connecting'"两条不一致路径。
// - connect 动作发请求前补 patchConnection(name,'connecting') + busyMap[name]=true，让卡片层
//   基于 connectionState 也能看到"连接中"，不再只靠 busy。
// - registerCustom 占位卡删除 connected: false（字段已从 ConnectorSummary 移除）。
// - enabledMap 在派生层（mcpState.ts deriveCardState 入参）用 `?? false` 兜底，不在 store 改。
//
// 2026-08-17 广场/我的改成后端两份列表（MCP 接口文档 v2 §5.1）：
// mcp.list 现在要求 filter 参数，'builtin' = 全部预置（含未连接），'local' = 已连接的预置 +
// 全部自定义（不含未连接的预置）。实测过 dev_aipc_feat_v2 分支：缺省 filter 时后端按 'builtin'
// 处理，之前"只调一次 mcp.list({}) 靠前端按 source 分流"的做法会让"我的MCP"完全看不到自定义
// MCP——不是理论风险，是已确认的真实 bug（评估记录见
// cjh/feature/MCP/_migration/mcp-interface-v2-gap-assessment.md）。用户已确认（2026-08-17）
// 按新文档实现，2026-08-10"我的 vs 广场只看 source"的结论作废——一个已连接的预置 MCP 现在
// 应该同时出现在"广场"和"我的MCP"里，不再是"customize 永远在我的、built_in 永远在广场"这种
// 与连接状态无关的静态归属。
//
// 三份列表的关系：
// - builtinConnectors：filter='builtin' 的原始结果，"MCP广场"tab 直接渲染这个。
// - myConnectors：filter='local' 的原始结果，"我的MCP"tab 直接渲染这个。
// - connectors：按 name 合并两次 fetch 的并集视图（后 fetch 的覆盖前 fetch 的同名项），供
//   "不关心当前在哪个tab、只要按 name 查/枚举全部已知 MCP"的场景使用——McpDetailPage 的详情
//   查找（用户可能从任一 tab 点进来）、InputArea 会话内 MCP 选择器（要看全部已连接 MCP，不分
//   来源）、CreatePluginPage 绑定 MCP 的选择器（要能选到广场+我的的全部条目）。
interface ConnectorState {
  builtinConnectors: ConnectorSummary[];
  myConnectors: ConnectorSummary[];
  connectors: ConnectorSummary[];
  detailCache: Record<string, ConnectorDetail>;
  isLoading: boolean;
  error: string | null;
  // 成功操作后的反馈文案（连接成功/删除成功等），顶层 index.tsx 订阅它弹绿色 Toast，
  // 和 error 对称——之前只有 error 没有 success，导致注册成功只能靠 UI 跳转暗示，没有明确提示。
  successMessage: string | null;
  noticeMessage: string | null;
  // per-name 重操作进行中标记，取值是具体操作种类（connect/disconnect/saveCredentialsAndConnect
  // 之一），不忙就是 undefined。操作前置具体种类、完成/失败清
  // undefined。mcpState.ts deriveCardState 据此判断"连接中"占位，优先于 connectionState——
  // 操作进行中即使后端态还没翻也显示占位，遮盖瞬时不一致。
  // 旧版是单值 busyName: string | null，但全代码库 0 处读取；改成 per-name map 后和 enabledMap
  // 对称，且允许同一时刻多个 MCP 处于不同操作阶段（用户切卡片查看不互相干扰）。
  // 2026-08-11：又从 boolean 改成具体操作种类——纯 boolean 只能让卡片统一显示"连接中"，点
  // "解绑"（disconnect 也置 busy）时同样显示"连接中"会误导用户，见 mcpState.ts busyLabelKey。
  // 2026-08-17：'delete' 取值随 deleteConnector action 一并删除，不再有"删除中"这个分支。
  busyMap: Record<string, McpBusyKind | undefined>;

  loadList: (filter: 'builtin' | 'local', options?: { silent?: boolean }) => Promise<void>;
  loadDetail: (name: string, options?: { refresh?: boolean }) => Promise<void>;
  installPackage: (assetId: string) => Promise<boolean>;
  uninstallPackage: (identifier: string) => Promise<boolean>;
  connect: (name: string) => Promise<ConnectorConnectResponse | null>;
  disconnect: (name: string) => Promise<void>;
  // 2026-08-17：deleteConnector（mcp.delete_custom，彻底删除自定义 MCP）的 UI 入口一度在
  // McpDetailPage 整体移除，这个 action 当时随之删除。2026-08-19 用户明确要求恢复：详情页断联态
  // （customize 且未连接）的"卸载"按钮要走真删除，不能只是再调一次 disconnect——那样"卸载"这个
  // 文案名不副实，实际效果还是解绑。成功后从三份列表里彻底移除该条目（不像 disconnect 那样只是
  // patch 状态字段），调用方拿到 false 表示失败，据此决定要不要继续停留在详情页。
  deleteConnector: (name: string) => Promise<boolean>;
  // 取代旧版 authComplete：一次 hold-open 请求等到最终结果，调用方（CliAuthModal）不用再自己轮询。
  waitAuth: (name: string, stepIndex: number) => Promise<ConnectorConnectResponse | null>;
  // 插"连接中"占位卡片（同步，调用方不 await 也能立刻看到）→ 长 RPC（mcp.register_custom
  // 内部"写配置→探活→注册"，hold 住到探活完成，最长 10min，见 MCP 接口文档 §5.6）在后台跑
  // →跑完 patchConnection 更新占位卡片态 + loadList 拉真实数据覆盖。成功弹 success Toast、
  // 失败弹 error Toast（带真实后端错误）。
  // 2026-08-11 从纯 fire-and-forget（返回 void）改成返回 Promise：原来的设计是"调完立即让 UI
  // 跳我的MCP，用户在那边看占位卡从连接中变已连接"，但用户实测发现 ConnectorMarket/index.tsx
  // 一旦切回 market 视图会立刻非静默 loadList 一次，这次 loadList 跑在 register_custom 的 10min
  // 长 RPC 真正落地之前，会用后端"还没这条记录"的真实态整份覆盖掉刚插的占位卡，导致用户跳过去
  // 那一刻列表里根本看不到刚创建的 MCP。改成返回 Promise 后，唯一调用方（RegisterMcpPage.tsx）
  // 可以自己选择 await 到真正结果出来再跳转，不再依赖"占位卡活得比这次 loadList 久"这个不成立
  // 的时序假设；调用方也可以选择不 await（该方法本身依然是"先同步插占位卡"，语义不变）。
  registerCustom: (params: {
    name: string;
    transport: 'stdio' | 'sse' | 'http' | 'streamable-http';
    command?: string;
    args?: string[];
    env?: Record<string, string>;
    url?: string;
    headers?: Record<string, string>;
    timeoutS?: number;
  }) => Promise<ConnectorConnectResponse | null>;
  saveCredentialsAndConnect: (name: string, tokens: Record<string, string>) => Promise<ConnectorConnectResponse | null>;
  clearError: () => void;
  clearSuccess: () => void;
  clearNotice: () => void;
}

function patchConnection(
  connectors: ConnectorSummary[],
  name: string,
  connectionState: ConnectorSummary['connectionState'],
): ConnectorSummary[] {
  return connectors.map((c) => (c.name === name ? { ...c, connectionState } : c));
}

// 2026-08-17：广场/我的拆成两份原始列表后，任何按 name 的重操作（connect/disconnect/
// registerCustom）都可能命中 builtinConnectors/myConnectors/connectors 三份数组里的任意子集
// （比如一个刚连上的预置 MCP 在 builtinConnectors 里有、myConnectors 里还没有——要等下面的
// scheduleQuickRefresh 重新拉 local 才会补进去，见该函数注释）。这个 helper 统一对三份数组
// 做同样的 map，调用点不用分别写三遍。
// 2026-08-17 同日追加：原来还有一个对称的 removeFromAll（filter 掉某 name），当时只被已删除的
// deleteConnector action 用，随它一起删掉过；2026-08-19 deleteConnector 恢复后不再单独拆出这个
// helper——只有一个调用点，直接在 deleteConnector 里内联 filter，没必要为了对称性重新抽象。
function patchConnectionAll(
  state: Pick<ConnectorState, 'connectors' | 'builtinConnectors' | 'myConnectors'>,
  name: string,
  connectionState: ConnectorSummary['connectionState'],
): Pick<ConnectorState, 'connectors' | 'builtinConnectors' | 'myConnectors'> {
  return {
    connectors: patchConnection(state.connectors, name, connectionState),
    builtinConnectors: patchConnection(state.builtinConnectors, name, connectionState),
    myConnectors: patchConnection(state.myConnectors, name, connectionState),
  };
}

// connectors（合并视图）的更新方式：按 name 用最新一次 fetch 的结果覆盖旧值，不在两次 fetch
// 之间的项保留原样——builtin/local 两份原始结果各自持有权威数据，合并视图只是把它们按 name
// 拍平方便按名查找，不代表"这个 name 一定同时在两份原始列表里都出现过"。
function mergeByName(existing: ConnectorSummary[], fresh: ConnectorSummary[]): ConnectorSummary[] {
  const map = new Map(existing.map((c): [string, ConnectorSummary] => [c.name, c]));
  for (const item of fresh) map.set(item.name, item);
  return Array.from(map.values());
}

// connect/disconnect/enable/disable/registerCustom 成功后，name 对应的 detail 缓存（含 tools）
// 必须失效——见文档 §5.2.1，这几个操作都可能改变已连接态/enabled/tools 可见性。
function invalidateDetail(detailCache: Record<string, ConnectorDetail>, name: string): Record<string, ConnectorDetail> {
  if (!(name in detailCache)) return detailCache;
  const next = { ...detailCache };
  delete next[name];
  return next;
}

// 2026-08-20 MCP 连接错误提示友好化（见 MCP连接错误提示友好化-接口对接说明.md）：CLI 驱动型
// MCP（飞书/钉钉/企微等）连接失败时，后端在 mcp.connect/mcp.wait_auth 的 ok:false payload 里新增
// code（MCP_RUNTIME_MISSING/MCP_INSTALL_NETWORK/MCP_CLI_INCOMPLETE 三种可分类失败）+ runtime +
// install_cmd 结构化字段，替代原来直接透传的底层报错串（如 `[WinError 2]`）。这里按 code 选 i18n
// key 拼出用户可读文案；非这三种 code（如 MCP_BAD_REQUEST、非 CLI 路径失败）走 error.message 兜底，
// 不受影响。只有 connect/waitAuth 两个 action 的 catch 分支会用到——文档明确只有这两个 RPC 的 CLI
// 边界失败会下发新字段，disconnect/deleteConnector/registerCustom 等不受影响。
const RUNTIME_LABELS: Record<string, string> = { node: 'Node.js', python: 'Python' };

function friendlyCliError(error: unknown): string {
  if (!(error instanceof Error)) return String(error);
  const code = (error as WebError).code;
  const payload = (error as WebError).payload as { runtime?: string; install_cmd?: string } | undefined;
  const runtime = payload?.runtime ?? '';
  const label = (runtime && RUNTIME_LABELS[runtime]) || runtime;
  const installCmd = payload?.install_cmd ?? '';

  if (code === 'MCP_RUNTIME_MISSING') {
    return label
      ? i18n.t('connectorMarket.errors.runtimeMissingNamed', { runtime: label })
      : i18n.t('connectorMarket.errors.runtimeMissing');
  }
  if (code === 'MCP_INSTALL_NETWORK') return i18n.t('connectorMarket.errors.installNetwork');
  if (code === 'MCP_CLI_INCOMPLETE') {
    return installCmd
      ? i18n.t('connectorMarket.errors.cliIncompleteNamed', { command: installCmd })
      : i18n.t('connectorMarket.errors.cliIncomplete');
  }
  return error.message; // 兜底：非 CLI 边界失败，展示原文
}

// 连接、断开或删除完成后补一次快速刷新，将乐观更新校准为后端真实状态。
// 用 debounce（清掉上一个定时器再重新计时）而不是每次操作都各自起一个 setTimeout，避免短时间
// 连点多个操作（比如切换好几个 MCP 的启用开关）时打出一串重复的 list 请求。
//
// 2026-08-17：同时刷 builtin 和 local 两份——mcp.list 是网关本地处理，不转发 agent、不被
// connect 这类长 RPC 阻塞（文档 §4.1），两次请求都很轻，值得都刷一遍：比如一个预置 MCP 刚连上，
// patchConnectionAll 只能更新它已经在的那几份数组，真正让它"出现在我的MCP列表里"这件事只有
// 重新拉一次 local 才做得到（patch 只改已有项的字段，不会往数组里新增项）。
let quickRefreshTimer: number | null = null;
const QUICK_REFRESH_DELAY_MS = 1500;

function scheduleQuickRefresh(get: () => ConnectorState): void {
  if (quickRefreshTimer !== null) window.clearTimeout(quickRefreshTimer);
  quickRefreshTimer = window.setTimeout(() => {
    quickRefreshTimer = null;
    void get().loadList('builtin', { silent: true });
    void get().loadList('local', { silent: true });
  }, QUICK_REFRESH_DELAY_MS);
}

// ExtensionPickerPanel 每次打开都无条件重新 loadList，
// 同一个 filter 可能有多次调用同时在途（比如上一次请求撞上网关卡顿还没回来，用户就关了面板
// 又重新打开，打出了第二次）。没有这层保护时，先发出但后落地的旧请求——不管它最终是成功还是
// 超时失败——会在它姗姗来迟的那一刻，把这期间已经由更新的请求成功写入的数据重新覆盖掉：这正是
// 用户 2026-08-18 实测复现的现象（控制台 `myConnectors` 明明有 7 条数据，面板却渲染成空）。
// 用一个按 filter 自增的序号给每次调用打标记，只有当前仍是"这个 filter 最后一次发起的调用"，
// 它的结果（无论成功失败）才允许写入 state；比它更晚发起的调用已经存在，说明这次结果已经过时，
// 直接丢弃、不覆盖任何东西——不管这次结果本身是好是坏。
const listRequestSeq: Record<'builtin' | 'local', number> = { builtin: 0, local: 0 };

export const useConnectorStore = create<ConnectorState>((set, get) => ({
  builtinConnectors: [],
  myConnectors: [],
  connectors: [],
  detailCache: {},
  isLoading: false,
  error: null,
  successMessage: null,
  noticeMessage: null,
  busyMap: {},

  // silent=true 仅用于安装/连接后的快速校准，不改变页面的加载状态。
  loadList: async (filter, options) => {
    const silent = options?.silent ?? false;
    const mySeq = ++listRequestSeq[filter];
    if (!silent) set({ isLoading: true, error: null });
    try {
      const response = await connectorApi.list(filter);
      if (listRequestSeq[filter] !== mySeq) return; // 已有更新的同 filter 调用发起过，这次结果作废
      const fresh = response;
      set((state) => ({
        connectors: mergeByName(state.connectors, fresh),
        ...(filter === 'builtin' ? { builtinConnectors: fresh } : { myConnectors: fresh }),
        isLoading: false,
      }));
    } catch (error) {
      if (listRequestSeq[filter] !== mySeq) return;
      if (silent) return;
      set({
        ...(filter === 'builtin' ? { builtinConnectors: [] } : { myConnectors: [] }),
        isLoading: false,
        error: error instanceof Error ? error.message : String(error),
      });
    }
  },

  loadDetail: async (name: string, options) => {
    if (!options?.refresh && get().detailCache[name]) return;
    try {
      const detail = await connectorApi.show(name);
      set((state) => ({
        detailCache: { ...state.detailCache, [name]: detail },
      }));
    } catch (error) {
      set({ error: error instanceof Error ? error.message : String(error) });
    }
  },

  installPackage: async (assetId: string) => {
    set((state) => ({
      busyMap: { ...state.busyMap, [assetId]: 'install' },
      error: null,
    }));
    try {
      const response = await connectorApi.install(assetId);
      const runtimeName = response.item.name;
      const patchInstalled = (items: ConnectorSummary[]) =>
        items.map((item) =>
          item.id === assetId || item.runtimePackageName === runtimeName
            ? { ...item, name: runtimeName, runtimePackageName: runtimeName, installed: true }
            : item,
        );
      set((state) => ({
        connectors: patchInstalled(state.connectors),
        builtinConnectors: patchInstalled(state.builtinConnectors),
        myConnectors: patchInstalled(state.myConnectors),
        busyMap: { ...state.busyMap, [assetId]: undefined },
        successMessage: successKey.mcpInstalled,
      }));
      void get().loadList('builtin', { silent: true });
      void get().loadList('local', { silent: true });
      return true;
    } catch (error) {
      set((state) => ({
        busyMap: { ...state.busyMap, [assetId]: undefined },
        error: error instanceof Error ? error.message : String(error),
      }));
      return false;
    }
  },

  uninstallPackage: async (identifier: string) => {
    set((state) => ({
      busyMap: { ...state.busyMap, [identifier]: 'uninstall' },
      error: null,
      noticeMessage: null,
    }));
    try {
      const response = await connectorApi.uninstall(identifier);
      const assetId = response.item.id;
      const runtimeName = response.item.name;
      const keepCatalogCard = (item: ConnectorSummary) =>
        item.id === assetId || item.runtimePackageName === runtimeName
          ? { ...item, installed: false, connectionState: 'disconnected' as const }
          : item;
      set((state) => {
        const detailCache = { ...state.detailCache };
        delete detailCache[identifier];
        delete detailCache[assetId];
        delete detailCache[runtimeName];
        return {
          connectors: state.connectors.map(keepCatalogCard),
          builtinConnectors: state.builtinConnectors.map(keepCatalogCard),
          myConnectors: state.myConnectors.filter(
            (item) => item.id !== assetId && item.runtimePackageName !== runtimeName,
          ),
          detailCache,
          busyMap: { ...state.busyMap, [identifier]: undefined },
          successMessage: successKey.mcpUninstalled,
          noticeMessage: response.applied
            ? null
            : response.error || i18n.t('connectorMarket.toast.mcpUninstallPendingReload'),
        };
      });
      void get().loadList('builtin', { silent: true });
      void get().loadList('local', { silent: true });
      return true;
    } catch (error) {
      set((state) => ({
        busyMap: { ...state.busyMap, [identifier]: undefined },
        error: error instanceof Error ? error.message : String(error),
      }));
      return false;
    }
  },

  connect: async (name: string) => {
    // 发请求前先翻 connecting + 置 busy，让卡片层立刻看到"连接中"占位（基于 connectionState
    // 或 busy 都能判出来，见 mcpState.ts deriveCardState）。旧版只置 busyName 且不翻 connecting，
    // 卡片层读不到中间态。失败时翻 error 让"连失败"可见（旧版静默回到原态，用户分不出没连过 vs 连失败）。
    // 2026-08-21：真正连接成功（本方法/waitAuth/saveCredentialsAndConnect 三处 response.type===
    // 'connected' 分支）都统一 set successMessage，让卡片网格快速连接、Token/CLI授权弹窗、
    // 详情页安装按钮这几个各自独立的调用方不用各自维护本地 toast，一次覆盖全部入口。
    set((state) => ({
      ...patchConnectionAll(state, name, 'connecting'),
      busyMap: { ...state.busyMap, [name]: 'connect' },
      error: null,
    }));
    try {
      const response = await connectorApi.connect(name);
      if (response.type === 'connected') {
        set((state) => ({
          ...patchConnectionAll(state, name, 'connected'),
          detailCache: invalidateDetail(state.detailCache, name),
          busyMap: { ...state.busyMap, [name]: undefined },
          successMessage: successKey.mcpConnected,
        }));
      } else {
        // credentials_required / auth_required 分支：连接未完成但非失败，回 idle 等用户走弹窗流程。
        set((state) => ({
          ...patchConnectionAll(state, name, 'disconnected'),
          busyMap: { ...state.busyMap, [name]: undefined },
        }));
      }
      scheduleQuickRefresh(get);
      return response;
    } catch (error) {
      set((state) => ({
        ...patchConnectionAll(state, name, 'error'),
        busyMap: { ...state.busyMap, [name]: undefined },
        error: friendlyCliError(error),
      }));
      scheduleQuickRefresh(get);
      return null;
    }
  },

  disconnect: async (name: string) => {
    set((state) => ({
      busyMap: { ...state.busyMap, [name]: 'disconnect' },
      error: null,
    }));
    try {
      await connectorApi.disconnect(name);
      set((state) => ({
        ...patchConnectionAll(state, name, 'disconnected'),
        detailCache: invalidateDetail(state.detailCache, name),
        busyMap: { ...state.busyMap, [name]: undefined },
      }));
      scheduleQuickRefresh(get);
    } catch (error) {
      set((state) => ({
        busyMap: { ...state.busyMap, [name]: undefined },
        error: error instanceof Error ? error.message : String(error),
      }));
      scheduleQuickRefresh(get);
    }
  },

  deleteConnector: async (name: string) => {
    set((state) => ({
      busyMap: { ...state.busyMap, [name]: 'delete' },
      error: null,
    }));
    try {
      await connectorApi.deleteCustom(name);
      set((state) => ({
        connectors: state.connectors.filter((c) => c.name !== name),
        builtinConnectors: state.builtinConnectors.filter((c) => c.name !== name),
        myConnectors: state.myConnectors.filter((c) => c.name !== name),
        detailCache: invalidateDetail(state.detailCache, name),
        busyMap: { ...state.busyMap, [name]: undefined },
      }));
      scheduleQuickRefresh(get);
      return true;
    } catch (error) {
      set((state) => ({
        busyMap: { ...state.busyMap, [name]: undefined },
        error: error instanceof Error ? error.message : String(error),
      }));
      scheduleQuickRefresh(get);
      return false;
    }
  },

  waitAuth: async (name: string, stepIndex: number) => {
    // CLI OAuth 多步授权推进的续接请求，本质还是"正在连接"，busyKind 用 'connect'。
    set((state) => ({
      ...patchConnectionAll(state, name, 'connecting'),
      busyMap: { ...state.busyMap, [name]: 'connect' },
    }));
    try {
      const response = await connectorApi.waitAuth(name, stepIndex);
      if (response.type === 'connected') {
        set((state) => ({
          ...patchConnectionAll(state, name, 'connected'),
          detailCache: invalidateDetail(state.detailCache, name),
          busyMap: { ...state.busyMap, [name]: undefined },
          successMessage: successKey.mcpConnected,
        }));
      } else {
        set((state) => ({ busyMap: { ...state.busyMap, [name]: undefined } }));
      }
      scheduleQuickRefresh(get);
      return response;
    } catch (error) {
      set((state) => ({
        ...patchConnectionAll(state, name, 'error'),
        busyMap: { ...state.busyMap, [name]: undefined },
        error: friendlyCliError(error),
      }));
      scheduleQuickRefresh(get);
      return null;
    }
  },

  registerCustom: async (params) => {
    // 1) 立即插一张"连接中"占位卡片（同步，函数是 async 但这段跑在第一个 await 之前，调用方
    // 哪怕不 await 这个 Promise 也能马上看到）。占位卡只填得出 name/displayName（=name），其他
    // 字段后端还没下发，用合理默认值占位：source=customize（自定义注册的必然归属"我的"）、
    // connectionState=connecting、icon/description 留空。
    // RPC 完成后 loadList 会用真实数据覆盖。
    const placeholder: ConnectorSummary = {
      id: params.name,
      name: params.name,
      runtimePackageName: params.name,
      displayName: params.name,
      description: '',
      category: 'custom',
      integrationType: params.transport === 'stdio' ? 'stdio-mcp' : 'remote-mcp',
      connectionState: 'connecting',
      hasBundledSkills: false,
      source: 'customize',
      installed: true,
    };
    // 占位卡只可能是 customize（register_custom 只用来注册自定义 MCP），只插进 myConnectors +
    // 合并视图 connectors，不碰 builtinConnectors（customize 恒不出现在 filter=builtin 的结果里）。
    set((state) => ({
      connectors: state.connectors.some((c) => c.name === params.name)
        ? state.connectors.map((c) => (c.name === params.name ? placeholder : c))
        : [...state.connectors, placeholder],
      myConnectors: state.myConnectors.some((c) => c.name === params.name)
        ? state.myConnectors.map((c) => (c.name === params.name ? placeholder : c))
        : [...state.myConnectors, placeholder],
      error: null,
      successMessage: null,
    }));

    // 2) mcp.register_custom 本身仅落盘不连接（接口文档 §5.6："填表 → register_custom →
    //   registered（仅落盘，秒回）→ mcp.connect → connected"）。2026-08-20 用户明确要求：注册
    //   成功后紧接着真正调一次 connect，不要在没有真实连接的情况下就把卡片/Toast 冒充成"已连接"
    //   （旧版就是这个 bug：不看响应 type，请求一成功就无条件 patch 成 'connected'）。
    //   直接复用 connect() action 而不是在这里重新实现一遍 connecting/connected/error 状态机+
    //   busyMap+friendlyCliError 兜底——connect() 失败时会自己把 store.error 写成友好文案，走
    //   顶层已有的红色 Toast 通道；这里只需要另外补一条独立的绿色"已创建成功"Toast，不用拼一条
    //   大字符串（两条 Toast 各管各的，成功/失败原因分别展示更清楚）。
    try {
      const response = await connectorApi.registerCustom(params);
      const connectResult = await get().connect(params.name);
      set({
        successMessage: connectResult?.type === 'connected' ? successKey.mcpCreatedAndConnected : successKey.mcpCreated,
      });
      void get().loadList('local');
      return response;
    } catch (error) {
      set((state) => ({
        connectors: patchConnection(state.connectors, params.name, 'error'),
        myConnectors: patchConnection(state.myConnectors, params.name, 'error'),
        error: error instanceof Error ? error.message : String(error),
      }));
      void get().loadList('local');
      return null;
    }
  },

  saveCredentialsAndConnect: async (name: string, tokens: Record<string, string>) => {
    set((state) => ({
      busyMap: { ...state.busyMap, [name]: 'saveCredentials' },
      error: null,
    }));
    try {
      await connectorApi.saveCredentials(name, tokens);
      const response = await connectorApi.connect(name);
      if (response.type === 'connected') {
        set((state) => ({
          ...patchConnectionAll(state, name, 'connected'),
          detailCache: invalidateDetail(state.detailCache, name),
          busyMap: { ...state.busyMap, [name]: undefined },
          successMessage: successKey.mcpConnected,
        }));
      } else {
        set((state) => ({
          ...patchConnectionAll(state, name, 'disconnected'),
          busyMap: { ...state.busyMap, [name]: undefined },
        }));
      }
      scheduleQuickRefresh(get);
      return response;
    } catch (error) {
      set((state) => ({
        ...patchConnectionAll(state, name, 'error'),
        busyMap: { ...state.busyMap, [name]: undefined },
        error: error instanceof Error ? error.message : String(error),
      }));
      scheduleQuickRefresh(get);
      return null;
    }
  },

  clearError: () => set({ error: null }),
  clearSuccess: () => set({ successMessage: null }),
  clearNotice: () => set({ noticeMessage: null }),
}));

// TEMP DEBUG（2026-08-18，排查"扩展面板 MCP 列表后端已返回数据但界面不显示"，定位后删除）：
// 挂到 window 方便控制台直接读 store 实时快照，不用装 React DevTools。
// 复现时控制台跑：window.__connectorStore.getState().myConnectors
if (typeof window !== 'undefined') {
  (window as unknown as { __connectorStore?: typeof useConnectorStore }).__connectorStore = useConnectorStore;
}

// success Toast 文案 key——放 store 顶层而不是组件内联，是因为 registerCustom 是 fire-and-reload，
// 成功发生在后台 .then 里，那时组件上下文已经不在了，得用稳定的常量 key 让顶层订阅者去翻译。
const successKey = {
  mcpInstalled: 'connectorMarket.toast.mcpInstalled',
  mcpUninstalled: 'connectorMarket.toast.mcpUninstalled',
  mcpConnected: 'connectorMarket.toast.mcpConnected',
  mcpCreated: 'connectorMarket.toast.mcpCreated',
  mcpCreatedAndConnected: 'connectorMarket.toast.mcpCreatedAndConnected',
};
