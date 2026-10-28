# DeepResearch 原生 MCP 搜索委派

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-20 |
| Refs | [#5552 — [Feature]: deepresearch支持本地和mcp信源](https://gitcode.com/openJiuwen/jiuwenswarm/issues/5552) |
| 范围 | DeepResearch rail/executor/tools、请求级 MCP adapter、注册表只读快照、SDK 依赖 |
| 关联 spec | S_09_mcp-integration.md，N10 |
| 测试基线 | Jiuwen 定向回归 407 passed；配套 Relay 143 passed、31 subtests passed |

## 背景

DeepSearch SDK 已支持原生 `mcp_servers`，但宿主的逐工具筛选、参数权限、撤权和会话隔离
不能因 SDK 重新发现工具而失效。主 Agent 预取正文还会引入第二套材料传递流程。

## 决策

当前请求完成原有 MCP 发现和安装后，adapter 保存运行时来源及工具绑定；DeepResearch
执行窗口创建随机 Bearer 保护的临时回环端点。SDK 自行发现和选择工具，端点逐次重验
宿主权限，最终调用原请求级工具及 worker。真实远程凭据留在宿主。

公共层只新增 `snapshot_for_deepresearch`：在既有锁内原子读取配置、工具缓存、连接参数，
复用存在及启用检查，不启动 worker。授权与端点逻辑放在 DeepResearch 专用模块。

生产者为 Jiuwen，消费者为 Relay runner 和 SDK；沿用 stdin v1 配置帧及 64 KiB 上限，
新增字符串 `MCP_SERVERS_JSON`，runner 映射为 SDK 六个原生字段。无可用来源时不添加该键、
不启动端点，继续原搜索；端点启动失败返回脱敏错误，授权不明确或撤权时拒绝调用。
持久状态只保存首次来源名称和身份摘要；恢复刷新当前凭据并与首次来源取交集。

用途沿用 SDK 的 `type=search`，不根据域名或工具名关键词分类；接入范围仍受宿主准入、
HTTP(S) SSE/Streamable HTTP 传输和认证兼容检查限制。SDK 的模型决定实际调用，连接并
传入配置不保证工具被调用或引用；当前没有“用户明确提到 MCP 才启用”的开关。

SDK 依赖切换到已支持原生 MCP 的 dev 锁定提交；AgentCore 保留宿主所需的 dev-stable，
通过 uv override 处理 SDK 声明版本冲突。未修改 SDK 源码；普通 pip 安装的依赖冲突
不由 uv override 解决。回滚应配套撤回 Jiuwen、Relay 的接线和依赖声明/锁定改动。

## 拒绝的方案

- 主 Agent 代查并搬运正文：增加材料路径，无法让 SDK 原生选择、采集和引用 MCP 工具。
- 直接下传远程地址和凭据：SDK 再次发现可能越过宿主工具集合和后续撤权。
- 域名或 search 关键词白名单：与 SDK 用途规则不一致，会排除兼容来源。
- 修改公共权限引擎或 SDK：影响面更大，现有原生接口与宿主工具已足以完成委派。

## 验证

Jiuwen 验证来源投影、权限/撤权、凭据轮换、恢复隔离、真实临时端点 RPC 与取消清理，
同时回归既有 execute/stream、请求级 MCP 与注册表；2026-10-21 提交前共 407 项通过。
Relay 配套验证原生字段、凭据脱敏及真实 checkpoint 保存恢复，共 143 项与 31 项子测试通过。
两仓 `git diff --check` 通过；Windows 未运行验证。

## 已知遗留

旧 Relay runner 接受新增字段但不映射到 SDK，可能静默不使用 MCP，因此功能交付需两仓配套。
当前 SDK 简版报告走独立搜索，不承诺使用 MCP，不自动切换报告类型。此次提交验证为定向
回归，没有重新执行模型驱动的完整研究报告验收。
