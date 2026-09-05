// 对齐 cjh/feature/MCP/MCP 接口文档.md，蛇形转驼峰。
// MCP 侧数据模型，不含"kind"字段——插件市场走独立的 pluginPackage.ts + plugin_packages.* 接口，
// 不是这份数据里的一个分类值。
//
// 2026-08-10 按新接口文档整体改造：
// - 新增 source（built_in|customize）——"我的"vs"广场"归属唯一依据，见 state-model-rectification
//   的更正结论：built_in 永远在广场、customize 永远在"我的"，与 connectionState/enabled 无关，
//   不再需要 connectorStore.ts 里那份 localStorage 模拟的"曾经连接过"名单。
// - enabled 现在由 mcp.list/mcp.show 直接下发，不再是前端纯本地维护的乐观状态。
// - trustState 整个删除——新接口没有 trust 这个状态轴，旧字段在组件里从未被渲染，属于死代码。
// - ConnectorConnectResponseType 收窄为 connect 真实会返回的 3 种；register_custom 成功统一复用
//   'connected'（不再有单独的 'registered'）。
//
// 2026-08-10 状态机重构：
// - 删除 connected 布尔镜像字段——后端下发的 `connection_state === 'connected'` 镜像，但全代码库
//   0 处读取（所有判断实际走 connectionState）。死字段，连同 connectorApi.ts 的映射一并清除。
// - 卡片态不再由各组件内联 `connectionState === 'connected'` 判断，改由 mcpState.ts 的
//   deriveCardState() 集中派生 McpCardState（idle/connecting/connected/error），统一消费。
//
// 2026-08-15 去除全局启用/禁用（状态C）：产品和后端又对齐一轮，MCP（连同插件）都不再有这个
// 维度，见 state-model-rectification-v2-remove-global-toggle.md。`enabled` 字段整个从这份类型
// 删除——后端目前可能还在下发这个字段（对齐是双向的，后端接口更新前不保证已经摘掉），
// connectorApi.ts 的 RawConnectorSummary 里照旧声明但不再读取（同款处理见 `connected` 死字段
// 那次改造），避免后端字段还没摘掉时这里读出一个类型上不存在的属性。

export type ConnectorIntegrationType = 'stdio-mcp' | 'cli' | 'remote-mcp' | 'skill-only';
export type ConnectorConnectionState = 'connected' | 'disconnected' | 'connecting' | 'error';
export type ConnectorSource = 'built_in' | 'customize' | 'hub';
// connectorStore.busyMap 的取值——per-name 进行中的重操作种类（原来只是个 boolean，卡片一律显示
// "连接中"，2026-08-11 用户发现点"解绑"卡片却显示"连接中"才暴露这个问题：busy 态需要知道具体
// 是哪种操作才能给出准确文案，见 mcpState.ts busyLabelKey）。
// 2026-08-17：'delete' 随 deleteConnector action 一并删除——当时彻底删除入口移除后不再有"删除中"
// 态。2026-08-19 用户明确要求恢复：断联态的自定义 MCP 详情页"卸载"按钮要走真删除（mcp.delete_
// custom），不能只是再调一次 disconnect，'delete' 取值随之恢复。
export type McpBusyKind = 'install' | 'uninstall' | 'connect' | 'disconnect' | 'delete' | 'saveCredentials';

export interface ConnectorSummary {
  /** Stable marketplace identity. Hub packages use the Hub asset id. */
  id: string;
  name: string;
  /** Runtime package name used by mcp.connect/chat.send. */
  runtimePackageName: string;
  hubAssetId?: string;
  displayName: string;
  // mcp.list 现在直接下发简介（backend-requests.md 需求14已解决），恒为 string，不再是可选缺省。
  description: string;
  category: string;
  integrationType: ConnectorIntegrationType;
  connectionState: ConnectorConnectionState;
  hasBundledSkills: boolean;
  icon?: string | null;
  // "我的"vs"广场"归属的唯一依据，见文件头注释。
  source: ConnectorSource;
  installed: boolean;
  version?: string;
  tags?: string[];
}

export interface ConnectorInstallResponse {
  type: 'installed';
  item: { id: string; name: string; installed: boolean };
}

export interface ConnectorUninstallResponse {
  type: 'uninstalled';
  item: { id: string; name: string; removed: boolean };
  applied: boolean;
  error?: string;
}

export interface ConnectorTool {
  name: string;
  description: string;
}

export interface ConnectorSkill {
  name: string;
  description: string;
}

export interface ConnectorDetail extends ConnectorSummary {
  examples?: string[];
  mcpSpec?: Record<string, unknown> | null;
  cliSpecPresent?: boolean;
  bundledSkills: string[];
  // mcp.show 一次性带回，不再需要单独的 connector.list_tools 请求。
  skills: ConnectorSkill[];
  tools: ConnectorTool[];
  // 自定义 MCP 编辑回填字段，仅 source==='customize' 时有意义（env/headers 原样明文返回）。
  transport?: 'stdio' | 'sse' | 'streamable-http';
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  timeoutS?: number;
}

export type ConnectorConnectResponseType = 'connected' | 'credentials_required' | 'auth_required';

export interface ConnectorCredentialField {
  label?: string;
  placeholder?: string;
  type?: 'text' | 'password';
  description?: string;
}

export interface ConnectorConnectResponse {
  type: ConnectorConnectResponseType;
  name?: string;
  applied?: boolean;
  error?: string;
  item?: Record<string, unknown>;
  installedSkills?: string[];
  serverIdScope?: string;
  // credentials_required 分支专属字段
  credentialsRequired?: boolean;
  requiredTokens?: string[];
  credentialKind?: 'token';
  title?: string;
  description?: string;
  docUrl?: string | null;
  docLabel?: string;
  fields?: Record<string, ConnectorCredentialField>;
  // auth_required 分支专属字段
  stepIndex?: number;
  stepsTotal?: number;
  authUrl?: string | null;
  authDomain?: string;
  command?: string;
}
