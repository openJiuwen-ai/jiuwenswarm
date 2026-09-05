import { webRequest } from './webClient';
import type { WebError } from '../types/websocket';
import type {
  LocalizedText,
  PluginCapabilityRef,
  PluginConnectionState,
  PluginPackageDetail,
  PluginPackageSource,
  PluginPackageSummary,
} from '../types/pluginPackage';
import { normalizeEquipmentIdentity, normalizeEquipmentSource } from '../features/equipmentMarketplace';
import { requestEquipmentList } from '../features/equipmentListRequest';

// 薄封装，照抄 connectorApi.ts / projectRegistryClient.ts 惯例。
// list/show/install/uninstall 对齐 cjh/feature/MCP/专家与插件装备-前端接口_v2.md §3。
// 2026-08-15：toggle（对应 plugin_packages.toggle，全局启用/禁用）已删除——插件不再有这个
// 状态维度，见 state-model-rectification-v2-remove-global-toggle.md。
//
// create 的参数形状也已按文档 §3.3 对齐：{id, name, description, skills}——注意 name/description
// 是纯字符串（不是双语对象），且没有 mcpNames 这个参数（文档里 plugin_packages.create 完全没提
// MCP 绑定）。
//
// 没有单独的 delete 方法：整份文档翻遍也没有 plugin_packages.delete，生命周期只到
// install/uninstall（详见 backend-requests.md 需求20）。产品结论是"我的插件"的删除
// 直接复用 uninstall，见 pluginPackageStore.ts 的 deletePackage 注释。
//
// 2026-08-17 v2 对齐两处关键变化：
// 1. `list` 参数从 `{session_id}` 改成 `{filter?: 'builtin'|'local'}`——注意这里特意跟 MCP 侧
//    mcp.list 保持同一种无连字符拼法（文档原文写的是带连字符的 'built-in'，但用户已确认会去和
//    插件后端同事对齐成跟 MCP 一致的 'builtin'，不按文档原文的拼法实现）。
// 2. `install` 失败时不再只抛一句 error 字符串——§1.6.3 的两阶段安装流程需要从失败响应的
//    `payload.pending_connectors` 里取待连 MCP 名单，webClient 的 WebError 现在会透传 payload
//    （见 types/websocket.ts），这里把 `pending_connectors` 非空的失败包成 PluginInstallPendingError
//    （带 `pendingConnectors: string[]`），调用方据此区分"需要走连接续跑"还是"纯硬失败"；没有
//    pending_connectors 的失败原样透传。
//
// 2026-08-19：后端 `plugin_packages.*` 已实测联调通过（不再是"unknown method"），MOCK FALLBACK
// （原 connectorMarketMock.ts 的 tryReal() 包装）已整个删除——每个方法真实调用失败会如实抛错，
// 不再静默退回内存假数据掩盖问题，跟 connectorApi.ts（2026-08-10 已删过一次同款 mock）保持一致。

/**
 * `plugin_packages.install` 半途失败：包依赖的 connector 还没就绪，后端不落盘、不写
 * installed=true，返回 pending_connectors 名单（v2 §1.6.3）。跟纯硬失败（包不存在等）区分开，
 * 前端据此决定是走连接续跑还是直接展示错误。
 */
export class PluginInstallPendingError extends Error {
  pendingConnectors: string[];
  constructor(message: string, pendingConnectors: string[]) {
    super(message);
    this.name = 'PluginInstallPendingError';
    this.pendingConnectors = pendingConnectors;
  }
}

interface RawPluginPackageSummary {
  id: string;
  packageName?: string;
  displayName: LocalizedText;
  displayDescription: LocalizedText;
  category?: string;
  source?: PluginPackageSource;
  installed?: boolean;
  // v2 §3.1：connection_state 是 snake_case（跟这个接口族其余字段的驼峰写法不一致，但文档
  // 原文就是这么给的，如实照抄，不擅自"统一"成驼峰再要求后端改）。
  connection_state?: PluginConnectionState;
  version?: string;
}

function fromRawSummary(raw: RawPluginPackageSummary): PluginPackageSummary {
  return {
    ...normalizeEquipmentIdentity(raw),
    displayName: raw.displayName,
    displayDescription: raw.displayDescription,
    category: raw.category ?? '',
    source: normalizeEquipmentSource(raw.source, 'local'),
    installed: raw.installed ?? false,
    // 未提供时按"未就绪"兜底（不是像旧 connected 占位那样恒 true）——connectionState 现在是
    // 真实门禁判断依据（installed && connectionState==='connected' 才能发消息，见 v2 §1.3），
    // 数据缺失时宁可让 UI 走"需要连接"分支，也不要在没有真实信号时假装已就绪。
    connectionState: raw.connection_state ?? 'disconnected',
    version: raw.version,
  };
}

interface RawPluginPackageDetail extends RawPluginPackageSummary {
  avatar?: string;
  version?: string;
  details?: string;
  tags: LocalizedText[];
  skills: PluginCapabilityRef[];
  tools: PluginCapabilityRef[];
  rails: PluginCapabilityRef[];
  mcps: PluginCapabilityRef[];
  // v2 §3.2：仅 show 有，未就绪的待连 MCP 名单。
  pending_connectors?: string[];
  // 2026-08-21 后端新增（extension_package_manager.py _build_show_card）：manifest 的
  // quick_inputs 双语示例问法，字段名后端已经是驼峰 quickInputs，不用像 connection_state 那样
  // 转写。
  quickInputs?: LocalizedText[];
}

function fromRawDetail(raw: RawPluginPackageDetail): PluginPackageDetail {
  return {
    ...fromRawSummary(raw),
    avatar: raw.avatar,
    version: raw.version,
    details: raw.details,
    tags: raw.tags ?? [],
    skills: raw.skills ?? [],
    tools: raw.tools ?? [],
    rails: raw.rails ?? [],
    mcps: raw.mcps ?? [],
    pendingConnectors: raw.pending_connectors,
    quickInputs: raw.quickInputs ?? [],
  };
}

/**
 * 从失败的 WebError 里取 v2 §1.6.3 的 pending_connectors（webClient 现在会把失败响应的 payload
 * 透传到 error.payload，见 types/websocket.ts）。取不到（非 install 相关失败、或字段确实没有）
 * 返回 undefined，调用方按普通硬失败处理。
 */
function extractPendingConnectors(error: unknown): string[] | undefined {
  const payload = (error as WebError | undefined)?.payload;
  if (!payload || typeof payload !== 'object') return undefined;
  const pending = (payload as { pending_connectors?: unknown }).pending_connectors;
  return Array.isArray(pending) && pending.length > 0 ? (pending as string[]) : undefined;
}

export const pluginPackagesApi = {
  // v2 §3.1：filter 值跟 mcp.list 保持一致用无连字符的 'builtin'（不是文档原文的 'built-in'，
  // 见文件头注释）；缺省/非法值后端按全量处理。
  list: async (filter?: 'builtin+hub' | 'mine'): Promise<PluginPackageSummary[]> => {
    const payload = await requestEquipmentList<{ packages: RawPluginPackageSummary[] }>(
      webRequest,
      'plugin_packages.list',
      { ...(filter ? { filter } : {}) },
    );
    return payload.packages.map(fromRawSummary);
  },
  show: async (id: string): Promise<PluginPackageDetail> => {
    const payload = await webRequest<{ package: RawPluginPackageDetail }>('plugin_packages.show', { id });
    return fromRawDetail(payload.package);
  },
  // 2026-08-21：后端 create_plugin_package（extension_package_manager.py）新增了 mcps 参数
  // （_require_mcp_names 校验，connector 名称数组，缺省/[] 都视为不挂 MCP）——之前这里没有承载
  // 位，CreatePluginPage.tsx 选的 mcpIds 提交时一直没带上，现在补齐。
  create: (params: { id: string; name: string; description: string; skills: string[]; mcps: string[] }) =>
    webRequest<void>('plugin_packages.create', params),
  // 2026-08-20：用户截图给出的真实接口（后端尚未实现，先按此形状对接）——path 是后端本地
  // 文件系统上的绝对路径（前端通过 features/workspace/localFilePicker.ts 的原生选择/桌面拖拽
  // 拿到，不是浏览器 File 对象）。截图里的 session_id 用户明确要求先不带（2026-08-20 口头确认），
  // 等后端那边定下来要不要这个字段再加回。响应结构未知，暂按 void 处理，等后端 ready 联调时
  // 再按实际返回值调整。
  importLocal: (params: { path: string }) => webRequest<void>('plugin_packages.import_local', params),
  // v2 §1.6.3：失败且带 pending_connectors → 包成 PluginInstallPendingError，让调用方走连接
  // 续跑；不带 pending_connectors 的纯硬失败原样上抛。
  install: async (id: string): Promise<void> => {
    try {
      await webRequest<void>('plugin_packages.install', { id });
    } catch (error) {
      const pendingConnectors = extractPendingConnectors(error);
      if (pendingConnectors) {
        throw new PluginInstallPendingError(error instanceof Error ? error.message : String(error), pendingConnectors);
      }
      throw error;
    }
  },
  // v2 §3.6：包声明了 connector 依赖时，卸载成功仍会带一句 `notice` 引导文案（"本装备依赖的
  // connector 仍保持连接，可在 MCP 管理页断开"），之前这里声明成 Promise<void> 把它丢了。
  uninstall: (id: string): Promise<{ notice?: string }> =>
    webRequest<{ notice?: string }>('plugin_packages.uninstall', { id }),
};
