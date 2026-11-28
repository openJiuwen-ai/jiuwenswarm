import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { useTranslation } from 'react-i18next';
import { useConnectorStore, CONNECTOR_ERROR_TOAST_ID } from '../../stores/connectorStore';
import { usePluginPackageStore } from '../../stores/pluginPackageStore';
import { MarketplacePage, type TopTab, type MarketKind, type PluginKind } from './MarketplacePage';
import { PluginDetailPage } from './PluginDetailPage';
import { SystemPluginDetailPage } from './SystemPluginDetailPage';
import { McpDetailPage } from './McpDetailPage';
import { CreatePluginPage } from './CreatePluginPage';
import { RegisterMcpPage } from './RegisterMcpPage';
import { UploadFileCreateModal } from './UploadFileCreateModal';
import { Toast } from './Toast';
import { toast } from '../ui/Toast/toastStore';
import { equipmentListFilter } from '../../features/equipmentMarketplace';
import { OfficeSuiteDetail, useOfficeSuites } from '../../features/officeAddins/OfficeSuite';
import type { SuiteId } from '../../features/officeAddins/officeAddins';
import { useSystemPluginStore } from '../../stores/systemPluginStore';
import { webClient } from '../../services/webClient';
import { EXTENSION_PACKAGE_BUSY } from '../../services/extensionsV2Api';
import { browserClientCapabilities } from '../../features/webui/client/browserClient';
import { isEnterprise } from '../../edition';
import {
  connectorApi,
  type ConnectorSourceProvider,
} from '../../services/connectorApi';

// "管理我的插件/MCP" 一次性跳转握手：调用方（InputArea.tsx 的扩展面板）先把目标 tab 存进这个
// 模块级变量，再触发 `jiuwen:nav` 切到 connectorMarket——ConnectorMarketPanel 挂载是这次导航
// 触发的异步重渲染之后才发生的，没法用同步派发的第二个事件传参（挂载完成前监听器还不存在），
// 模块级变量不受 React 生命周期影响，挂载时的 useState 惰性初始化读取一次就够。用完立即清空，
// 纯粹是"下一次挂载读一次"的一次性握手，不是持续订阅的状态源。
let pendingManageView: { myKind: MarketKind } | null = null;

export function requestManageView(myKind: MarketKind) {
  pendingManageView = { myKind };
  window.dispatchEvent(new CustomEvent<string>('jiuwen:nav', { detail: 'connectorMarket' }));
}

type View =
  | { name: 'market' }
  | { name: 'plugin-detail'; id: string; fromMy: boolean }
  | { name: 'system-detail'; id: string }
  | { name: 'mcp-detail'; connectorName: string }
  | { name: 'office-suite'; suite: SuiteId }
  | { name: 'create-manual' }
  // editName 有值＝从 McpDetailPage 的"编辑"按钮进来，RegisterMcpPage 据此切到编辑态（name 只读+
  // 回填表单）；不传就是原有的"注册自定义MCP"新建流程，见该文件 editName prop 注释。
  | { name: 'register-mcp'; editName?: string };

// 顶层容器：topTab/myKind/view 是纯 UI 导航状态，留在这一层的 useState 就够——不像 demo
// 阶段那样需要额外把 installed/enabled 也提到这里，因为正式版这两个状态来自 zustand store，
// 本来就是跨组件重挂载持续存在的全局状态，不会因为详情页/创建页切换导致 MarketplacePage
// 卸载重装就丢失（demo 阶段那个"回退总退回默认插件tab"的 bug 根源就是这个，store 天然免疫）。
interface ConnectorMarketPanelProps {
  /**
   * MCP 详情页"试试这样用"里点某条示例——跳新会话并把示例文案填进输入框。真正的导航逻辑在
   * App.tsx（enterNewConversation 的 initialInputValue 机制），本组件不知道怎么跳转会话，
   * 只管往下透传。不传就是 McpDetailPage 自己退化成不可点击的纯展示（同款可选 prop 处理见
   * onUse/handleUseNotWired）。跟 CronPanel「通过对话创建」onCreateViaChat 是同一条路径
   * （App.tsx:2431），只是入口从定时任务面板换成了 MCP 详情页。
   */
  onUseExample?: (text: string, mcpName: string, displayName?: string) => void;
  /**
   * 插件详情页"试试这样用"里点某条示例——跟 onUseExample 是同一个设计，但插件版 quickInputs
   * 是后端 2026-08-21 新增的字段，第二个参数是 pluginId（不是 mcpName），单独开一个 prop
   * 而不是复用 onUseExample，见 PluginDetailPage.tsx 该 prop 的类型注释。
   */
  onUsePluginExample?: (text: string, pluginId: string) => void;
  /**
   * 插件/MCP 详情页顶部"使用"按钮——跳新会话并顺带打开这个扩展的会话内启用开关（跟
   * onUseExample 是同一条 requestSessionNavigation('new', ...) 通道，只是这次带的是
   * initialEnabledPlugins/initialEnabledMcps 而不是 initialInputValue，两者可以同时带
   * ——McpDetailPage 那边"使用"和"试试这样用"是两个不同按钮，各自只传各自需要的字段，但
   * "试试这样用"现在也会带上 mcpName，好让 App.tsx 把 initialEnabledMcps 一起传上，见
   * onUseExample 的类型注释）。
   */
  onUseExtension?: (payload: { kind: 'plugin' | 'mcp'; id: string }) => void;
  /**
   * 是否提供「使用 / 试试这样用」这类跳到个人会话的入口。个人版保持开启；团队版没有
   * 对应的个人会话页面，关掉后卡片和详情页都不渲染这些按钮。
   */
  sessionUse?: boolean;
  /**
   * "创建"下拉菜单里的"通过聊天创建"——跳新会话，自动预填"帮我创建一个xxx插件，擅长xxx"提示词
   * 并把 plugin-creator 选中为技能 chip（2026-08-25，取代此前"不带预填"的决定）。跟
   * SkillPanel.handleCreateViaChat（"通过聊天创建技能"）走同一条 jiuwen:new-conversation
   * 事件链路，而不是 onUseExample/onUseExtension 用的 requestSessionNavigation('new', ...)
   * ——因为 plugin-creator 本质是技能，需要 skill chip 机制而不是 initialEnabledPlugins。
   * 不传这个 prop 就退化成原来的"尚未接入"提示（同款可选 prop 处理）。
   */
  onCreateViaChat?: () => void;
}

export function ConnectorMarketPanel({
  onUseExample,
  onUsePluginExample,
  onUseExtension,
  onCreateViaChat,
  sessionUse = true,
}: ConnectorMarketPanelProps = {}) {
  const { t } = useTranslation();
  const [view, setView] = useState<View>({ name: 'market' });
  // Enterprise hides the plugin plaza; personal keeps the original default.
  const [topTab, setTopTab] = useState<TopTab>(() =>
    pendingManageView ? 'my' : isEnterprise() ? 'mcp' : 'plugin',
  );
  const [myKind, setMyKind] = useState<MarketKind>(() => {
    if (isEnterprise()) {
      // Enterprise "我的扩展" only keeps the connector sub-tab.
      pendingManageView = null;
      return 'mcp';
    }
    const kind = pendingManageView?.myKind ?? 'plugin';
    pendingManageView = null;
    return kind;
  });
  const [pluginKind, setPluginKind] = useState<PluginKind>('agent');
  const [uploadModalOpen, setUploadModalOpen] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Office 加载项套件只有桌面端有：网页端状态接口 404，suites 为 null，广场不出卡片。
  const office = useOfficeSuites();
  const officeStatus = view.name === 'office-suite' ? office.suites?.[view.suite] : undefined;
  const [connectorSources, setConnectorSources] = useState<ConnectorSourceProvider[]>([]);
  const [selectedConnectorSourceId, setSelectedConnectorSourceId] = useState('');

  const loadConnectorList = useConnectorStore((s) => s.loadList);
  const loadPluginList = usePluginPackageStore((s) => s.loadList);
  const enterpriseEdition = isEnterprise();

  // 2026-08-11：connectorStore/pluginPackageStore 的所有 action（connect/disconnect/enable/
  // disable/registerCustom/deleteConnector/saveCredentialsAndConnect/waitAuth，插件那边同理）
  // 失败时统一只是把 error 写进各自 store，之前没有任何组件读这个字段展示给用户——真实案例：
  // 连接飞书 MCP 时后端返回 "CLI version check failed"，用户点了"安装"，按钮转一下又变回原样，
  // 什么提示都没有。这里在顶层容器统一订阅两个 store 的 error，一旦出现就用红色 Toast 弹出来
  // 再清空，不用在每个调用点（MarketplacePage/McpDetailPage/ConnectTokenModal/
  // RegisterMcpPage...）各自重复接线一遍——action 一多很容易漏改。
  // loadList(silent) 的轮询失败不会走到这里（loadList 内部 `if (silent) return` 直接跳过设置
  // error，不会打断用户），只有用户主动触发的操作失败才会弹。
  const connectorError = useConnectorStore((s) => s.error);
  const clearConnectorError = useConnectorStore((s) => s.clearError);
  const connectorSuccess = useConnectorStore((s) => s.successMessage);
  const clearConnectorSuccess = useConnectorStore((s) => s.clearSuccess);
  const connectorNotice = useConnectorStore((s) => s.noticeMessage);
  const clearConnectorNotice = useConnectorStore((s) => s.clearNotice);
  const loadSystemCatalog = useSystemPluginStore((s) => s.loadCatalog);
  const loadSystemMine = useSystemPluginStore((s) => s.loadMine);
  const importPackagePath = useSystemPluginStore((s) => s.importPath);
  const systemError = useSystemPluginStore((s) => s.error);
  const systemErrorCode = useSystemPluginStore((s) => s.errorCode);
  const clearSystemError = useSystemPluginStore((s) => s.clearError);
  const pluginError = usePluginPackageStore((s) => s.error);
  const clearPluginError = usePluginPackageStore((s) => s.clearError);
  const pluginNotice = usePluginPackageStore((s) => s.noticeMessage);
  const clearPluginNotice = usePluginPackageStore((s) => s.clearNotice);
  const pluginSuccess = usePluginPackageStore((s) => s.successMessage);
  const clearPluginSuccess = usePluginPackageStore((s) => s.clearSuccess);
  const [successToast, setSuccessToast] = useState<string | null>(null);

  useEffect(() => {
    if (connectorError) {
      // 与抽屉共用 id 以合并同一次失败；显式补上页面 Toast 的 testid/data-variant，
      // 否则全局默认 ui-toast 无法被自动化识别。
      toast.open({
        id: CONNECTOR_ERROR_TOAST_ID,
        content: connectorError,
        variant: 'error',
        duration: 5,
        testId: 'connector-market-toast',
        dataVariant: 'error',
      });
      clearConnectorError();
    }
  }, [connectorError, clearConnectorError]);

  useEffect(() => {
    if (systemError) {
      // Busy packages are a normal, transient state; show the actionable copy
      // instead of the Core report message.
      toast.open({
        id: CONNECTOR_ERROR_TOAST_ID,
        content: systemErrorCode === EXTENSION_PACKAGE_BUSY
          ? t('connectorMarket.systemPlugin.busyMessage')
          : systemError,
        variant: 'error',
        duration: 5,
      });
      clearSystemError();
    }
  }, [systemError, systemErrorCode, clearSystemError, t]);

  useEffect(() => {
    if (pluginError) {
      // 插件错误与连接器错误共用同一 id：页内任意操作失败都替换上一条，不叠两条。
      toast.open({ id: CONNECTOR_ERROR_TOAST_ID, content: pluginError, variant: 'error', duration: 5 });
      clearPluginError();
    }
  }, [pluginError, clearPluginError]);

  // 成功反馈订阅：registerCustom 等后台 RPC 跑完成功后 set successMessage，这里翻译成文案
  // 弹绿色 Toast——用户已确认（2026-08-10）成功要弹。和 error 订阅对称，successMessage 存的是
  // i18n key（store 在后台 .then 里没法拿 t()，只能存 key 让带 t 的组件翻译）。
  useEffect(() => {
    if (connectorSuccess) {
      setSuccessToast(t(connectorSuccess));
      clearConnectorSuccess();
    }
  }, [connectorSuccess, clearConnectorSuccess, t]);

  useEffect(() => {
    if (connectorNotice) {
      setSuccessToast(connectorNotice);
      clearConnectorNotice();
    }
  }, [connectorNotice, clearConnectorNotice]);

  // 插件版同款——install() 落盘成功后 set 的 i18n key，覆盖卡片网格快速安装 + 详情页安装按钮
  // 两个入口（2026-08-21 用户明确要求两处都要有提示）。
  useEffect(() => {
    if (pluginSuccess) {
      setSuccessToast(t(pluginSuccess));
      clearPluginSuccess();
    }
  }, [pluginSuccess, clearPluginSuccess, t]);

  // plugin_packages.uninstall 的 notice 是后端直接下发的原文提示（不是 i18n key，跟 error 一样
  // 原样透传，不经过 t()），复用同一个绿色 Toast 展示——语义上是"卸载成功但有件事要提醒"，不是
  // 失败，用 success 变体。
  useEffect(() => {
    if (pluginNotice) {
      setSuccessToast(pluginNotice);
      clearPluginNotice();
    }
  }, [pluginNotice, clearPluginNotice]);

  // 插件和 MCP 各自独立拉取：当前在插件页就只拉插件列表，在 MCP 页就只拉 MCP 列表，不像
  // 之前那样挂载时无脑把两份列表都拉一遍——两边数据量都可能不小，没必要为了看不见的那一页
  // 多打一次请求。activeKind 是"当前实际展示的是哪种列表"：topTab 直接是 'plugin'/'mcp' 时
  // 就是它本身，topTab 是 'my'（我的扩展）时看 myKind 子筛选。
  const activeKind: MarketKind = topTab === 'my' ? myKind : topTab === 'mcp' ? 'mcp' : 'plugin';

  // 企业版拉取连接器远端源；单机不请求，避免多源页签（CMS-V-04）。
  // Empty enabled list ≠ request failure: do not forge a fake source; catalogue
  // effect already skips when selectedConnectorSourceId is empty.
  useEffect(() => {
    if (!enterpriseEdition) {
      setConnectorSources([]);
      setSelectedConnectorSourceId('');
      return;
    }
    let cancelled = false;
    void connectorApi
      .listSourceProviders()
      .then((providers) => {
        if (cancelled) return;
        const enabled = providers.filter(
          (item) =>
            item.enabled !== false &&
            (!item.capabilities || item.capabilities.length === 0 || item.capabilities.includes('search')),
        );
        setConnectorSources(enabled);
        setSelectedConnectorSourceId((current) => {
          if (current && enabled.some((item) => item.source_id === current)) return current;
          return enabled[0]?.source_id || '';
        });
        if (enabled.length === 0) {
          toast.open({
            content: t('connectorMarket.sources.noSources'),
            variant: 'info',
            duration: 5,
            testId: 'connector-market-toast',
          });
        }
      })
      .catch(() => {
        if (cancelled) return;
        // Keep prior connectorSources / selection; surface failure instead of
        // forging DEFAULT_CONNECTOR_SOURCE_ID (openjiuwen-hub).
        toast.open({
          id: CONNECTOR_ERROR_TOAST_ID,
          content: t('connectorMarket.sources.loadFailed'),
          variant: 'error',
          duration: 5,
          testId: 'connector-market-toast',
          dataVariant: 'error',
        });
      });
    return () => {
      cancelled = true;
    };
  }, [enterpriseEdition, t]);

  // 一键还原会重启后端，此时列表请求必然失败，还原前的旧快照（开关仍为开启）会留在页面上。
  // 记录连接与客户端声明是否就绪：未就绪时先不拉取，就绪后（含重连成功）effect 会自动重跑。
  const [connected, setConnected] = useState(() => webClient.getState() === 'ready');
  useEffect(() => webClient.onStateChange((state) => setConnected(state === 'ready')), []);
  const clientContextReady = useSyncExternalStore(
    browserClientCapabilities.subscribe,
    () => !browserClientCapabilities.isChanging() && !browserClientCapabilities.isUnavailable()
  );

  // 存档还原会通过 set_enabled 关闭插件并广播 extensions.v2.installed；重连成功同样意味着
  // 后端换了实例。两种情况都要重拉列表，否则开关会一直停留在还原前的状态。
  // silent=false 用于正常的进入/切换列表（保留加载态），silent=true 只用于失败后的重试。
  const reloadVisibleLists = useCallback(async (silent: boolean): Promise<boolean> => {
    if (view.name !== 'market') return true;
    // 声明还没到就发请求会被判为过期上下文；返回失败交给重试队列等它就绪。
    if (!connected || !clientContextReady) return false;
    const scope = topTab === 'my' ? 'mine' : 'catalog';
    const options = { silent };
    if (activeKind === 'mcp') {
      // Enterprise catalogue aligns with skills.source.search: no list until a
      // source is selected; source_id is always sent. Personal omits source_id.
      if (enterpriseEdition && scope === 'catalog' && !selectedConnectorSourceId) {
        return true;
      }
      const catalogSourceId =
        enterpriseEdition && scope === 'catalog' ? selectedConnectorSourceId : undefined;
      return loadConnectorList(equipmentListFilter('mcp', scope), {
        ...options,
        sourceId: catalogSourceId,
      });
    }
    const results = await Promise.all([
      loadPluginList(equipmentListFilter('plugin', scope), options),
      topTab === 'my'
        ? loadSystemMine(options)
        : pluginKind === 'system' ? loadSystemCatalog(options) : Promise.resolve(true),
    ]);
    return results.every(Boolean);
  }, [
    activeKind,
    topTab,
    pluginKind,
    view.name,
    connected,
    clientContextReady,
    loadConnectorList,
    loadPluginList,
    loadSystemCatalog,
    loadSystemMine,
    enterpriseEdition,
    selectedConnectorSourceId,
  ]);

  // 还原重启后 AgentServer 仍在启动，首批列表请求会超时；只对失败结果按梯度静默重试，
  // 避免用户必须手动刷新才能看到还原后的开关状态。
  const reloadRef = useRef(reloadVisibleLists);
  reloadRef.current = reloadVisibleLists;
  const retryingRef = useRef(false);
  const disposedRef = useRef(false);
  const retryTimersRef = useRef<number[]>([]);
  const scheduleReload = useCallback(() => {
    if (retryingRef.current || disposedRef.current) return;
    retryingRef.current = true;
    const backoff = [0, 2000, 5000, 10000, 20000, 30000];
    const run = async (attempt: number) => {
      const ok = await reloadRef.current(true);
      if (disposedRef.current) {
        retryingRef.current = false;
        return;
      }
      if (ok || attempt + 1 >= backoff.length) {
        retryingRef.current = false;
        return;
      }
      retryTimersRef.current.push(window.setTimeout(() => void run(attempt + 1), backoff[attempt + 1]));
    };
    void run(0);
  }, []);
  useEffect(() => {
    disposedRef.current = false;
    return () => {
      disposedRef.current = true;
      retryingRef.current = false;
      for (const id of retryTimersRef.current) window.clearTimeout(id);
      retryTimersRef.current = [];
    };
  }, []);

  // 进入市场、切换插件/连接器范围或重连恢复时，请求当前列表；失败交给静默重试。
  useEffect(() => {
    // 断线（如一键还原重启后端）或客户端声明未就绪时不发请求：此时失败只会把旧快照
    // 留在页面上；连接与声明恢复后本 effect 会重跑，拿到还原后的真实开关状态。
    if (view.name !== 'market' || !connected || !clientContextReady) return;
    void (async () => {
      if (!(await reloadRef.current(false))) scheduleReload();
    })();
  }, [
    view.name,
    activeKind,
    topTab,
    pluginKind,
    connected,
    clientContextReady,
    scheduleReload,
    selectedConnectorSourceId,
    enterpriseEdition,
  ]);

  useEffect(() => webClient.on('extensions.v2.installed', () => scheduleReload()), [scheduleReload]);

  const [refreshNonce, setRefreshNonce] = useState(0);

  // 主动刷新：Hub 目录由后端缓存（TTL 10min），refresh 只触发后台重拉并立刻返回旧快照，
  // 前端沿用既有轮询收敛。
  const handleRefresh = async () => {
    setRefreshNonce((value) => value + 1);
    if (activeKind === 'mcp') {
      const scope = topTab === 'my' ? 'mine' : 'catalog';
      await loadConnectorList(equipmentListFilter('mcp', scope), {
        refresh: true,
        sourceId:
          enterpriseEdition && scope === 'catalog' ? selectedConnectorSourceId : undefined,
      });
    } else if (pluginKind === 'system') await loadSystemCatalog({ refresh: true });
    else await loadPluginList('builtin+hub', { refresh: true });
  };

  // "会话使用"按钮目前没有真实目的地——真正的会话内使用入口是 ChatPanel 输入框的"+"面板
  // （backend-requests.md 需求11/12），不在这次连接器市场的范围内，点击先给个明确提示，
  // 不要让按钮看起来像"点了没反应"的坏按钮。
  const handleUseNotWired = () => window.alert(t('connectorMarket.card.useNotWired'));

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="connector-market-panel">
      {view.name === 'market' && (
        <MarketplacePage
          topTab={topTab}
          onTopTabChange={setTopTab}
          myKind={myKind}
          onMyKindChange={setMyKind}
          pluginKind={pluginKind}
          onPluginKindChange={setPluginKind}
          onOpenConnectorDetail={(name) => setView({ name: 'mcp-detail', connectorName: name })}
          onOpenPluginDetail={(id) => setView({ name: 'plugin-detail', id, fromMy: topTab === 'my' })}
          officeSuites={office.suites}
          onOpenOfficeSuite={(suite) => setView({ name: 'office-suite', suite })}
          onUse={sessionUse ? (onUseExtension ?? handleUseNotWired) : undefined}
          onOpenSystemPluginDetail={(id) => setView({ name: 'system-detail', id })}
          onCreateManual={() => setView({ name: 'create-manual' })}
          // 2026-08-25：onCreateViaChat 改为跳新会话并预填"帮我创建一个xxx插件，擅长xxx"提示词，
          // 同时把 plugin-creator 作为技能 chip 自动选中（App.tsx 派发 jiuwen:new-conversation，
          // 跟 SkillPanel.handleCreateViaChat 的"通过聊天创建技能"是同一条链路，见该文件
          // handleCreateViaChat 注释）——2026-08-20 那版"不带预填、直接跳空白新会话"的决定已被
          // 本次需求取代。不传这个 prop（理论上不会发生，App.tsx 恒传）才退化成提示未接入，跟
          // onUse/handleUseNotWired 同款可选 prop 兜底处理。
          onCreateWithSkill={onCreateViaChat ?? (() => window.alert(t('connectorMarket.create.withSkillNotWired')))}
          onCreateWithUpload={() => {
            setUploadError(null);
            setUploadModalOpen(true);
          }}
          onRegisterCustomMcp={() => setView({ name: 'register-mcp' })}
          onRefresh={handleRefresh}
          refreshNonce={refreshNonce}
          connectorSources={enterpriseEdition ? connectorSources : []}
          selectedConnectorSourceId={selectedConnectorSourceId}
          onConnectorSourceChange={setSelectedConnectorSourceId}
        />
      )}

      {view.name === 'plugin-detail' && (
        <PluginDetailPage
          id={view.id}
          fromMy={view.fromMy}
          onBack={() => setView({ name: 'market' })}
          onDeleted={() => {
            setTopTab('my');
            setMyKind('plugin');
            setView({ name: 'market' });
          }}
          onUse={
            !sessionUse
              ? undefined
              : onUseExtension
                ? (runtimePackageName) => onUseExtension({ kind: 'plugin', id: runtimePackageName })
                : handleUseNotWired
          }
          onUseExample={sessionUse ? onUsePluginExample : undefined}
        />
      )}

      {view.name === 'system-detail' && (
        <SystemPluginDetailPage
          id={view.id}
          onBack={() => setView({ name: 'market' })}
          onDeleted={() => {
            setTopTab('my');
            setMyKind('plugin');
            setPluginKind('system');
            setView({ name: 'market' });
          }}
        />
      )}

      {view.name === 'mcp-detail' && (
        <McpDetailPage
          name={view.connectorName}
          onBack={() => setView({ name: 'market' })}
          onUse={
            !sessionUse
              ? undefined
              : onUseExtension
                ? () => {
                    const connector = useConnectorStore
                      .getState()
                      .connectors.find(
                        (item) => item.id === view.connectorName || item.runtimePackageName === view.connectorName,
                      );
                    onUseExtension({ kind: 'mcp', id: connector?.runtimePackageName ?? view.connectorName });
                  }
                : handleUseNotWired
          }
          onUseExample={sessionUse ? onUseExample : undefined}
          onEdit={() => setView({ name: 'register-mcp', editName: view.connectorName })}
        />
      )}

      {view.name === 'office-suite' && officeStatus && (
        <OfficeSuiteDetail
          suite={view.suite}
          status={officeStatus}
          onStatus={(status) => office.setStatus(view.suite, status)}
          onBack={() => setView({ name: 'market' })}
        />
      )}

      {view.name === 'create-manual' && (
        <CreatePluginPage onBack={() => setView({ name: 'market' })} onCreated={() => setView({ name: 'market' })} />
      )}

      {view.name === 'register-mcp' && (
        <RegisterMcpPage
          editName={view.editName}
          // 编辑态取消/返回：回到刚才那个 MCP 的详情页（编辑本来就是从那进来的）；新建态维持
          // 原来退回市场页的行为。
          onBack={() =>
            setView(view.editName ? { name: 'mcp-detail', connectorName: view.editName } : { name: 'market' })
          }
          // 注册/编辑成功后的落点不一样：编辑态回到该 MCP 详情页（McpDetailPage 挂载时会重新
          // loadDetail，自然显示编辑后的最新配置）；新建态沿用原逻辑——切到"我的扩展 / MCP"
          // 子筛选而不是退回 MCP 广场，理由见下方原注释：新注册的 MCP 是 source==='customize'，
          // 按 MarketplacePage 的过滤规则永远落在"我的MCP"、不会出现在广场，只退回 market 且
          // topTab 还是 'mcp' 的话用户会看到"列表啥也没多出来"，误以为没成功。
          onRegistered={() => {
            if (view.editName) {
              setView({ name: 'mcp-detail', connectorName: view.editName });
              return;
            }
            setTopTab('my');
            setMyKind('mcp');
            setView({ name: 'market' });
          }}
        />
      )}

      {uploadModalOpen && (
        <UploadFileCreateModal
          error={uploadError}
          onCancel={() => {
            setUploadModalOpen(false);
            setUploadError(null);
          }}
          onConfirm={async (filePath) => {
            setUploadError(null);
            const result = await importPackagePath(filePath);
            if (result.ok) {
              setUploadModalOpen(false);
              setSuccessToast(t('connectorMarket.upload.importSuccess'));
              void loadPluginList(equipmentListFilter('plugin', 'mine'));
              return;
            }
            setUploadError(result.error);
          }}
        />
      )}

      {successToast && <Toast message={successToast} variant="success" onClose={() => setSuccessToast(null)} />}
    </div>
  );
}
