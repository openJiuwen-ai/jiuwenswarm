import { useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { Loader2 } from 'lucide-react';
import { useChatStore, useSessionStore } from '../../stores';
import { useConnectorStore } from '../../stores/connectorStore';
import { usePluginPackageStore } from '../../stores/pluginPackageStore';
import { localizedText } from '../../types/pluginPackage';
import type { ConnectorConnectResponse } from '../../types/connector';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import { PickerPanel } from './PickerPanel';
import { EntityAvatar } from '../ConnectorMarket/EntityAvatar';
import '../ConnectorMarket/ConnectorMarket.css';
import { ConnectTokenModal } from '../ConnectorMarket/ConnectTokenModal';
import { CliAuthModal } from '../ConnectorMarket/CliAuthModal';
import { requestManageView } from '../ConnectorMarket';
import { usePendingConnectorFlow, PendingConnectorModals } from '../ConnectorMarket/usePendingConnectorFlow';
import { Switch } from '../Switch';
import { resolvePluginPickerIdentifiers } from '../../features/equipmentMarketplace';
import { pruneEnabledExtensions } from '../../utils/enabledExtensions';
import PlusIcon from '../../assets/agent-management/agent-plus.svg?react';
import SearchIcon from '../../assets/agent-management/agent-search.svg?react';

// 列表行高：与 ChatPanel.css 里 .chat-extension-picker__item 的 min-height: 44px 保持一致——
// 用来按条目数反推列表的"自然高度"。一次最多同时显示 5 条，超出的靠列表自身 overflow-y:auto
// 内部滚动（用户 2026-08-18 反馈：面板别太长）。行高/封顶行数的计算在 PickerPanel 内完成。
const LIST_ROW_HEIGHT = 44;

interface ExtensionPickerPanelProps {
  onClose: () => void;
  /** 挂到面板根节点——由调用方（InputArea）持有，好在一级"+"菜单自己的 outside-click 判断里
   * 把这个二级面板也算作"菜单内部"，否则点二级面板（搜索框/开关）会被一级菜单
   * 的监听器误判成"点了外面"直接把整个"+"菜单收起。 */
  panelRef: RefObject<HTMLDivElement>;
  /** 一级"+"菜单展开方向：up 时面板与触发项底边齐平向上生长（见 PickerPanel direction） */
  direction?: 'up' | 'down';
}

/**
 * "+"菜单"扩展"项的二级面板：插件/MCP 两个 tab + 搜索 + 列表，已连接/已安装显示会话内开关
 * （切换 sessionStore.enabledPlugins/enabledMcps，不随 chat.send 清空，见该 store 头部注释），
 * 未连接/未安装显示连接icon（点击走 mcp.connect 三态流程或 plugin_packages.install）。
 * 渲染在"+"菜单内部，与智能体/技能面板同款定位（见 ChatPanel.css .chat-agent-picker 规则）。
 */
export function ExtensionPickerPanel({ onClose, panelRef, direction }: ExtensionPickerPanelProps) {
  const { t, i18n } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const enabledPlugins = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.enabledPlugins ?? []);
  const enabledMcps = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.enabledMcps ?? []);
  const [tab, setTab] = useState<'plugin' | 'mcp'>('plugin');
  const [searchQuery, setSearchQuery] = useState('');

  const packages = usePluginPackageStore((s) => s.localPackages);
  const installedMap = usePluginPackageStore((s) => s.installed);
  const pluginConnectionStateMap = usePluginPackageStore((s) => s.connectionStateMap);
  const installPendingMap = usePluginPackageStore((s) => s.installPendingMap);
  const clearInstallPending = usePluginPackageStore((s) => s.clearInstallPending);
  const pluginLoading = usePluginPackageStore((s) => s.isLoading);
  const pluginBusyId = usePluginPackageStore((s) => s.busyId);
  const loadPluginList = usePluginPackageStore((s) => s.loadList);
  const loadPluginDetail = usePluginPackageStore((s) => s.loadDetail);
  const installPlugin = usePluginPackageStore((s) => s.install);
  // 当前正在走连接续跑（安装或重连）的插件 id——用 ref 不用 state，原因同 PluginDetailPage.tsx
  // 头注释：handleConnectPlugin/handleReconnectPlugin 里"记下 id"和"起串行连接"是同一个事件
  // 处理函数内的同步调用，若用 state，串行连接内部的收尾闭包会读到这次渲染里还没生效的旧值。
  const connectingPluginIdRef = useRef<string | null>(null);

  const myConnectors = useConnectorStore((s) => s.myConnectors);
  const connectorLoading = useConnectorStore((s) => s.isLoading);
  const busyMap = useConnectorStore((s) => s.busyMap);
  const loadConnectorList = useConnectorStore((s) => s.loadList);
  const connectMcp = useConnectorStore((s) => s.connect);

  const [tokenTarget, setTokenTarget] = useState<{ name: string; response: ConnectorConnectResponse } | null>(null);
  const [authTarget, setAuthTarget] = useState<{ name: string; response: ConnectorConnectResponse } | null>(null);

  // 插件"首次安装"（§1.6.3）：install() 半途失败会把 pending_connectors 记进
  // installPendingMap（见 pluginPackageStore.ts），下面的 effect 侦测到就自动起串行连接续跑；
  // 全部连完幂等重试 install。
  const pluginInstallFlow = usePendingConnectorFlow(
    () => {
      const id = connectingPluginIdRef.current;
      connectingPluginIdRef.current = null;
      if (id) {
        clearInstallPending(id);
        void installPlugin(id);
      }
    },
    () => {
      // 依赖 connector 自动连接失败/被取消：清 ref + 清 pending 名单，否则下面这个 effect 会
      // 反复重启连接续跑。安装到此终止（2026-08-31 修死循环，见 usePendingConnectorFlow 头注释）。
      const id = connectingPluginIdRef.current;
      connectingPluginIdRef.current = null;
      if (id) clearInstallPending(id);
    },
  );
  useEffect(() => {
    const id = connectingPluginIdRef.current;
    const pending = id ? installPendingMap[id] : undefined;
    if (id && pending && pending.length > 0 && !pluginInstallFlow.active) {
      pluginInstallFlow.start(pending);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [installPendingMap]);

  // 插件"已装重连"（§1.6.4）：面板列表只有 list 汇总数据，没有 pending_connectors（那是仅
  // show 才有的字段），点击时先懒加载一次详情拿名单，再连，全程不调 install。
  const pluginReconnectFlow = usePendingConnectorFlow(() => {
    const id = connectingPluginIdRef.current;
    connectingPluginIdRef.current = null;
    if (id) void loadPluginDetail(id);
  });

  // 面板每次打开都重新拉一遍 local 数据（不再是"只拉一次，之后开都用旧缓存"）——2026-08-18
  // 用户明确要求：+号点开后台重新获取，但界面先用已有数据顶着，等新数据回来再替换渲染，不要在
  // 重新获取期间/失败时把已有列表清空。这个开发环境的网关偶发几十秒的整体卡顿（不是 mcp.list
  // 本身慢，是连接上所有待处理请求一起被压住，见 progress.md 2026-08-18 记录的实测数据：请求
  // 55 秒后才收到响应，此时前端 15 秒客户端超时早已把列表重置成空，真正的数据到达时已经找不到
  // 对应的 pending 请求、被 webClient 静默丢弃），此前"超时/失败就整份清空成 []"的兜底策略在
  // 这种环境下反而制造了"数据明明回来了、界面却是空的"的假象——传 `{silent:true}` 就能避免：
  // silent 模式失败时直接保留旧列表不清空（见 pluginPackageStore.ts/connectorStore.ts 里
  // loadList 的既有实现），成功时仍然照常替换渲染。首次打开（列表还是空的，没有"旧数据"可顶）
  // 才用非静默调用走正常的 loading 态，避免用户盯着一片空白却看不出到底是"真没数据"还是"正在
  // 加载中"。
  useEffect(() => {
    void loadPluginList('mine', { silent: packages.length > 0 });
    void loadConnectorList('local', { silent: myConnectors.length > 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 2026-08-21 用户明确要求：不止 chat.send 发送前兜底一次（见 useWebSocket.ts），面板每次拿到
  // 最新的插件/MCP连接态后也要顺手核对一遍当前会话已启用的开关是否还真的连接着——断连/被卸载的
  // 项直接摘掉，让开关同步变回关闭，不用等到真正发消息那一刻才被静默过滤掉。两处共用同一个
  // pruneEnabledExtensions（见该文件头注释：不做"断连/卸载那一刻全局清理所有会话"的源头治理，
  // 只做这两处兜底）。
  useEffect(() => {
    if (!activeSessionId) return;
    pruneEnabledExtensions(activeSessionId);
  }, [activeSessionId, installedMap, pluginConnectionStateMap, myConnectors]);

  useEffect(() => {
    // 授权/连接弹窗（ConnectTokenModal/CliAuthModal，含 pluginInstallFlow/pluginReconnectFlow
    // 串行续跑的那两份）是单独 createPortal 到 document.body 的兄弟节点，不是 panelRef 的子
    // 节点——靠 data-connector-auth-modal 属性（见两个弹窗组件头注释）识别"点的是弹窗内部"，
    // 跳过关闭，弹窗只由它自己的取消/关闭按钮控制。不能靠 tokenTarget/authTarget 等 state 判断
    // ——那样只覆盖了本组件直接持有的这份 state，pluginInstallFlow/pluginReconnectFlow 内部的
    // tokenTarget/authTarget 是 usePendingConnectorFlow 钩子私有的，这里拿不到，还是会漏（
    // 2026-08-25 用户反馈：点连接弹窗任何地方，整个"+"扩展下拉框直接退出，弹窗跟着一起消失）。
    const handlePointerDown = (event: PointerEvent) => {
      if ((event.target as HTMLElement | null)?.closest?.('[data-connector-auth-modal]')) return;
      if (!panelRef.current?.contains(event.target as Node)) onClose();
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [onClose]);

  const filteredPlugins = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return packages;
    return packages.filter((p) => localizedText(p.displayName, i18n.language).toLowerCase().includes(q));
  }, [packages, searchQuery, i18n.language]);

  const filteredMcps = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return myConnectors;
    return myConnectors.filter((c) => c.displayName.toLowerCase().includes(q));
  }, [myConnectors, searchQuery]);

  const { tooltip, handlers: tooltipHandlers } = useAdaptiveTooltip({ offsetX: -50 });

  // 插件/MCP 两个 tab 共用同一个列表高度——itemCount 取两边条目数的较大值，由 PickerPanel 按
  // "行高 × 条目数、封顶 5 行"反推显式 height：条目少的一侧在列表内部留白撑到同样高度，超过
  // 5 条的一侧靠列表自身的 overflow-y:auto 内部滚动，不会把面板撑爆、挤出面板边框（2026-08-18
  // 用户反馈：最后一条 MCP 顶出弹出框外——根因是之前用 minHeight 只兜底"不小于"，条目一多没有
  // 封顶）。两个 tab 都没数据时 itemCount 是 0，PickerPanel 不写显式高度，退回空态自然撑满。

  function handleTogglePlugin(id: string) {
    const sid = useChatStore.getState().activeSessionId;
    if (!sid) return;
    const store = useSessionStore.getState();
    if (enabledPlugins.includes(id)) store.removeEnabledPlugin(sid, id);
    else store.addEnabledPlugin(sid, id);
  }

  function handleToggleMcp(name: string) {
    const sid = useChatStore.getState().activeSessionId;
    if (!sid) return;
    const store = useSessionStore.getState();
    if (enabledMcps.includes(name)) store.removeEnabledMcp(sid, name);
    else store.addEnabledMcp(sid, name);
  }

  // 未安装点连接icon走 §1.6.3；installPlugin 内部失败带 pending_connectors 时不会 throw，
  // 只是把名单写进 store（见 pluginPackageStore.ts install 注释），上面的 effect 侦测到会自动
  // 接上串行连接续跑。
  async function handleConnectPlugin(id: string) {
    connectingPluginIdRef.current = id;
    await installPlugin(id);
  }

  // 已安装但依赖 connector 未就绪时点连接icon走 §1.6.4：先懒加载详情拿 pendingConnectors
  // （面板的 list 汇总数据没有这个字段），再串行连，全程不调 install。
  async function handleReconnectPlugin(id: string) {
    connectingPluginIdRef.current = id;
    const ok = await loadPluginDetail(id);
    if (!ok) {
      connectingPluginIdRef.current = null;
      return;
    }
    const detail = usePluginPackageStore.getState().detailCache[id];
    pluginReconnectFlow.start(detail?.pendingConnectors ?? []);
  }

  async function handleConnectMcp(name: string) {
    const response = await connectMcp(name);
    if (!response) return;
    if (response.credentialsRequired) {
      setTokenTarget({ name, response });
    } else if (response.type === 'auth_required') {
      setAuthTarget({ name, response });
    }
    // type === 'connected'：store 已经在 connect() 内部把 connectionState patch 成 connected，
    // 这里不需要额外处理，列表项会随订阅自动从"连接icon"重渲染成"开关"。
  }

  function handleManageClick() {
    onClose();
    requestManageView(tab);
  }

  const loading = tab === 'plugin' ? pluginLoading : connectorLoading;
  const tokenTargetConnector = tokenTarget ? myConnectors.find((c) => c.name === tokenTarget.name) : undefined;

  return (
    <>
      <PickerPanel
        panelRef={panelRef}
        className="chat-extension-picker"
        direction={direction}
        testId="chat-panel-extension-picker-panel"
        rowHeight={LIST_ROW_HEIGHT}
        itemCount={Math.max(filteredPlugins.length, filteredMcps.length)}
        tabs={
          <div className="chat-picker-panel__tabs" role="tablist">
            {(['plugin', 'mcp'] as const).map((key) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={tab === key}
                onClick={() => setTab(key)}
                className={clsx(tab === key && 'is-active')}
                data-testid="chat-panel-extension-picker-tab"
                data-variant={key}
              >
                {t(key === 'plugin' ? 'connectorMarket.tabs.plugin' : 'connectorMarket.tabs.mcp')}
              </button>
            ))}
          </div>
        }
        search={
          <div className="chat-picker-panel__search">
            <div className="chat-picker-panel__search-inner">
              <SearchIcon aria-hidden="true" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder={t('chat.extensionSearchPlaceholder')}
                data-testid="chat-panel-extension-picker-search-input"
              />
            </div>
          </div>
        }
        footer={{ label: t('chat.extensionMore'), onClick: handleManageClick }}
      >
        {loading && <div className="chat-skill-select__state" data-testid="chat-panel-extension-picker-state" data-variant="loading">{t('skills.detailLoading')}</div>}

        {!loading && tab === 'plugin' && filteredPlugins.length === 0 && (
          <div className="chat-skill-select__state" data-testid="chat-panel-extension-picker-state" data-variant="no-plugin">
            {t('chat.extensionEmpty')}
          </div>
        )}
        {!loading && tab === 'plugin' && filteredPlugins.map((pkg) => {
          const { marketplaceId, sessionPluginName } = resolvePluginPickerIdentifiers(pkg);
          const label = localizedText(pkg.displayName, i18n.language);
          const desc = localizedText(pkg.displayDescription, i18n.language);
          const avatar = getSkillAvatar(label);
          const installed = installedMap[marketplaceId];
          const linked = (pluginConnectionStateMap[marketplaceId] ?? 'disconnected') === 'connected';
          const isEnabled = enabledPlugins.includes(sessionPluginName);
          const isConnectingThis = connectingPluginIdRef.current === marketplaceId && (pluginInstallFlow.active || pluginReconnectFlow.active);
          const busy = pluginBusyId === marketplaceId || isConnectingThis;
          return (
            <div
              key={marketplaceId}
              className="chat-skill-select__item chat-extension-picker__item"
              data-testid="chat-panel-extension-picker-item"
              data-variant={marketplaceId}
            >
              <div className={`chat-skill-select__avatar ${avatar.color}`}>
                {avatar.firstChar}
              </div>
              <ItemDescCell text={desc} handlers={tooltipHandlers}>
                <div className="chat-skill-select__item-name">{label}</div>
              </ItemDescCell>
              {busy ? (
                <Loader2 className="chat-extension-picker__spinner" size={16} />
              ) : installed && linked ? (
                <Switch checked={isEnabled} onChange={() => handleTogglePlugin(sessionPluginName)} />
              ) : installed ? (
                <ConnectButton label={t('chat.extensionConnect')} handlers={tooltipHandlers} onClick={() => void handleReconnectPlugin(marketplaceId)} />
              ) : (
                <ConnectButton label={t('chat.extensionConnect')} handlers={tooltipHandlers} onClick={() => void handleConnectPlugin(marketplaceId)} />
              )}
            </div>
          );
        })}

        {!loading && tab === 'mcp' && filteredMcps.length === 0 && (
          <div className="chat-skill-select__state" data-testid="chat-panel-extension-picker-state" data-variant="no-mcp">
            {t('chat.extensionEmpty')}
          </div>
        )}
        {!loading && tab === 'mcp' && filteredMcps.map((connector) => {
          const avatar = getSkillAvatar(connector.displayName);
          const linked = connector.connectionState === 'connected';
          const isEnabled = enabledMcps.includes(connector.name);
          const busy = Boolean(busyMap[connector.name]);
          return (
            <div key={connector.name} className="chat-skill-select__item chat-extension-picker__item" data-testid="chat-panel-extension-picker-item" data-variant={connector.name}>
              <EntityAvatar
                iconUrl={connector.icon ?? undefined}
                avatar={avatar}
                className="chat-skill-select__avatar"
              />
              <ItemDescCell text={connector.description ?? undefined} handlers={tooltipHandlers}>
                <div className="chat-skill-select__item-name">{connector.displayName}</div>
              </ItemDescCell>
              {busy ? (
                <Loader2 className="chat-extension-picker__spinner" size={16} />
              ) : linked ? (
                <Switch checked={isEnabled} onChange={() => handleToggleMcp(connector.name)} />
              ) : (
                <ConnectButton label={t('chat.extensionConnect')} handlers={tooltipHandlers} onClick={() => void handleConnectMcp(connector.name)} />
              )}
            </div>
          );
        })}
      </PickerPanel>

      {tokenTarget && (
        <ConnectTokenModal
          name={tokenTarget.name}
          displayName={tokenTargetConnector?.displayName ?? tokenTarget.name}
          iconUrl={tokenTargetConnector?.icon ?? undefined}
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
      {/* 插件"首次安装"/"已装重连"（§1.6.3/§1.6.4）连接续跑的弹窗；跟上面 tokenTarget/authTarget
          是两套独立状态——那两个只服务 MCP tab 的单个 mcp.connect，这里服务插件 tab 可能要串行
          连多个 pending connector。互斥（同一时刻只会有一个在跑），各自按 active 独立渲染。 */}
      <PendingConnectorModals flow={pluginInstallFlow} />
      <PendingConnectorModals flow={pluginReconnectFlow} />
      {tooltip}
    </>
  );
}

type TooltipHandlers = {
  onMouseEnter: (event: { currentTarget: EventTarget | null }) => void;
  onMouseLeave: () => void;
  onFocus: (event: { currentTarget: EventTarget | null }) => void;
  onBlur: () => void;
};

function ItemDescCell({ text, handlers, children }: { text?: string; children: React.ReactNode; handlers: TooltipHandlers }) {
  return (
    <div
      className="chat-skill-select__item-main"
      data-tooltip={text}
      {...handlers}
    >
      {children}
    </div>
  );
}

function ConnectButton({ label, handlers, onClick }: { label: string; onClick: () => void; handlers: TooltipHandlers }) {
  return (
    <button
      type="button"
      className="connector-market-icon-btn flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-bg-muted/75 text-[color:var(--color-text-placeholder)] transition-colors hover:bg-connector-add-hover-surface hover:text-[color:var(--color-chat-accent)]"
      aria-label={label}
      data-tooltip={label}
      {...handlers}
      onClick={onClick}
    >
      <PlusIcon aria-hidden="true" />
    </button>
  );
}
