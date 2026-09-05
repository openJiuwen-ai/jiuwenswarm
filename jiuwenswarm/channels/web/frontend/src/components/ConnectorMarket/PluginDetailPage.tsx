import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Trash2, Plus, Wrench, Link2, Plug, Loader2, X, ExternalLink, Pencil } from 'lucide-react';
import { usePluginPackageStore } from '../../stores/pluginPackageStore';
import { localizedText, type PluginCapabilityRef } from '../../types/pluginPackage';
import { NewConversationIcon } from './icons';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { EntityAvatar } from './EntityAvatar';
import { PillButton, DetailLinkButton } from './Buttons';
import { ConfirmDialog } from './ConfirmDialog';
import { usePendingConnectorFlow, PendingConnectorModals } from './usePendingConnectorFlow';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';

interface PluginDetailPageProps {
  id: string;
  onBack: () => void;
  /** 从"我的扩展"里点进来的，还是从插件广场点进来的——面包屑和返回落点不一样。 */
  fromMy?: boolean;
  /** "我的插件"卸载后 show() 探测发现条目已不存在时调用，退出到"我的插件"列表页。 */
  onDeleted?: () => void;
  /** "会话使用"点击——真正入口是 ChatPanel 输入框的"+"面板，这里没有真实目的地。 */
  onUse?: () => void;
  /**
   * 点"试试这样用"下面的某个示例——跳新会话并把示例文案填进输入框，同时打开这个插件的会话内
   * 启用开关。跟 McpDetailPage.tsx 的 onUseExample 是同一条设计（App.tsx 那边对称接了
   * onUsePluginExample，走同一个 requestSessionNavigation('new', {initialInputValue,
   * initialEnabledPlugins:[id]})），只是插件这边不复用 MCP 那个 prop——两者的第二个参数语义
   * 不同（mcpName vs pluginId），且各自的"能不能点"门控条件也不共享一个 onUseExample 引用更
   * 清楚。不传就退化成纯展示（同款可选 prop 处理见 onUse）。
   */
  onUseExample?: (text: string, pluginId: string) => void;
}

// 2026-08-15 按钮矩阵再整改（去除全局启用/禁用，见
// state-model-rectification-v2-remove-global-toggle.md）：原来的"卸载"（可逆，广场视角，不需
// 确认）和"删除"（我的视角，需确认）合并成一个统一的"卸载"按钮，广场/我的两处都需要弹框确认
// （新方案文案明确写了）。插件还有一个状态轴——依赖的 connector 是否就绪（`connectionState`，
// 字段名和后端接口 `connection_state` 对齐，见 pluginPackageStore.ts 头注释）：已安装但未就绪
// 时展示断联 banner。
//
// 2026-08-17 对齐 专家与插件装备-前端接口_v2.md §1.6：占位字段 `connected` 换成真实
// `connectionState` 之后，"未就绪"不再是永远不触发的占位分支——现在有真实的 `pendingConnectors`
// 名单可用，"使用"按钮和断联 banner 的"连接MCP"都改成真正驱动连接续跑（复用
// usePendingConnectorFlow，和 McpDetailPage.tsx 的 handleUse/pendingUseAfterConnect 是同一个
// 设计意图，但这里用 ref 而不是 state 存"要不要连完自动跳转"这个标记——handleUse 里
// setState 和触发连接续跑是在同一个事件处理函数内同步发生的，若用 state，串行连接第一步的
// 回调闭包会读到这次渲染里 setState 生效前的旧值，导致连完误判"不需要跳转"；ref 没有这个
// 陈旧闭包问题）。
//
// 卸载后的收尾行为，广场和我的不对称（按新方案文案逐条对应）：
// - 广场视角（!fromMy）："卸载后回到不可用状态"——留在详情页，卡片自然变成"未安装"展示，
//   不用跳转。
// - 我的视角（fromMy）："卸载后通过 show 去 get 这个插件，还有数据就留在详情页继续显示，
//   没有就退出到我的插件列表页"——卸载完重新拉一次详情，probe 出这个条目还在不在。插件目前
//   uninstall 从不会让条目从 plugin_packages.list/show 里消失（只是 installed 变 false，见
//   pluginPackageStore.ts uninstall 的注释），所以这条"读不到就退出"分支在当前后端行为下大概率
//   不会触发，但逻辑按方案原文实现，为将来后端行为变化（真的会让条目消失）留好退路。
export function PluginDetailPage({ id, onBack, fromMy, onDeleted, onUse, onUseExample }: PluginDetailPageProps) {
  const { t, i18n } = useTranslation();
  const detail = usePluginPackageStore((s) => s.detailCache[id]);
  const loadDetail = usePluginPackageStore((s) => s.loadDetail);
  const probeExists = usePluginPackageStore((s) => s.probeExists);
  const installed = usePluginPackageStore((s) => s.installed[id] ?? false);
  const connectionState = usePluginPackageStore((s) => s.connectionStateMap[id] ?? 'disconnected');
  const installPending = usePluginPackageStore((s) => s.installPendingMap[id]);
  const clearInstallPending = usePluginPackageStore((s) => s.clearInstallPending);
  const install = usePluginPackageStore((s) => s.install);
  const uninstall = usePluginPackageStore((s) => s.uninstall);
  const [installing, setInstalling] = useState(false);
  const [confirmUninstall, setConfirmUninstall] = useState(false);
  const [uninstalling, setUninstalling] = useState(false);
  useEffect(() => {
    loadDetail(id);
  }, [id, loadDetail]);

  // §1.6.3 首次安装：install() 半途失败会把 pending_connectors 记进 store（见
  // pluginPackageStore.ts install 注释），这里侦测到就自动起串行连接续跑；全部连完在
  // onAllConnected 里幂等重试 install（真正落盘，文档强调"再次调用同一个 install"）。
  const installFlow = usePendingConnectorFlow(
    () => {
      clearInstallPending(id);
      void install(id);
    },
    () => {
      // 依赖 connector 自动连接失败/被取消：清掉 pending 名单，否则下面这个 effect 会因为
      // installPending 仍非空而反复重启连接续跑。安装到此终止，用户处理好依赖 MCP 后可再点"安装"。
      // 失败原因 connectorStore 已写进自己的 error（顶层红色 Toast），这里不重复弹。
      clearInstallPending(id);
    },
  );

  useEffect(() => {
    if (installPending && installPending.length > 0 && !installFlow.active) {
      installFlow.start(installPending);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [installPending]);

  // §1.6.4 已装重连：直接用 detail.pendingConnectors 连，连完只刷新 detail（**不**再调
  // install——文档原文强调了两次"全程不调 install"）。连完不再自动跳转会话——"使用"按钮在
  // 未就绪时已 disabled（2026-08-29 用户要求待连接状态使用按钮不可用），连接由断联 banner
  // 触发，连完留在详情页让用户自己点"使用"。
  const reconnectFlow = usePendingConnectorFlow(() => {
    void loadDetail(id);
  });

  if (!detail) return null;

  const title = localizedText(detail.displayName, i18n.language);
  const avatar = getSkillAvatar(title);
  const linked = connectionState === 'connected';
  const installBusy = installing || installFlow.active;

  async function handleInstall() {
    setInstalling(true);
    await install(id);
    setInstalling(false);
  }

  // "使用"：仅在依赖 connector 已就绪（linked）时可点——按钮在 !linked 时 disabled（见下方
  // JSX），进来时必然已就绪，直接跳转。未就绪时的连接走断联 banner 的"连接MCP"，不再由
  // "使用"按钮承担触发连接续跑的职责（2026-08-29 用户要求：待连接状态使用按钮不可用）。
  function handleUse() {
    if (linked) onUse?.();
  }

  async function handleUninstall() {
    setUninstalling(true);
    await uninstall(id);
    setConfirmUninstall(false);
    if (fromMy) {
      // 2026-08-21 用户反馈根因确认：后端卸载会把包目录整个删掉，探测"还在不在"用 probeExists
      // （不是 loadDetail）——404 在这里是预期内的正常结果，不该像 loadDetail 那样在失败时顺带
      // 弹一条红色错误 Toast，把"卸载完预期中的探测404"和"真错误"混在一起，见 pluginPackageStore.ts
      // probeExists 的注释。
      const stillExists = await probeExists(id);
      setUninstalling(false);
      if (!stillExists) onDeleted?.();
    } else {
      setUninstalling(false);
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
            iconUrl={detail.avatar}
            avatar={avatar}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl text-[16px] font-semibold"
          />
          <div>
            <h1 className="text-[18px] font-semibold leading-7 text-text">{title}</h1>
            {detail.tags.length > 0 && (
              <div className="mt-0.5 flex flex-wrap gap-1">
                {detail.tags.map((tag) => (
                  <span
                    key={localizedText(tag, i18n.language)}
                    className="inline-block rounded-[2px] bg-connector-tag-surface px-1.5 py-0.5 text-[12px] leading-[18px] text-text"
                  >
                    {localizedText(tag, i18n.language)}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center gap-3">
          {/* 自定义插件（source==='local'）的编辑——后端 plugin_packages.* 目前只有
              list/show/create/install/uninstall，没有任何 update/编辑接口（create 对已存在 id
              会直接拒绝，不是隐式 upsert，见 backend-requests.md 需求13），先做降级占位：按钮
              可见，点击提示"等待后端支持"，不假装真的能保存。等后端补上编辑接口后再接成真实表单
              （参照 McpDetailPage.tsx 的 onEdit + RegisterMcpPage editName 回填这一套做法）。 */}
          {/* 2026-08-29 用户要求：自定义插件的编辑按钮先全部隐藏（隐藏不删除），等后端编辑接口
              就绪后去掉下面的 `&& false` 即可恢复原占位逻辑。 */}
          {detail.source === 'local' && false && (
            <DetailLinkButton
              icon={<Pencil size={14} />}
              label={t('connectorMarket.card.edit')}
              onClick={() => window.alert(t('connectorMarket.card.editNotSupportedYet'))}
            />
          )}
          {/* 2026-08-31 用户要求：自定义插件（source==='local'）无论安装/连接处于什么状态，
              都要能卸载（刚 create 完还没安装时也不例外）——卸载走的就是后端
              uninstall_plugin_package（会把包目录整个删掉，见 pluginPackageStore.ts），对自定义
              插件语义上等同于"删除这个插件"。built-in 插件仍保持"装了才有卸载"。 */}
          {(installed || detail.source === 'local') && (
            <DetailLinkButton
              icon={<Trash2 size={14} />}
              label={t('connectorMarket.card.uninstall')}
              onClick={() => setConfirmUninstall(true)}
              danger
              disabled={uninstalling}
            />
          )}
          {installed && (
            <button
              type="button"
              onClick={handleUse}
              disabled={!linked || reconnectFlow.active}
              className="flex items-center gap-1 text-[13px] text-text hover:text-[color:var(--color-chat-accent)] disabled:cursor-not-allowed disabled:opacity-60"
            >
              <NewConversationIcon size={14} />
              {t('connectorMarket.card.use')}
            </button>
          )}
          {!installed && !installBusy && (
            <PillButton icon={<Plus size={14} />} label={t('connectorMarket.card.install')} onClick={handleInstall} />
          )}
          {!installed && installBusy && (
            <PillButton
              icon={<Loader2 size={14} className="animate-spin" />}
              label={t('connectorMarket.card.installing')}
              disabled
            />
          )}
        </div>
      </div>

      {/* "已安装+依赖 connector 未就绪"断联提示（§1.6.4 已装重连）——"连接MCP"直接用
          detail.pendingConnectors 驱动真实连接续跑，不再是空目的地占位。
          2026-08-20 用户明确要求：文案不变，但图标/"连接MCP"文字颜色/整行底色直接抄
          McpDetailPage.tsx 断联 banner 那一版视觉（浅红底 #FCE3E1 + 红圆底白X图标 + accent蓝
          链接文字），不用这里原来的纯文字+danger红。 */}
      {installed && !linked && (
        <div className="mb-6 flex items-center gap-1.5 rounded-lg bg-[#FCE3E1] px-3 py-2 text-[13px] text-text-muted">
          <span className="flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-full bg-danger">
            <X size={9} strokeWidth={3} className="text-text-inverse" />
          </span>
          <span>{t('connectorMarket.detail.mcpDisconnectedBanner')}</span>
          <button
            type="button"
            disabled={reconnectFlow.active}
            className="flex items-center gap-0.5 font-medium text-[color:var(--color-chat-accent)] hover:opacity-80 disabled:opacity-60"
            onClick={() => reconnectFlow.start(detail.pendingConnectors ?? [])}
          >
            {t('connectorMarket.detail.connectMcp')}
            <ExternalLink size={12} />
          </button>
        </div>
      )}

      {confirmUninstall && (
        <ConfirmDialog
          title={t('connectorMarket.confirmUninstall.title', { name: title })}
          message={t('connectorMarket.confirmUninstallPlugin.message')}
          confirmLabel={t('connectorMarket.card.uninstall')}
          onCancel={() => setConfirmUninstall(false)}
          onConfirm={handleUninstall}
        />
      )}

      <Section title={t('connectorMarket.detail.sections.basicInfo')}>
        <p className="text-[12px] leading-[18px] text-text">
          {localizedText(detail.displayDescription, i18n.language)}
        </p>
      </Section>

      {/* "试试这样用"——照抄 McpDetailPage.tsx 同款示例区，2026-08-21 后端 show 接口新增
          quickInputs（双语对象数组，跟 MCP 的 examples: string[] 不同，要过 localizedText()）。
          未就绪（installed && !linked）时这个插件在会话里根本用不了，示例点了也没意义，跟 MCP
          那边"onUseExample && linked 才可点，否则渲染成不可点的纯展示 span"是同一个门控。 */}
      {detail.quickInputs && detail.quickInputs.length > 0 && (
        <div className="mb-6">
          <h2 className="mb-3 text-[14px] font-semibold leading-[22px] text-text">
            {t('connectorMarket.detail.sections.examples')}
          </h2>
          <div className="flex flex-wrap gap-2">
            {detail.quickInputs.map((quickInput) => {
              const text = localizedText(quickInput, i18n.language);
              return onUseExample && linked ? (
                <button
                  key={text}
                  type="button"
                  onClick={() => onUseExample(text, id)}
                  className="flex items-center gap-1.5 rounded-full border border-border bg-bg-muted px-3 py-1 text-[12px] leading-[18px] text-text-muted transition-colors hover:border-[color:var(--color-chat-accent)] hover:text-[color:var(--color-chat-accent)]"
                >
                  <NewConversationIcon size={12} />
                  {text}
                </button>
              ) : (
                <span
                  key={text}
                  className="rounded-full border border-border bg-bg-muted px-3 py-1 text-[12px] leading-[18px] text-text-muted"
                >
                  {text}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {detail.skills.length > 0 && (
        <Section title={t('connectorMarket.detail.sections.skills')}>
          <CapabilityGrid items={detail.skills} language={i18n.language} icon={undefined} skillStyle />
        </Section>
      )}

      {detail.tools.length > 0 && (
        <Section title={t('connectorMarket.detail.sections.tools')}>
          <CapabilityGrid items={detail.tools} language={i18n.language} icon={<Wrench size={16} />} />
        </Section>
      )}

      {detail.rails.length > 0 && (
        <Section title={t('connectorMarket.detail.sections.rails')}>
          <CapabilityGrid items={detail.rails} language={i18n.language} icon={<Link2 size={16} />} />
        </Section>
      )}

      {detail.mcps.length > 0 && (
        <Section title={t('connectorMarket.detail.sections.mcps')}>
          <CapabilityGrid items={detail.mcps} language={i18n.language} icon={<Plug size={16} />} />
        </Section>
      )}

      {/* 首次安装（installFlow）和已装重连（reconnectFlow）互斥——一个插件同一时刻只会处于
          "未安装"或"已装但未就绪"其中一种状态，不会两个 flow 同时 active，但保险起见两套弹窗
          都按各自的 active 状态独立渲染，不额外加互斥判断。 */}
      <PendingConnectorModals flow={installFlow} />
      <PendingConnectorModals flow={reconnectFlow} />
      </div>
    </div>
  );
}

function CapabilityGrid({
  items,
  language,
  icon,
  skillStyle,
}: {
  items: PluginCapabilityRef[];
  language: string;
  icon?: React.ReactNode;
  skillStyle?: boolean;
}) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
      {items.map((item) => {
        const title = localizedText(item.displayName, language);
        const desc = localizedText(item.displayDescription, language);
        const avatar = getSkillAvatar(title);
        return (
          <div key={item.id} className="relative rounded-xl border border-border bg-card p-4">
            <div className="mb-1.5 flex items-center gap-2.5">
              {skillStyle ? (
                <span
                  className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] text-[16px] font-black text-text-inverse ${avatar.color}`}
                >
                  {avatar.firstChar}
                </span>
              ) : (
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[7.5px] border border-connector-tool-icon-border bg-connector-tool-icon-surface text-text-muted">
                  {icon}
                </span>
              )}
              <span className="text-[14px] font-semibold leading-[22px] text-text">{title}</span>
            </div>
            {/* min-h-5（=leading-5，20px）：desc 为空字符串时 <p> 没有任何行内内容，不会撑出
                一个 line box，浏览器会把它渲染成 0 高度，导致这张卡片比旁边有描述的卡片矮一截
                （2026-08-21 用户反馈，同款修法见 McpDetailPage.tsx 的技能/工具卡片）。 */}
            <p
              className="min-h-5 truncate text-[13px] leading-5 text-[color:var(--color-text-placeholder)]"
              title={desc}
            >
              {desc}
            </p>
          </div>
        );
      })}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-6">
      <h2 className="mb-3 text-[14px] font-semibold leading-[22px] text-text">{title}</h2>
      {children}
    </div>
  );
}
