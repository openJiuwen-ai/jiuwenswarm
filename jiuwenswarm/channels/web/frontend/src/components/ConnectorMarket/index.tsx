import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useConnectorStore } from '../../stores/connectorStore';
import { usePluginPackageStore } from '../../stores/pluginPackageStore';
import { MarketplacePage, type TopTab, type MarketKind } from './MarketplacePage';
import { PluginDetailPage } from './PluginDetailPage';
import { McpDetailPage } from './McpDetailPage';
import { CreatePluginPage } from './CreatePluginPage';
import { RegisterMcpPage } from './RegisterMcpPage';
import { UploadFileCreateModal } from './UploadFileCreateModal';
import { Toast } from './Toast';
import { equipmentListFilter } from '../../features/equipmentMarketplace';
import { ApplicationPluginsPanel } from '../../applicationPlugins/ApplicationPluginsPanel';
import type { ApplicationPluginContribution } from '../../applicationPlugins/types';

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
  | { name: 'application-plugins' }
  | { name: 'plugin-detail'; id: string; fromMy: boolean }
  | { name: 'mcp-detail'; connectorName: string }
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
  onUseExample?: (text: string, mcpName: string) => void;
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
   * "创建"下拉菜单里的"通过聊天创建"——跳新会话，自动预填"帮我创建一个xxx插件，擅长xxx"提示词
   * 并把 plugin-creator 选中为技能 chip（2026-08-25，取代此前"不带预填"的决定）。跟
   * SkillPanel.handleCreateViaChat（"通过聊天创建技能"）走同一条 jiuwen:new-conversation
   * 事件链路，而不是 onUseExample/onUseExtension 用的 requestSessionNavigation('new', ...)
   * ——因为 plugin-creator 本质是技能，需要 skill chip 机制而不是 initialEnabledPlugins。
   * 不传这个 prop 就退化成原来的"尚未接入"提示（同款可选 prop 处理）。
   */
  onCreateViaChat?: () => void;
  applicationPlugins?: ApplicationPluginContribution[];
  applicationPluginsLoading?: boolean;
  applicationPluginsError?: string;
  onRefreshApplicationPlugins?: () => Promise<void>;
}

export function ConnectorMarketPanel({
  onUseExample,
  onUsePluginExample,
  onUseExtension,
  onCreateViaChat,
  applicationPlugins = [],
  applicationPluginsLoading = false,
  applicationPluginsError = '',
  onRefreshApplicationPlugins = async () => {},
}: ConnectorMarketPanelProps = {}) {
  const { t } = useTranslation();
  const [view, setView] = useState<View>({ name: 'market' });
  const [topTab, setTopTab] = useState<TopTab>(() => (pendingManageView ? 'my' : 'plugin'));
  const [myKind, setMyKind] = useState<MarketKind>(() => {
    const kind = pendingManageView?.myKind ?? 'plugin';
    pendingManageView = null;
    return kind;
  });
  const [uploadModalOpen, setUploadModalOpen] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const loadConnectorList = useConnectorStore((s) => s.loadList);
  const loadPluginList = usePluginPackageStore((s) => s.loadList);
  const importPluginLocal = usePluginPackageStore((s) => s.importLocal);

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
  const pluginError = usePluginPackageStore((s) => s.error);
  const clearPluginError = usePluginPackageStore((s) => s.clearError);
  const pluginNotice = usePluginPackageStore((s) => s.noticeMessage);
  const clearPluginNotice = usePluginPackageStore((s) => s.clearNotice);
  const pluginSuccess = usePluginPackageStore((s) => s.successMessage);
  const clearPluginSuccess = usePluginPackageStore((s) => s.clearSuccess);
  const [errorToast, setErrorToast] = useState<string | null>(null);
  const [successToast, setSuccessToast] = useState<string | null>(null);

  useEffect(() => {
    if (connectorError) {
      setErrorToast(connectorError);
      clearConnectorError();
    }
  }, [connectorError, clearConnectorError]);

  useEffect(() => {
    if (pluginError) {
      setErrorToast(pluginError);
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
  const activeKind: MarketKind = topTab === 'my' ? myKind : topTab;

  // 进入市场或切换插件/连接器范围时，直接请求当前列表。
  useEffect(() => {
    if (view.name !== 'market') return;
    // MCP/插件都按 filter 分别拉"广场"(builtin)/"我的"(local) 两份列表（MCP 见 connectorStore.ts
    // 头注释，2026-08-17 按 MCP 接口文档 v2 改造；插件 2026-08-19 对齐同款做法——之前这里插件分支
    // 固定传 undefined 拿混合列表，靠 MarketplacePage.tsx 前端按 pkg.source 二次过滤，用户反馈
    // "为什么不直接用后端 filter 分开"，两边现在都是"具体拉哪个由 topTab 决定"，不是固定一次调用
    // 能覆盖两个 tab 的。
    const scope = topTab === 'my' ? 'mine' : 'catalog';
    if (activeKind === 'mcp') {
      void loadConnectorList(equipmentListFilter('mcp', scope));
    } else {
      void loadPluginList(equipmentListFilter('plugin', scope));
    }
  }, [activeKind, topTab, view.name, loadConnectorList, loadPluginList]);

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
          onOpenConnectorDetail={(name) => setView({ name: 'mcp-detail', connectorName: name })}
          onOpenPluginDetail={(id) => setView({ name: 'plugin-detail', id, fromMy: topTab === 'my' })}
          onUse={onUseExtension ?? handleUseNotWired}
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
          onOpenApplicationPlugins={() => setView({ name: 'application-plugins' })}
        />
      )}

      {view.name === 'application-plugins' && (
        <ApplicationPluginsPanel
          plugins={applicationPlugins}
          loading={applicationPluginsLoading}
          error={applicationPluginsError}
          onRefresh={onRefreshApplicationPlugins}
          onBack={() => setView({ name: 'market' })}
        />
      )}

      {view.name === 'plugin-detail' && (
        <PluginDetailPage
          id={view.id}
          fromMy={view.fromMy}
          onBack={() => setView({ name: 'market' })}
          onDeleted={() => setView({ name: 'market' })}
          onUse={
            onUseExtension
              ? (runtimePackageName) => onUseExtension({ kind: 'plugin', id: runtimePackageName })
              : handleUseNotWired
          }
          onUseExample={onUsePluginExample}
        />
      )}

      {view.name === 'mcp-detail' && (
        <McpDetailPage
          name={view.connectorName}
          onBack={() => setView({ name: 'market' })}
          onUse={
            onUseExtension
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
          onUseExample={onUseExample}
          onEdit={() => setView({ name: 'register-mcp', editName: view.connectorName })}
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
            // 截图接口里的 session_id 用户明确要求先不带（2026-08-20），见 pluginPackagesApi.ts
            // importLocal 注释。失败在弹窗内展示映射后的中文，与专家上传对齐；不走 store.error
            // Toast，避免和弹窗重复、也避免 Toast 一闪而过用户只看到英文原文。
            setUploadError(null);
            const result = await importPluginLocal({ path: filePath });
            if (result.ok) {
              setUploadModalOpen(false);
              setUploadError(null);
              setSuccessToast(t('connectorMarket.upload.importSuccess'));
              return;
            }
            setUploadError(result.error);
          }}
        />
      )}

      {errorToast && <Toast message={errorToast} variant="error" onClose={() => setErrorToast(null)} />}
      {successToast && <Toast message={successToast} variant="success" onClose={() => setSuccessToast(null)} />}
    </div>
  );
}
