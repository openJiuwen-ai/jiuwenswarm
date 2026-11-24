# 转换与开发流程

## 1. 确认目标源码

优先使用用户指定的仓库目录，不切换分支，不覆盖用户修改。只为缺少且本次需要的仓库获取源码。

| 仓库 | 固定分支 | 源码 | 主要阅读位置 |
| --- | --- | --- | --- |
| JiuwenSwarm | `develop` | [openJiuwen/jiuwenswarm](https://gitcode.com/openJiuwen/jiuwenswarm/tree/develop) | `<SWARM_ROOT>/jiuwenswarm/extensions_v2/` |
| agent-core | `develop` | [openJiuwen/agent-core](https://gitcode.com/openJiuwen/agent-core/tree/develop) | `<CORE_ROOT>/openjiuwen/core/events/`、`openjiuwen/core/extension/` |
| agent-gateway | `develop` | [openJiuwen/agent-gateway](https://gitcode.com/openJiuwen/agent-gateway/tree/develop) | `<GATEWAY_ROOT>/agent_gateway/extensions_v2/` |

三个仓库都读取 `develop`。Swarm 和 Core 优先从 GitCode 获取，失败后再使用 GitHub 的 `develop`；Gateway 使用上表中的独立 GitCode 仓库。需要 clone 时按 [系统插件开发说明](system-plugin-development.md#1-读取目标平台源码) 使用浅克隆、部分克隆和 sparse-checkout，只取当前 domain 所需目录及适用规则文件。

远端都无法获取时：源码或 Wheel 环境可以读取当前运行解释器中已安装的对应模块；EXE、DMG 请用户提供源码目录或可访问地址，不搜索 `_internal` 或二进制内容。

先读取仓库根和目标模块的 `AGENTS.md`，再按事件名、Ops、槽位和业务关键词搜索命中片段。接口和能力以目标运行版本的实现为准。

## 2. 建立转换映射

不要翻译 Cordis API 名称，要把原插件的行为映射到 JiuwenSwarm 公开扩展点：

| DSH 能力 | JiuwenSwarm 中需要寻找的对应能力 |
| --- | --- |
| `apply(ctx)` 注册和生命周期 | `create_extension()`、`register(registrar)`、系统插件加载与卸载 |
| Context 事件或服务 | 已登记事件、Matcher、Handler 链和 Ops |
| tool / command | 已有业务事件或公开 Ops；没有真实触发入口时不能伪造 |
| profile 或 session 状态 | 系统插件实例、会话隔离状态、宿主已有持久化能力 |
| Host RPC | 对应 domain 的事件、Ops 或 UI 上下行协议 |
| Client 页面、slot、widget | Gateway 已支持的 UI contribution、槽位和组件 |
| 定时任务、进程、网络 | 宿主已有调度、Shell、网络或审批能力 |
| MCP bundle | 目标环境已有 MCP 接入与工具注册能力 |

映射表至少记录：

| 原能力 | 目标事件或 Ops | domain | Handler 行为 | UI 槽位 | 验证入口 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |

结论只有三类：直接支持、可用现有能力等价实现、缺少必要能力。最后一类如果影响核心功能，停止开发并向用户说明；只影响非核心体验时采用最佳现有实现。

## 3. 系统插件结构

以目标 Core 和宿主当前解析器为准，具体开发约束和自带示例见 [系统插件开发说明](system-plugin-development.md#2-开发独立的系统插件)。常见结构如下：

```text
package-root/
├── manifest.json
├── README.md
└── package_module/
    ├── __init__.py
    └── extension.py
```

核对要求：

- ZIP 根目录直接包含唯一 `manifest.json`，`schemaVersion` 为目标版本支持的值，`package_type` 为 `extension`。
- `package.domain` 只能使用目标宿主支持的 `agentserver` 或 `gateway`；不要因为原插件有 Web UI 就默认选错域。
- `package.id` 稳定，版本使用三段非负整数。入口文件和 `extensions[].module` 对应真实路径。
- 使用顶层 `display` 提供名称、说明和必要详情；依赖只声明实际需要的系统插件包。
- Handler 使用公开 Registrar，默认 USER 层；系统插件身份由平台注入，不自行构造可信元数据。
- 按契约决定是否调用 `next_handler(event)`，继续和短路都返回合法结果，默认业务不能被重复执行。
- 状态按会话或业务实例隔离，不保存本次调用的 Ops 或 `next_handler` 供以后使用。
- UI 复用已有槽位、组件和样式 token，检查窄窗口、长文案、目标会话和活跃实例。

## 4. 测试和打包

覆盖以下范围：

- manifest、入口、真实加载、Handler 发布和系统插件身份。
- 匹配与未匹配、继续与短路、错误、取消、重复调用、会话隔离和资源清理。
- UI contribution、上行操作、下行渲染、页面重挂载和 reload。
- 外部服务或 MCP 使用可控替身，但不能绕过插件加载和 Handler 链。

实现完成后使用 [最终验证脚本](../scripts/finalize_package.py) 一次性运行本插件测试并生成 ZIP。检查压缩包根目录、manifest、版本、README、许可证声明和依赖，使用最终 ZIP 做隔离加载测试，后续安装同一份文件。只运行新系统插件自己的测试，不运行三个平台仓库的完整测试集。

## 5. 安装和界面验收

通过现有 `jiuwenswarm extension` 命令安装，不扫描端口、不读取认证信息、不另写管理客户端。检查退出码、原始 Report、系统插件 ID、版本、generation 和安装目录；结果未知时只查询状态，不重复安装。

首次安装后的修改同步到安装报告返回的副本，再执行 reload；当前验收循环中不 uninstall、不重新 install、不递增版本。涉及界面时，每次确认已激活后使用提问卡片让用户判断实际效果，直到用户明确确认。

用户以后再次要求修改时，复制已交付源码，保持 `package.id`、domain 和展示名称，递增版本后开发新的 ZIP，让旧版本源码和产物继续保留。
