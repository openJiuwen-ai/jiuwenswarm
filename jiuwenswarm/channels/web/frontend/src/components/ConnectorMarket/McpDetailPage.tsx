import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Unlink2, Trash2, Plus, Wrench, Terminal, Loader2, AlertCircle, X, ExternalLink, Pencil } from 'lucide-react';
// 2026-08-17：Trash2（原"卸载"按钮图标，按 source 分流 delete/disconnect）曾随彻底删除入口一起
// 移除。2026-08-19 用户明确要求恢复：自定义 MCP 断联态（已经解绑过一次）的按钮要变成真正的
// "卸载"（彻底删除，见 mcp.delete_custom），配图标也要换成垃圾桶——Unlink2 是"解绑"语义的图标，
// 用在"删除"上不对，见下方按钮渲染处的 icon 条件。
// 跟左侧导航栏"技能"入口用同一个图标（SessionSidebar/index.tsx 的 nav.skills），而不是随手挑一个
// lucide 图标——用户明确要求技能展示区的图标要跟左侧栏"技能"视觉统一。
import SkillIcon from '../../assets/agent-management/agent-skill.svg?react';
import { useConnectorStore } from '../../stores/connectorStore';
import { NewConversationIcon } from './icons';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { EntityAvatar } from './EntityAvatar';
import { ConnectTokenModal } from './ConnectTokenModal';
import { CliAuthModal } from './CliAuthModal';
import { ConfirmDialog } from './ConfirmDialog';
import { PillButton, DetailLinkButton } from './Buttons';
import { deriveCardState, deriveMcpAvailability } from './mcpState';
import type { ConnectorConnectResponse, ConnectorIntegrationType } from '../../types/connector';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';

// integrationType 决定了这个 MCP 的接入方式（要不要走 CLI OAuth、有没有"连接"按钮），之前
// mcp.show 下发了这个字段但详情页完全没展示，用户看不出"为什么这个要跳浏览器授权""为什么那个
// 没有连接按钮"。只在这一个组件用，不抽到 mcpState.ts 那种跨组件共用的派生模块里。
function integrationTypeLabelKey(type: ConnectorIntegrationType): string {
  switch (type) {
    case 'cli':
      return 'connectorMarket.detail.integrationType.cli';
    case 'remote-mcp':
      return 'connectorMarket.detail.integrationType.remoteMcp';
    case 'skill-only':
      return 'connectorMarket.detail.integrationType.skillOnly';
    case 'stdio-mcp':
    default:
      return 'connectorMarket.detail.integrationType.stdioMcp';
  }
}

interface McpDetailPageProps {
  name: string;
  onBack: () => void;
  /** "会话使用"点击——真正入口是 ChatPanel 输入框的"+"面板，这里没有真实目的地。 */
  onUse?: () => void;
  /**
   * 点"试试这样用"下面的某个示例——真正跳转新建会话并把示例文案填进输入框，复用
   * App.tsx enterNewConversation 的 initialInputValue 机制（CronPanel「通过对话创建」
   * onCreateViaChat 就是这条路径，见 App.tsx:2431）。不传就退化成不可点击的纯展示 chip
   * （跟 onUse 一样，没有目的地就别装作能点）。
   *
   * 第二个参数 mcpName 是这个示例所属的 MCP 名字——2026-08-18 用户反馈：点示例能带上文案跳过去，
   * 但会话里没有自动选中这个 MCP，示例实际用不了。带上 mcpName 让 App.tsx 能同时把
   * initialEnabledMcps 一起传给 requestSessionNavigation（跟 initialInputValue 走的是同一条
   * NewConversationOptions 通道，两者本来就能同时带，只是这条调用之前没传第二个字段）。
   */
  onUseExample?: (text: string, mcpName: string) => void;
  /**
   * "编辑"按钮——只对 connector.source==='customize' 展示（见下方 JSX 门控），真正的编辑表单
   * （RegisterMcpPage 复用，见该文件 editName prop）由父容器（ConnectorMarket/index.tsx）承接
   * 导航，这里只负责在点击时通知父层"要编辑我"。built_in 的 MCP 不可编辑，不传这个 prop 时按钮
   * 也不会渲染（跟 onUse/onUseExample 同款"没有目的地就不渲染"的可选 prop 处理）。
   */
  onEdit?: () => void;
}

// MCP 详情和插件详情结构不完全一样：只有"基本信息+工具列表"两组，没有技能/Rail/MCP 三组分类
// （那是插件包 plugin_packages.show 特有的字段）。
//
// 2026-08-15 按钮矩阵再整改（去除全局启用/禁用，见
// state-model-rectification-v2-remove-global-toggle.md）：原来的"解绑"（广场,可逆,需确认）和
// "删除"（我的,不可逆,需确认）两个按钮合并成一个统一的"卸载"按钮。
//
// 2026-08-17 按按钮矩阵 v2 最终态再改：详情页右上角"已安装+已连接"态只剩两个按钮——"解绑"和
// "会话使用"。"解绑"对预置和自定义 MCP 一视同仁，统一走 mcp.disconnect（断开连接，清 CredentialStore
// token，保留定义可重连，文档 §5.5），不再按 connector.source 分流到 mcp.delete_custom。
// 彻底删除（mcp.delete_custom）的 UI 入口整个移除——自定义 MCP 要彻底删，就先解绑（断连后仍在
// 列表里、可重连），不再有"一键不可逆删除"的路径。原 handleUninstall 里的 deleteConnector 分支
// 由此变成死代码，连同 store.deleteConnector action、connectorApi.deleteCustom、McpBusyKind 的
// 'delete' 取值、confirmUninstallCustomMcp 文案一起删除。
//
// "解绑"弹框确认后停留在详情页（不再 onBack 跳回列表）——disconnect 成功后 store 把
// connectionState 翻成 disconnected，详情页自动重渲染成"已安装+未连接"断联 banner + 红色"连接MCP"
// 按钮态（见下方 `installed && !linked` 那段 JSX），用户当场就能看到结果、直接重连。
//
// built_in 没有独立于 connection_state 的"是否装过"标记（deriveMcpAvailability 的已知限制，见
// mcpState.ts 头注释），实测过 mcp.show 对"从没连过"和"连过又解绑"两种预置 MCP 返回的数据完全
// 一样——所以预置 MCP 解绑后这里自然渲染成的是和"从没装过"一样的"未安装"态（安装按钮）。这是当前
// 数据模型下 source==='built_in' 分支的真实状态，用户已确认接受，**不额外加 source 判断特殊处理**
// （不做"预置解绑就退回列表"这种分流，交给下面的派生逻辑统一渲染，跟 customize 走同一套代码）。
//
// 2026-08-17 之前这里是按 fromMy（进入路径：广场/我的）门控的，因为那时"我的MCP"恒等于
// customize，fromMy 和 source 是等价判据；按 MCP 接口文档 v2 改造后"我的MCP"也会展示已连接的
// 预置 MCP（见 connectorStore.ts 头注释），fromMy 不再等价于 source，入口路径（fromMy）不再需要
// 作为 prop 传入。
//
// 详情页可达性也变了：新方案要求"已安装+未连接"这个中间态也能打开详情页（展示断联 banner），
// 不再是旧版"不是 connected 就整个拒绝进入"，见 mcpState.ts deriveMcpAvailability。
export function McpDetailPage({ name, onBack, onUse, onUseExample, onEdit }: McpDetailPageProps) {
  const { t } = useTranslation();
  const connector = useConnectorStore((s) => s.connectors.find((c) => c.name === name));
  const detail = useConnectorStore((s) => s.detailCache[name]);
  const tools = detail?.tools;
  // mcp.show 一次性带回的预置技能（name+description），之前只用了同批返回的 tools，这份完全
  // 没消费出口——插件详情页（PluginDetailPage.tsx）有对称的"技能"分区，MCP 这边照抄视觉补上。
  const skills = detail?.skills;
  const loadDetail = useConnectorStore((s) => s.loadDetail);
  const connectAction = useConnectorStore((s) => s.connect);
  const disconnectAction = useConnectorStore((s) => s.disconnect);
  const deleteConnectorAction = useConnectorStore((s) => s.deleteConnector);
  const busyMap = useConnectorStore((s) => s.busyMap);
  const [installing, setInstalling] = useState(false);
  const [tokenTarget, setTokenTarget] = useState<ConnectorConnectResponse | null>(null);
  const [authTarget, setAuthTarget] = useState<ConnectorConnectResponse | null>(null);
  const [confirmUnbind, setConfirmUnbind] = useState(false);
  const [busy, setBusy] = useState(false);

  // 卡片统一状态机（见 mcpState.ts）。busy 用 busyMap[name]。
  const cardState = connector
    ? deriveCardState({ connectionState: connector.connectionState, busy: busyMap[name] })
    : 'idle';
  // 详情页可达性：installed=能不能看到这个详情页（"已安装"，含"已安装但未连接"这个中间态），
  // linked=是否已连接（決定"会话使用"能不能点、要不要展示断联 banner）。见 mcpState.ts
  // deriveMcpAvailability 的详细说明。
  const { installed, linked } = connector ? deriveMcpAvailability(connector.source, cardState) : { installed: false, linked: false };

  // 2026-08-19 修根因：之前这个 effect 只依赖 [name, loadDetail]，"连接MCP"成功后
  // connectorStore.connect() 会 invalidateDetail(name) 清掉这份缓存（这一步本身是对的——连接后
  // 工具列表理应刷新），但停留在同一个详情页时 name 没变、loadDetail 这个函数引用也没变，这个
  // effect 根本不会重新跑，导致缓存清空后没人再去重新拉，工具/技能区块就那么空着，直到用户退出
  // 详情页重新进（组件重新挂载，effect 才会再跑一次）。现在把 detail 加进依赖——只要
  // detailCache[name] 变成 undefined（不管是最初挂载时没缓存，还是被 connect/disconnect 之类的
  // 操作清空），这个 effect 就会重新触发去重新拉，不用等重新挂载。
  useEffect(() => {
    // mcp.show 一次性带回 skills+tools（见文档 §5.2）；loadDetail 内部已经按 name 缓存，
    // detailCache[name] 有值时这里的调用会被它自己的守卫短路，不会产生多余请求。
    if (!detail) {
      loadDetail(name);
    }
  }, [name, detail, loadDetail]);

  if (!connector) return null;

  const avatar = getSkillAvatar(connector.displayName);
  // 自定义 MCP 一旦断联（已经解绑过一次），右上角那个按钮的语义从"解绑"变成"卸载"（彻底删除，
  // mcp.delete_custom）——用普通变量而不是每处都重新读 connector.source/!linked，也顺便避开
  // 闭包里 TS 认不出 `connector` 已经非空narrow 的问题（handleUnbind/handleDelete 定义在下面，
  // 是嵌套函数，TS 不会把上面 `if (!connector) return null` 的窄化带进闭包）。
  const isCustomize = connector.source === 'customize';
  const isDeleteMode = isCustomize && !linked;

  async function handleInstall() {
    setInstalling(true);
    const response = await connectAction(name);
    setInstalling(false);
    if (!response) return;
    if (response.credentialsRequired) {
      setTokenTarget(response);
    } else if (response.type === 'auth_required') {
      setAuthTarget(response);
    }
  }

  // "使用"：只在已连接（linked）时可点（按钮本身按这个条件 disabled，见下方 JSX），直接跳转。
  // 2026-08-19 之前这里还有"未连接时点击自动先连接再跳转"的分支（配合 pendingUseAfterConnect
  // 标记），但用户明确要求断联态下"使用"整个置灰不可点，那条分支连同标记状态一起整个删除——
  // 不再有能触发它的入口，留着就是死代码。
  function handleUse() {
    onUse?.();
  }

  // "解绑"：对所有 MCP（预置/自定义）统一走 mcp.disconnect（清 CredentialStore token，保留
  // 定义可重连，文档 §5.5）。这个按钮只在"连接态点解绑"这一次会走这个函数——自定义 MCP 一旦
  // 断联，同一个按钮位置会切成 handleDelete（见下方 isDeleteMode），不会再回到 handleUnbind。
  // 2026-08-19 用户明确要求解绑后的落点按 source 分流：
  // - built_in（广场）：直接 onBack 回到进入前的列表页（不管是从"MCP广场"还是"我的扩展-MCP"进来，
  //   onBack 本来就是父容器传入的"回到来源列表"回调）。
  // - customize（我的）：不跳转，停留在详情页——disconnect 成功后 store 把 connectionState 翻成
  //   disconnected，下面 `installed && !linked` 那段断联 banner 会自动渲染出来，用户当场看到结果、
  //   直接重连、编辑，或者卸载（真删除，见 handleDelete）。
  async function handleUnbind() {
    setBusy(true);
    await disconnectAction(name);
    setBusy(false);
    setConfirmUnbind(false);
    if (!isCustomize) {
      onBack();
    }
  }

  // "卸载"：只在自定义 MCP 已经断联的状态下出现（isDeleteMode），走 mcp.delete_custom 彻底删除
  // 配置/凭证/技能（文档 §5.7），不是"解绑"的同义词。2026-08-19 用户明确指出之前这个按钮虽然
  // 文案改了但底层还是调 disconnect（对已断联的 MCP 是个空操作），是真实 bug——删除成功后这个
  // MCP 在 store 里已经被摘除（deleteConnector 内部处理），详情页不能再停留在一个不存在的实体
  // 上，必须 onBack。
  async function handleDelete() {
    setBusy(true);
    const ok = await deleteConnectorAction(name);
    setBusy(false);
    setConfirmUnbind(false);
    if (ok) {
      onBack();
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <button
        type="button"
        onClick={onBack}
        className="detail-back mb-[35px]"
      >
        <BackIcon aria-hidden="true" />
        {t('connectorMarket.common.back')}
      </button>

      <div className="detail-body relative flex-1 min-h-0 overflow-y-auto pb-6">

      <div className="mb-6 flex items-start justify-between">
        <div className="flex items-center gap-3">
          <EntityAvatar
            iconUrl={connector.icon ?? undefined}
            avatar={avatar}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-[16px] font-semibold"
          />
          <h1 className="text-[18px] font-semibold leading-7 text-text">{connector.displayName}</h1>
          <span
            data-tooltip={detail?.cliSpecPresent ? t('connectorMarket.detail.cliSpecPresentHint') : undefined}
            className="flex items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[11px] leading-4 text-text-muted"
          >
            {detail?.cliSpecPresent && <Terminal size={11} />}
            {t(integrationTypeLabelKey(connector.integrationType))}
          </span>
          {cardState === 'error' && (
            <span className="flex items-center gap-1 text-[12px] text-danger">
              <AlertCircle size={14} />
              {t('connectorMarket.card.stateError')}
            </span>
          )}
        </div>

        <div className="flex items-center gap-3">
          {/* 自定义 MCP 才能编辑（source==='customize'，built_in 没有可改的连接配置）——放在
              解绑左边，和"卸载/解绑的左边一个小编辑按键"的产品要求对齐。 */}
          {isCustomize && onEdit && (
            <DetailLinkButton icon={<Pencil size={14} />} label={t('connectorMarket.card.edit')} onClick={onEdit} disabled={busy} />
          )}
          {installed && (
            // 2026-08-19 用户明确要求：自定义 MCP 断联态（isDeleteMode，即已经解绑过一次）这个
            // 按钮不只是文案变"卸载"，点击后要走真删除（handleDelete/mcp.delete_custom），图标也
            // 要换成垃圾桶 Trash2——Unlink2 是"解绑"语义，用在"删除"上不对。只在 isDeleteMode 时
            // 切换，built_in 走 error 态落进 installed&&!linked 时（"从没连上过"，不是"卸载"
            // 语境）维持原来的解绑图标/文案/行为。
            <DetailLinkButton
              icon={isDeleteMode ? <Trash2 size={14} /> : <Unlink2 size={14} />}
              label={isDeleteMode ? t('connectorMarket.card.uninstall') : t('connectorMarket.card.unbind')}
              onClick={() => setConfirmUnbind(true)}
              danger
              disabled={busy}
            />
          )}
          {installed && (
            // 2026-08-19 用户明确要求：断联态（installed && !linked）"使用"按钮要置灰不可点——
            // 之前只在 installing 时禁用，未连接时点击会静默触发一次后台连接（handleUse 里
            // linked===false 分支），容易让用户误以为按钮坏了或者不清楚点了发生了什么。
            <button
              type="button"
              onClick={handleUse}
              disabled={installing || !linked}
              // disabled:hover:text-text 优先级比裸 hover: 高（多一层 :disabled 伪类，选择器更
              // 精确），专门用来盖掉禁用态下鼠标悬停仍然变蓝的问题——原生 disabled 属性不保证
              // 阻止 :hover 伪类生效，具体行为跟浏览器有关，不能只靠 disabled 属性本身。
              className="flex items-center gap-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)] disabled:cursor-not-allowed disabled:opacity-60 disabled:hover:text-text"
            >
              <NewConversationIcon size={14} />
              {t('connectorMarket.card.use')}
            </button>
          )}
          {!installed && !installing && (
            <PillButton icon={<Plus size={14} />} label={cardState === 'error' ? t('connectorMarket.card.retry') : t('connectorMarket.card.install')} onClick={handleInstall} />
          )}
          {installing && (
            <PillButton icon={<Loader2 size={14} className="animate-spin" />} label={t('connectorMarket.card.installing')} disabled />
          )}
        </div>
      </div>

      {/* "已安装+未连接"断联提示——新方案要求这个中间态在详情页左上角图标名称下方展示一行提示，
          "连接MCP"按钮复用 handleInstall（和顶部安装按钮走同一个 connect 调用）。
          2026-08-19 用户明确要求的视觉调整：整行加浅红底（精确色值 #FCE3E1，不用现有
          danger-subtle token——那个是 10% 透明度红叠加在卡片背景上换算出来的颜色，跟用户给的
          实际色值有肉眼可辨的偏差，这里按用户给的精确值来，不凑近似）；前面的断联图标从线框
          Unlink 换成"红圆底+白色X"这种更常见的错误态图标（用 bg-danger 圆形 + 白色 X 手搭，
          lucide 没有现成的实心圆+X 组合图标）；"连接MCP"文字从红色改成蓝色（用跟全局一致的
          accent 蓝 token）。 */}
      {installed && !linked && (
        <div className="mb-6 flex items-center gap-1.5 rounded-lg bg-[#FCE3E1] px-3 py-2 text-[13px] text-text-muted">
          <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full bg-danger">
            <X size={9} strokeWidth={3} className="text-text-inverse" />
          </span>
          <span>{t('connectorMarket.detail.mcpDisconnectedBannerSelf')}</span>
          <button
            type="button"
            onClick={handleInstall}
            disabled={installing}
            className="flex items-center gap-0.5 font-medium text-[color:var(--color-chat-accent)] hover:opacity-80 disabled:opacity-60"
          >
            {t('connectorMarket.detail.connectMcp')}
            <ExternalLink size={12} />
          </button>
        </div>
      )}

      {confirmUnbind && (
        // 2026-08-19：isDeleteMode 时这个弹窗要换成"彻底删除"的文案/确认动作（mcp.delete_
        // custom，走 handleDelete），不能沿用 disconnect 那套"保留定义可重连"的措辞——两个操作
        // 后端语义完全不同，混着说会误导用户以为删除也能重连。
        <ConfirmDialog
          title={
            isDeleteMode
              ? t('connectorMarket.confirmDeleteMcp.title', { name: connector.displayName })
              : t('connectorMarket.confirmUnbind.title', { name: connector.displayName })
          }
          message={
            isDeleteMode
              ? t('connectorMarket.confirmDeleteMcp.message')
              : t('connectorMarket.confirmUnbind.message')
          }
          confirmLabel={isDeleteMode ? t('connectorMarket.card.uninstall') : t('connectorMarket.card.unbind')}
          onCancel={() => setConfirmUnbind(false)}
          onConfirm={isDeleteMode ? handleDelete : handleUnbind}
        />
      )}

      <div className="mb-6">
        <h2 className="mb-3 text-[14px] font-semibold leading-[22px] text-text">{t('connectorMarket.detail.sections.basicInfo')}</h2>
        <p className="text-[12px] leading-[18px] text-text">{detail?.description ?? ''}</p>
      </div>

      {detail?.examples && detail.examples.length > 0 && (
        <div className="mb-6">
          <h2 className="mb-3 text-[14px] font-semibold leading-[22px] text-text">{t('connectorMarket.detail.sections.examples')}</h2>
          <div className="flex flex-wrap gap-2">
            {/* 未连接/连接失败态下，这个 MCP 在会话里根本用不了（跟顶部"会话使用"按钮的 linked
                门控是同一个判断），示例点了跳过去也没意义——之前只判断 onUseExample 有没有传，
                没管 MCP 当下能不能真用，2026-08-12 用户实测发现禁用态下示例还能点，改成两个
                条件都满足才可点。 */}
            {detail.examples.map((example) =>
              onUseExample && linked ? (
                <button
                  key={example}
                  type="button"
                  onClick={() => onUseExample(example, name)}
                  className="flex items-center gap-1.5 rounded-full border border-border bg-bg-muted px-3 py-1 text-[12px] leading-[18px] text-text-muted transition-colors hover:border-[color:var(--color-chat-accent)] hover:text-[color:var(--color-chat-accent)]"
                >
                  <NewConversationIcon size={12} />
                  {example}
                </button>
              ) : (
                <span
                  key={example}
                  className="rounded-full border border-border bg-bg-muted px-3 py-1 text-[12px] leading-[18px] text-text-muted"
                >
                  {example}
                </span>
              ),
            )}
          </div>
        </div>
      )}

      {connector.source === 'customize' && detail && (
        // 自定义 MCP 的连接配置只读展示，作为编辑前的概览——真正改配置走右上角"编辑"按钮
        // （2026-08-18 已接上，见 onEdit/RegisterMcpPage editName 回填），这里维持只读摘要，
        // 不在这块小卡片里直接改。env/headers 只展示 key，不展示 value——即使后端
        // 这两个字段是明文返回的（给编辑表单回填用），只读场景没必要把密钥渲染到页面上。
        <div className="mb-6">
          <h2 className="mb-3 text-[14px] font-semibold leading-[22px] text-text">{t('connectorMarket.detail.sections.connectionConfig')}</h2>
          <div className="space-y-2 rounded-xl border border-border bg-card p-4 text-[12px] leading-5">
            {detail.transport && (
              <div className="flex gap-2">
                <span className="shrink-0 text-text-muted">{t('connectorMarket.detail.config.transport')}</span>
                <span className="break-all text-text">{detail.transport}</span>
              </div>
            )}
            {detail.transport === 'stdio' ? (
              detail.command && (
                <div className="flex gap-2">
                  <span className="shrink-0 text-text-muted">{t('connectorMarket.detail.config.command')}</span>
                  <span className="break-all font-mono text-text">
                    {detail.command}
                    {detail.args && detail.args.length > 0 ? ` ${detail.args.join(' ')}` : ''}
                  </span>
                </div>
              )
            ) : (
              <>
                {detail.url && (
                  <div className="flex gap-2">
                    <span className="shrink-0 text-text-muted">{t('connectorMarket.detail.config.url')}</span>
                    <span className="break-all font-mono text-text">{detail.url}</span>
                  </div>
                )}
                {detail.headers && Object.keys(detail.headers).length > 0 && (
                  <div className="flex gap-2">
                    <span className="shrink-0 text-text-muted">{t('connectorMarket.detail.config.headers')}</span>
                    <span className="break-all font-mono text-text">
                      {Object.keys(detail.headers).map((key) => `${key}=***`).join(', ')}
                    </span>
                  </div>
                )}
              </>
            )}
            {detail.env && Object.keys(detail.env).length > 0 && (
              <div className="flex gap-2">
                <span className="shrink-0 text-text-muted">{t('connectorMarket.detail.config.env')}</span>
                <span className="break-all font-mono text-text">
                  {Object.keys(detail.env).map((key) => `${key}=***`).join(', ')}
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      {installed && skills && skills.length > 0 && (
        <div className="mb-6">
          <h2 className="mb-3 text-[14px] font-semibold leading-[22px] text-text">{t('connectorMarket.detail.sections.skills')}</h2>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {skills.map((skill) => (
              <div key={skill.name} className="relative rounded-xl border border-border bg-card p-4">
                <div className="mb-1.5 flex items-center gap-2.5">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[7.5px] border border-connector-tool-icon-border bg-connector-tool-icon-surface text-text-muted">
                    <SkillIcon aria-hidden width={16} height={16} />
                  </span>
                  <span className="text-[14px] font-semibold leading-[22px] text-text">{skill.name}</span>
                </div>
                {/* min-h-5（=leading-5，20px）：desc 为空字符串时 <p> 没有任何行内内容，不会
                    撑出一个 line box，浏览器会把它渲染成 0 高度，导致这张卡片比旁边有描述的卡片
                    矮一截（2026-08-21 用户反馈）。固定 min-height 让描述有没有内容都占同样的高度。 */}
                <p className="min-h-5 truncate text-[13px] leading-5 text-[color:var(--color-text-placeholder)]" title={skill.description}>
                  {skill.description}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {installed && tools && tools.length > 0 && (
        <div className="mb-6">
          <h2 className="mb-3 text-[14px] font-semibold leading-[22px] text-text">{t('connectorMarket.detail.sections.tools')}</h2>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {tools.map((tool) => (
              <div key={tool.name} className="relative rounded-xl border border-border bg-card p-4">
                <div className="mb-1.5 flex items-center gap-2.5">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[7.5px] border border-connector-tool-icon-border bg-connector-tool-icon-surface text-text-muted">
                    <Wrench size={16} />
                  </span>
                  <span className="text-[14px] font-semibold leading-[22px] text-text">{tool.name}</span>
                </div>
                <p className="min-h-5 truncate text-[13px] leading-5 text-[color:var(--color-text-placeholder)]" title={tool.description}>
                  {tool.description}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {tokenTarget && (
        <ConnectTokenModal
          name={name}
          displayName={connector.displayName}
          iconUrl={connector.icon ?? undefined}
          response={tokenTarget}
          onCancel={() => setTokenTarget(null)}
          onConnected={() => setTokenTarget(null)}
        />
      )}

      {authTarget && (
        <CliAuthModal
          name={name}
          initial={authTarget}
          onCancel={() => setAuthTarget(null)}
          onConnected={() => setAuthTarget(null)}
        />
      )}
      </div>
    </div>
  );
}
