import type { ConnectorSummary, McpBusyKind } from '../../types/connector';

// MCP 卡片统一状态机。取代之前散落在 MarketplacePage/McpDetailPage 各自内联的
// `connectionState === 'connected'` 判断——那套写法把后端 4 值枚举（connected/disconnected/
// connecting/error）塌缩成二元（connected vs 其它），error 态完全没被消费（连失败的 MCP 显示成
// 和从未连过一样），connecting 态只有 registerCustom 路径写过、connect 路径靠 busyName 兜底，
// 两条路径不一致。现在用一个纯派生函数把 connectionState + busy 两路信号压成一个语义明确的
// 卡片态，所有组件消费它，不再裸读 connectionState/busyMap。
//
// 照抄 utils/skillAvatar.ts getSkillAvatar 的纯函数模式：无副作用、可单测、可 memo、从原始数据
// 派生展示值。
//
// 2026-08-15 去除全局启用/禁用（状态C）：插件/MCP 都不再有这个维度（见
// state-model-rectification-v2-remove-global-toggle.md），原来的 'disabled'
// （已连接+被全局禁用）态整个删除——"已连接"不再需要区分是否被禁用，只剩 idle/connecting/
// connected/error 四态。

export type McpCardState =
  // 未连接（disconnected）：显示"+"联接按钮
  | 'idle'
  // 连接中（connecting 或有进行中的重操作）：spinner 占位，所有操作按钮藏起来
  | 'connecting'
  // 已连接：会话使用 icon + 卸载
  | 'connected'
  // 连接失败（error）：红点 + "连接失败"文案 + 可重试"+"
  | 'error';

export interface McpCardInput {
  connectionState: ConnectorSummary['connectionState'];
  // 进行中的重操作种类（connect/disconnect/delete/saveCredentials），没有就是 undefined。由
  // store 在操作前置位、完成/失败清除。busy 优先于 connectionState——操作进行中即使后端
  // connectionState 还没翻成 connecting，卡片也显示占位，遮盖底态的瞬时不一致。registerCustom
  // 走 connectionState='connecting' 占位卡，不重复置 busy。
  //
  // 2026-08-11 之前这里是纯 boolean，卡片态只要 busy 就统一显示 'connecting'，文案也写死"连接中"
  // ——点"解绑"时 disconnect 也会置 busy，卡片却显示"连接中"，用户实测发现这个误导。卡片态本身
  // （McpCardState）不拆细粒度（拆了要多出 disconnecting/deleting 好几个态，按钮矩阵、
  // canOpenDetail 等下游判断全部要跟着长分支），只把 busy 的取值从 boolean 换成具体操作种类，
  // 卡片组件读 busyKind 单独选精确文案，见 busyLabelKey。
  busy?: McpBusyKind;
}

// MCP 主派生函数。busy 直接传 busyMap[name]（undefined 就是不忙，不需要 `?? false` 兜底）。
export function deriveCardState(input: McpCardInput): McpCardState {
  if (input.busy) return 'connecting';
  switch (input.connectionState) {
    case 'connecting':
      return 'connecting';
    case 'error':
      return 'error';
    case 'connected':
      return 'connected';
    case 'disconnected':
    default:
      return 'idle';
  }
}

// busy 态精确文案 key。connect/saveCredentials 都属于"正在建立连接"，沿用 card.connecting；
// disconnect 有专属文案，不再统一显示"连接中"。调用方（MarketCard/MyMarketCard）
// busyKind 为 undefined 时（比如 registerCustom 占位卡、后端真实推的 connecting 中间态，这两种
// 场景不经过 busyMap）回退到 card.connecting，语义上仍然准确——那本来就是"正在连接"。
// 2026-08-17：'delete' 分支曾随 deleteConnector action 一并删除；2026-08-19 deleteConnector
// 恢复后补回专属文案，不跟着 disconnect/connect 混用（"删除中"跟"解绑中"是不同的操作反馈）。
export function busyLabelKey(kind: McpBusyKind | undefined): string {
  switch (kind) {
    case 'disconnect':
      return 'connectorMarket.card.disconnecting';
    case 'delete':
      return 'connectorMarket.card.deleting';
    case 'connect':
    case 'saveCredentials':
    default:
      return 'connectorMarket.card.connecting';
  }
}

// MCP 详情页可达性。2026-08-15 新方案要求"已安装+未连接"这个中间态也要能打开详情页（展示断联
// banner），不再像旧版一样只要不是 connected 就整个拒绝进入（canOpenDetail 旧逻辑）。
// "已安装"对 MCP 来说：customize（我的MCP）一旦注册就有持久身份，不管当下连没连，恒为 true；
// built_in（广场）没有独立于 connectionState 的"是否曾注册"标记，只能退化成"当前不是从未连接过
// 的 idle 态"（connecting/connected/error 都算"已安装"）——一旦断开连接会翻回 idle，视觉上等同
// "未安装"，这是当前数据模型下的合理简化，具体是否需要后端补一个独立标记见
// state-model-rectification-v2-remove-global-toggle.md 第5节待确认问题。
export function deriveMcpAvailability(
  source: ConnectorSummary['source'],
  state: McpCardState,
): { installed: boolean; linked: boolean } {
  return { installed: source === 'customize' || state !== 'idle', linked: state === 'connected' };
}

// 连接过程中卡片只展示进度反馈，详情页此时没有可执行操作，也不应响应卡片点击。
export function canOpenMcpDetail(
  source: ConnectorSummary['source'],
  state: McpCardState,
): boolean {
  return state !== 'connecting' && deriveMcpAvailability(source, state).installed;
}

// 插件侧的卡片态派生。插件没有 connectionState 多值模型（plugin_packages.* 只有 installed boolean，
// 没有连接中/连接失败态），但 MarketCard 是插件和 MCP 共用的展示壳，需要把插件的 installed/
// connected 归一到同一套 McpCardState。插件恒不会进 connecting/error 分支。
//
// 2026-08-15：connected 是新增的第二个维度——插件"是否可用"现在不只看 installed，还要看它绑定
// 的 MCP 有没有连接（见 state-model-rectification-v2 文档 2.2 节），字段名和后端接口
// `plugin_packages.list/show` 的 `connected` 保持一致。卡片列表层级"已安装+MCP未连接"和
// "未安装"两种情况按钮完全一样（都显示"+"），所以这里两者都塌缩进 'idle'，只有detail页面才
// 需要把这两种情况分开展示（见 PluginDetailPage.tsx 直接读 installed/connected 两个布尔值，
// 不通过这个函数）。
export function derivePluginCardState(installed: boolean, connected: boolean): McpCardState {
  if (!installed || !connected) return 'idle';
  return 'connected';
}

// statusFilter 派生：把 McpCardState 归到列表筛选用的 pending/available 两态（全局启用/禁用
// 去掉后，不再有第三个"disabled"筛选项）。connecting 和 error 都归 pending（连接中/连失败都
// 尚未可用）。
export function cardStateToStatusFilter(state: McpCardState): 'pending' | 'available' {
  switch (state) {
    case 'connected':
      return 'available';
    case 'connecting':
    case 'error':
    case 'idle':
    default:
      return 'pending';
  }
}
