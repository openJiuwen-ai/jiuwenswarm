# 系统插件开发说明

完成 DeepSeek Harness 插件分析和能力映射后，按本文读取目标平台源码、实现系统插件并完成最终验证。只在进入开发阶段后读取本文。

## 1. 读取目标平台源码

Core 是两个运行域共同依赖。`agentserver` 域读取 Core 和 Swarm；`gateway` 域读取 Core 和 Gateway。只有跨域协作或用户明确要求时，才补充第三个仓库，不为了解完整平台同时下载全部源码。

用户给了仓库目录时直接使用，不切换分支、不覆盖修改。没有提供的仓库读取以下 `develop` 分支：

| 仓库 | GitCode | GitHub 回退 | 系统插件入口 |
| --- | --- | --- | --- |
| JiuwenSwarm | [develop](https://gitcode.com/openJiuwen/jiuwenswarm/tree/develop) | [develop](https://github.com/openJiuwen-ai/jiuwenswarm/tree/develop) | `jiuwenswarm/extensions_v2/` |
| agent-core | [develop](https://gitcode.com/openJiuwen/agent-core/tree/develop) | [develop](https://github.com/openJiuwen-ai/agent-core/tree/develop) | `openjiuwen/core/events/`、`openjiuwen/core/extension/` |
| agent-gateway | [develop](https://gitcode.com/openJiuwen/agent-gateway/tree/develop) | — | `agent_gateway/extensions_v2/` |

需要 clone 时，按“仓库地址 + `develop`”建立阅读缓存。同一缓存直接复用，只在缺少目录时追加 sparse-checkout：

```text
git clone --filter=blob:none --depth 1 --single-branch --no-tags --no-checkout --branch develop "<仓库 URL>" "<CACHE_ROOT>"
git -C "<CACHE_ROOT>" sparse-checkout set --cone "<本次需要的目录>"
git -C "<CACHE_ROOT>" checkout develop
```

先读取仓库根和目标模块内的 `AGENTS.md`。然后用一组事件名、Ops、槽位和业务关键词集中搜索，把本轮需要的文件、符号和相邻上下文列成清单后批量读取。不要按一个命中执行一次命令，也不要预先通读整个仓库、`docs/` 或测试目录。出现新的必要依赖时再补一批。

读取时以目标运行环境的实现为准：

- Core 核对 Event、Matcher、Handler 链、Registrar、manifest 和加载生命周期。
- Swarm 核对 AgentServer 事件、Ops、管理适配、业务触发入口和 WebUI 协议。
- Gateway 核对 Gateway 事件、Ops、WebUI 槽位、管理适配和系统插件生命周期。
- 文档、示例和测试只能帮助定位；接口名、参数、返回值和真实触发条件仍要回到源码确认。

远端无法获取时，源码或 Wheel 环境可以读取当前 Swarm 解释器中已安装的对应模块。EXE、DMG 环境请用户提供源码目录或可访问地址，不搜索 `_internal`、安装目录或二进制内容。

## 2. 开发独立的系统插件

在独立目录中创建系统插件，不修改三个平台仓库。需要具体结构时先读取 [自带示例](../assets/package_example/)；示例只说明 manifest、入口和 Handler 的基本关系，事件与 Ops 必须替换为映射表中已经确认的真实能力。

源码至少包含：

```text
package-root/
├── manifest.json
├── README.md
├── CAPABILITY_CHOICES.md
└── package_module/
    ├── __init__.py
    └── extension.py
```

- `manifest.json` 的 ZIP 根目录唯一，`schemaVersion`、`package_type`、`package.domain`、入口和依赖符合目标 Core 解析器。
- `package.id` 保持稳定，版本使用三段非负整数；修改已交付转换结果时复制源码形成新版本，不覆盖旧版。
- 提供 `create_extension()` 和入口对象的 `register(registrar)`，通过公开 Registrar 注册 Handler。
- Handler 按契约决定是否调用 `next_handler(event)`，继续和短路都返回合法结果，默认业务不能重复执行。
- 只调用映射表中确认存在的 Ops；状态按会话或业务实例隔离，不保存本次调用的 Ops 或 `next_handler`。
- UI 只使用目标版本已有的 contribution、槽位、组件和样式 token，核对目标会话、活跃实例、窄窗口和长文案。
- README 说明原插件来源和版本、运行条件、系统插件 ID 与版本、安装、验证、reload、卸载和已知限制。
- `CAPABILITY_CHOICES.md` 只记录最终采用的事件、Ops、UI 槽位、真实触发入口、源码依据和验收方式，不复制平台文档。

原插件依赖外部服务、凭据、MCP、文件、进程或网络时，先确认宿主已有公开能力和授权方式。缺少核心能力时停止开发；不要通过直接调用平台私有对象、增加演示触发按钮或修改宿主源码绕过。

## 3. 编写系统插件自己的测试

实现完成后再统一运行测试，不在每次小改动后启动。只运行本次系统插件新写或修改的测试，不运行 Swarm、Core 或 Gateway 的完整测试集。

测试必须从 `JIUWEN_SYSTEM_PLUGIN_ZIP` 指向的最终候选 ZIP 安装到隔离的临时目录，覆盖本次实际使用的行为：

- manifest、入口、真实加载、Handler 发布和可信系统插件身份；
- 命中与未命中、继续与短路、成功、主要失败、取消、重复调用和会话隔离；
- 状态、资源和外部替身的清理；
- 有界面时验证 contribution、上行操作、下行渲染和 reload，真实视觉效果仍由用户确认。

外部服务可以使用可控替身，但不能绕过系统插件加载、Handler 链或本次核心行为。失败后只重跑失败项及其直接相关测试，修复完成再做一次最终批量验证。

## 4. 一次性测试并生成最终 ZIP

使用当前 Swarm 已有的 Python 或桌面后端执行入口，不为下载的源码新建环境或安装依赖：

```text
"<PYTHON_ENTRY>" "<SKILL_ROOT>/scripts/finalize_package.py" "<系统插件源码目录>" "<输出 ZIP 路径>" "<系统插件测试目录或文件>"
```

脚本完成以下工作：

1. 使用目标 Core 解析器校验 manifest 和入口路径，生成临时 ZIP。
2. 通过 `JIUWEN_SYSTEM_PLUGIN_ZIP` 和 `JIUWEN_SYSTEM_PLUGIN_SOURCE` 把候选产物交给测试。
3. 只运行命令中给出的系统插件测试。
4. 测试全部通过后，把同一 ZIP 发布为最终产物；失败时不生成最终 ZIP。

ZIP 根目录直接包含 `manifest.json`，不多包一层目录。输出已存在时不默认覆盖；用户确认替换后再使用 `--force`。最终 ZIP 验证后如有修改，必须重新运行脚本。

用户明确要求安装时，安装这份最终 ZIP，再查询目标版本和激活状态；涉及页面时完成真实业务触发并让用户确认效果。用户未要求安装时，交付源码、README、测试结果和 ZIP，不默认修改运行环境。
