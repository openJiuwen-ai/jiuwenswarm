// 对齐 cjh/feature/MCP/专家与插件装备-前端接口(3).md §3（plugin_packages.*，v1.5）。
// 插件包的文案字段是后端直接下发的双语对象，不走 i18next——取值按当前语言选 zh/en 字段，
// 不要套用 connector.ts 那套"界面文案走 i18n key"的逻辑。
//
// 2026-08-07：category/source/installed/enabled 已经是 list/show 真实下发的字段（对齐新文档
// §3.1/§3.2），不再是前端本地模拟——之前 backend-requests.md 需求 8（分类字段）/需求 17
// （installed 字段）/需求 1 归属字段（对应 source）都是靠这批字段解决的。pluginPackageStore
// 仍保留 localStorage 兜底，只是用途从"唯一数据源"变成"离线乐观更新缓存"，见该 store 文件头注释。
//
// 2026-08-15 去除全局启用/禁用（状态C）：`enabled` 字段整个删除，插件不再有这个维度，见
// state-model-rectification-v2-remove-global-toggle.md。
//
// 2026-08-17 对齐 专家与插件装备-前端接口_v2.md §3.1/§3.2：占位字段 `connected?: boolean`
// （backend-requests.md 需求22，恒 true 占位）换成文档给出的真实字段 `connection_state:
// 'connected'|'disconnected'|'connecting'`（list/show 都有，命名对齐 MCP 侧 ConnectorSummary.
// connectionState 的驼峰写法）；`show` 额外带 `pending_connectors: string[]`——未就绪的待连
// MCP 名单，只在 show 里有（list 没有），见 §1.6.4"已装重连"。当前后端实例这批接口还没实现
// （返回 unknown method，属预期内——后端只有接口文档、代码还没写），类型先按文档对齐，真实联调
// 等后端 ready。

export interface LocalizedText {
  zh: string;
  en: string;
}

export type PluginPackageSource = 'local' | 'builtin' | 'hub';

/** 包级连接态，语义同 MCP 侧 ConnectorSummary.connectionState（v2 §2.1/§3.1）。 */
export type PluginConnectionState = 'connected' | 'disconnected' | 'connecting';

export interface PluginPackageSummary {
  id: string;
  runtimePackageName: string;
  hubAssetId?: string;
  displayName: LocalizedText;
  displayDescription: LocalizedText;
  /** 分类；来自 manifest.category；后端缺省为 ""——前端按"未分类"归进"其他"桶处理。 */
  category: string;
  /** local: 用户自己创建，builtin: 广场内置。 */
  source: PluginPackageSource;
  /** 是否已安装；未登记时后端按目录存在视为 true。 */
  installed: boolean;
  /**
   * 插件依赖的 connector（MCP）是否就绪；v2 §3.1 list/show 都下发。`connected` 才能
   * chat.send（§1.3 硬拒绝），装了但未连接走"已装重连"（§1.6.4）。
   */
  connectionState: PluginConnectionState;
  version?: string;
}

export interface PluginCapabilityRef {
  id: string;
  displayName: LocalizedText;
  displayDescription: LocalizedText;
}

export interface PluginPackageDetail extends PluginPackageSummary {
  /** 包内头像相对路径；manifest 无则 ""。 */
  avatar?: string;
  version?: string;
  details?: string;
  tags: LocalizedText[];
  skills: PluginCapabilityRef[];
  tools: PluginCapabilityRef[];
  rails: PluginCapabilityRef[];
  mcps: PluginCapabilityRef[];
  /**
   * 未就绪（非 connected）的待连 MCP 名单；仅 show 有（v2 §3.2）。§1.6.4"已装重连"首选这份
   * 名单串行走 mcp.connect，不要自己猜 MCP 名。
   */
  pendingConnectors?: string[];
  /**
   * 2026-08-21 后端新增：manifest 里的 quick_inputs（双语示例问法），仅 show 有——跟 MCP 侧
   * ConnectorDetail.examples 是同一个"详情页试试这样用"概念，但插件这边沿用包文案的双语对象
   * 惯例（同 displayName/tags），不是 MCP 那边的纯字符串数组，渲染时要过 localizedText()。
   */
  quickInputs?: LocalizedText[];
}

export function localizedText(value: LocalizedText, language: string): string {
  return language.startsWith('zh') ? value.zh : value.en;
}
