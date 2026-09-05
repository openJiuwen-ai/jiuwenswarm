import { webRequest } from './webClient';
import type {
  ConnectorConnectResponse,
  ConnectorCredentialField,
  ConnectorDetail,
  ConnectorInstallResponse,
  ConnectorSummary,
  ConnectorUninstallResponse,
} from '../types/connector';
import { normalizeEquipmentIdentity, normalizeEquipmentSource } from '../features/equipmentMarketplace';
import { requestEquipmentList } from '../features/equipmentListRequest';

// 薄封装，照抄 projectRegistryClient.ts 惯例：一个方法一行 webRequest，不额外包装错误处理。
// 对齐 cjh/feature/MCP/MCP 接口文档.md，方法名/参数按该文档的 mcp.* 命名 + snake_case，响应体在
// 这里转成驼峰（各方法内联 map，不建通用转换器，量不大没必要）。
//
// 2026-08-10 按新接口文档整体改造：方法族从 connector.* 改成 mcp.*，且：
// - connector.delete → mcp.delete_custom（只对 source==='customize' 有效，built_in 会拿到
//   bad_request——调用方必须自己按 source 门控，这层不做隐式拦截，如实转发后端判断）
// - connector.status / connector.list_tools / connector.auth_complete 整个废弃：enabled/
//   connection_state 现在直接在 list/show 里给；tools 内嵌进 mcp.show 的 detail；CLI OAuth 轮询
//   收进后端一个 hold-open 的 mcp.wait_auth（见 waitAuth，前端不再自己轮询）。
// - connector.register_custom 成功响应从 'registered' 统一成 'connected'，跟 connect 复用同一
//   解析函数 fromRawConnect。
//
// 2026-08-10 起 mcp.* 已经在 dev_mcp 分支后端联调验证过（list/show/register_custom/
// delete_custom 全链路跑通），去掉了 MOCK FALLBACK：不再有 tryReal() 包装，真实调用失败就如实
// 抛错给调用方，不会静默掉回假数据掩盖问题。插件那半（pluginPackagesApi.ts）后端还没实现，
// mock 兜底继续保留，两边独立，不要混着改。

interface RawConnectorSummary {
  id?: string;
  name: string;
  package_name?: string;
  display_name: string;
  description: string;
  category: string;
  integration_type: ConnectorSummary['integrationType'];
  connection_state: ConnectorSummary['connectionState'];
  has_bundled_skills: boolean;
  icon?: string | null;
  source?: string;
  installed?: boolean;
  version?: string;
  tags?: string[];
  // connected 布尔镜像字段已删（2026-08-10 状态机重构，types/connector.ts 注释见说明），
  // 后端仍下发但我们不再读取——statusFilter/卡片态一律走 connection_state。
  connected: boolean;
  // 2026-08-15 全局启用/禁用（状态C）整个去掉，enabled 字段不再映射进 ConnectorSummary
  // （见 types/connector.ts 头注释）。后端接口对齐前可能仍会下发这个字段，这里保留声明但
  // 不读取，和 `connected` 死字段是同一处理方式。
  enabled?: boolean;
}

function fromRawSummary(raw: RawConnectorSummary): ConnectorSummary {
  const identity = normalizeEquipmentIdentity({
    id: raw.id,
    name: raw.name,
    packageName: raw.package_name,
    source: raw.source,
  });
  const source = normalizeEquipmentSource(raw.source, 'local');
  return {
    id: identity.id,
    name: identity.runtimePackageName,
    runtimePackageName: identity.runtimePackageName,
    hubAssetId: identity.hubAssetId,
    displayName: raw.display_name,
    description: raw.description,
    category: raw.category,
    integrationType: raw.integration_type,
    connectionState: raw.connection_state,
    hasBundledSkills: raw.has_bundled_skills,
    icon: raw.icon,
    source: source === 'builtin' ? 'built_in' : source === 'local' ? 'customize' : 'hub',
    installed: raw.installed ?? source !== 'hub',
    version: raw.version,
    tags: raw.tags ?? [],
  };
}

interface RawConnectorTool {
  name: string;
  description: string;
}

interface RawConnectorDetail extends RawConnectorSummary {
  examples?: string[];
  mcp_spec?: Record<string, unknown> | null;
  cli_spec_present?: boolean;
  bundled_skills: string[];
  skills: RawConnectorTool[];
  tools: RawConnectorTool[];
  transport?: 'stdio' | 'sse' | 'streamable-http';
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  url?: string;
  headers?: Record<string, string>;
  timeout_s?: number;
}

function fromRawDetail(raw: RawConnectorDetail): ConnectorDetail {
  return {
    ...fromRawSummary(raw),
    examples: raw.examples,
    mcpSpec: raw.mcp_spec,
    cliSpecPresent: raw.cli_spec_present,
    bundledSkills: raw.bundled_skills,
    skills: raw.skills ?? [],
    tools: raw.tools ?? [],
    transport: raw.transport,
    command: raw.command,
    args: raw.args,
    env: raw.env,
    url: raw.url,
    headers: raw.headers,
    timeoutS: raw.timeout_s,
  };
}

interface RawConnectResponse {
  type: ConnectorConnectResponse['type'];
  name?: string;
  applied?: boolean;
  item?: Record<string, unknown>;
  error?: string;
  installed_skills?: string[];
  server_id_scope?: string;
  credentials_required?: boolean;
  required_tokens?: string[];
  credential_kind?: ConnectorConnectResponse['credentialKind'];
  title?: string;
  description?: string;
  doc_url?: string | null;
  doc_label?: string;
  fields?: Record<string, ConnectorCredentialField>;
  step_index?: number;
  steps_total?: number;
  auth_url?: string | null;
  auth_domain?: string;
  command?: string;
}

function fromRawConnect(raw: RawConnectResponse): ConnectorConnectResponse {
  return {
    type: raw.type,
    name: raw.name,
    applied: raw.applied,
    item: raw.item,
    error: raw.error,
    installedSkills: raw.installed_skills,
    serverIdScope: raw.server_id_scope,
    credentialsRequired: raw.credentials_required,
    requiredTokens: raw.required_tokens,
    credentialKind: raw.credential_kind,
    title: raw.title,
    description: raw.description,
    docUrl: raw.doc_url,
    docLabel: raw.doc_label,
    fields: raw.fields,
    stepIndex: raw.step_index,
    stepsTotal: raw.steps_total,
    authUrl: raw.auth_url,
    authDomain: raw.auth_domain,
    command: raw.command,
  };
}

// mcp.wait_auth 是 hold-open 长轮询，后端在一个请求里等到 OAuth 完成/超时才回，前端超时要给够
// （文档 §1.3：10min），不能用默认的 15s。
const WAIT_AUTH_TIMEOUT_MS = 10 * 60 * 1000;
const REGISTER_CUSTOM_TIMEOUT_MS = 10 * 60 * 1000;
// mcp.connect 同样可能是 hold-open 的一次性握手（CLI 探活/OAuth 首步），后端最长等 10min 才回——
// 之前漏了这个超时覆盖，一直用 webClient 的默认 15s，CLI 类连接器随手一连就客户端先超时报错，
// 而后端其实还在正常跑，事后会把真实结果覆盖回来，造成"明明连上了却弹超时"的假象
// （2026-08-11 用户实测发现：token 弹窗提交后停留不退出、以及"连接中"几十秒后误报超时）。
const CONNECT_TIMEOUT_MS = 10 * 60 * 1000;

export const connectorApi = {
  // 2026-08-17 按 MCP 接口文档 v2：mcp.list 现在需要 filter 参数区分"广场"(builtin)/"我的"
  // (local，= 已连接的预置 + 全部自定义)。实测过 dev_aipc_feat_v2 分支：缺省 filter 时后端按
  // "builtin" 处理，之前不传 filter 的单次调用会让"我的MCP"完全看不到自定义 MCP，是真实 bug，
  // 不是理论风险（评估过程见 cjh/feature/MCP/_migration/mcp-interface-v2-gap-assessment.md）。
  list: async (filter: 'builtin' | 'local'): Promise<ConnectorSummary[]> => {
    const payload = await requestEquipmentList<{ items: RawConnectorSummary[] }>(webRequest, 'mcp.list', { filter });
    return payload.items.map(fromRawSummary);
  },
  show: async (id: string): Promise<ConnectorDetail> => {
    const payload = await webRequest<{ item: RawConnectorDetail }>('mcp.show', { id });
    return fromRawDetail(payload.item);
  },
  install: (id: string): Promise<ConnectorInstallResponse> =>
    webRequest<ConnectorInstallResponse>('mcp.install', { id }),
  uninstall: (id: string): Promise<ConnectorUninstallResponse> =>
    webRequest<ConnectorUninstallResponse>('mcp.uninstall', { id }),
  connect: async (name: string): Promise<ConnectorConnectResponse> => {
    const payload = await webRequest<RawConnectResponse>('mcp.connect', { name }, { timeoutMs: CONNECT_TIMEOUT_MS });
    return fromRawConnect(payload);
  },
  // 后端把"轮询直到 CLI OAuth 完成"整个收进这一个 hold-open 请求，前端只发一次，最长等 10 分钟，
  // 直接拿到最终的 connected/connect_failed——不再需要自己维护 setTimeout 轮询循环
  // （CliAuthModal.tsx 旧版 authComplete 那套）。多步授权时每一步都要再调一次，带上新的 stepIndex。
  waitAuth: async (name: string, stepIndex: number): Promise<ConnectorConnectResponse> => {
    const payload = await webRequest<RawConnectResponse>(
      'mcp.wait_auth',
      { name, step_index: stepIndex },
      { timeoutMs: WAIT_AUTH_TIMEOUT_MS },
    );
    return fromRawConnect(payload);
  },
  disconnect: (name: string) =>
    webRequest<{ type: 'disconnected'; name: string; applied: boolean; item: Record<string, unknown> }>(
      'mcp.disconnect',
      { name },
    ),
  // 2026-08-17：deleteCustom（mcp.delete_custom，彻底删除自定义 MCP）曾随 UI 入口一起删除。
  // 2026-08-19 用户明确要求恢复：详情页断联态"卸载"按钮要走真删除，不能只是再调一次 disconnect
  // （那样"卸载"这个文案就是假的，实际还是解绑）。响应结构对齐 §5.7：deleted + applied +
  // item{name,removed,was_connected}，跟 disconnect 是同一个响应形状家族，只是 type 值不同。
  deleteCustom: (name: string) =>
    webRequest<{ type: 'deleted'; name: string; applied: boolean; item: Record<string, unknown> }>(
      'mcp.delete_custom',
      { name },
    ),
  registerCustom: async (params: {
    name: string;
    transport: 'stdio' | 'sse' | 'http' | 'streamable-http';
    command?: string;
    args?: string[];
    env?: Record<string, string>;
    url?: string;
    headers?: Record<string, string>;
    timeoutS?: number;
  }) => {
    const payload = await webRequest<RawConnectResponse>(
      'mcp.register_custom',
      {
        name: params.name,
        transport: params.transport,
        command: params.command,
        args: params.args,
        env: params.env,
        url: params.url,
        headers: params.headers,
        timeout_s: params.timeoutS,
      },
      { timeoutMs: REGISTER_CUSTOM_TIMEOUT_MS },
    );
    return fromRawConnect(payload);
  },
  saveCredentials: (name: string, tokens: Record<string, string>) =>
    webRequest<{ type: 'credentials_saved'; name: string; saved_keys: string[] }>('mcp.save_credentials', {
      name,
      tokens,
    }),
};
