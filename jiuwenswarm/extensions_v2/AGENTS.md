# Extensions V2 开发与文档维护规范

本目录用于 JiuwenSwarm V2 扩展机制的建设及其设计、规约、特性记录和开发计划维护。
通用开发与文档要求采用 [JiuwenSwarm 包公共规范](../AGENTS.md)，本文仅补充模块职责和维护入口；
当前行为通过实现与测试核实，已确认契约由主责规约维护。两者不一致时，按
[包级同步流程](../AGENTS.md#特性设计与契约同步)处理，并区分目标设计与已实现能力。

## 适用范围与规则入口

- 本文适用于 `jiuwenswarm/extensions_v2/` 及其全部子目录。先遵循
  [仓库根规则](../../AGENTS.md)和 [JiuwenSwarm 包公共规范](../AGENTS.md)，再执行本模块补充要求，
  同时遵守适用的 `AGENTS.local.md`。
- 本模块关注 V2 扩展机制的公共抽象、装配与生命周期，以及同宿主和业务扩展的职责边界。
  具体能力、代码归属与依赖方向须经设计明确；业务模块负责所属业务逻辑，不将业务实现直接耦合进扩展基础设施。
- 当前目录已建立分类目录、[设计概览](docs/designs/extensions-v2-overview.md)、
  [详细架构](docs/designs/extensions-v2-architecture.md)、包管理与 WebUI 特性方案及
  [门面规约](docs/specs/S_01_extension-management-facade.md)，以及
  [内置事件开发模板](docs/designs/builtin-event-development-guide.md)。Registry 门面、AgentServer
  装配、WebUI 本地渲染链路及 Token Weather 示例包已实现并通过单元验证；Gateway/WebClient 跨进程联调、
  产品化 UI 验收及在途计数接入仍待开展，不将目标设计或目录存在视为能力已全部落地。
- 已有扩展实现在 [extensions/](../extensions/)。V2 与已有扩展的共存、复用、适配或替代关系须在设计中明确，
  不以目录名称推断兼容关系，不直接将旧机制的接口、默认行为或限制认定为 V2 契约。
- 修改前阅读已有实现、公开导出、调用方、相关测试和模块设计。跨模块修改核对提供方与消费方，
  明确配置生效范围、装配入口及资源归属，不预设所有 Harness、运行模式和接入端均支持同一能力。

## 内容边界与文档分工

分类与内容分工采用 [包级文档分工](../AGENTS.md#内容边界与文档分工)。本模块入口如下：

| 目录 / 文件 | 用途 | 创建或更新时机 |
| --- | --- | --- |
| [AGENTS.md](AGENTS.md) | 模块规则、职责边界与阅读入口 | 模块职责或维护要求变化时 |
| [docs/designs/](docs/designs/) | 概览、详细架构、开发计划、备选方案和待确认问题 | 整体方案、范围或开发安排变化时 |
| [docs/specs/](docs/specs/) 中的 `S_NN_*.md` | 扩展接口、装配与生命周期语义、边界和不变量 | 新契约形成或已有契约变化时 |
| [docs/features/](docs/features/) 中的 `F_NN_*.md` | 具体特性或重构的方案、决策、实施影响和验证记录 | 实现前建档，开发中补充，落地后保留历史 |

### 主要设计文件

已建立的设计文档如下，当前为设计基线：

- [设计概览](docs/designs/extensions-v2-overview.md)：整体定位、适用范围、宿主装配、业务与 WebUI 流程、职责边界及落地条件。
- [详细架构](docs/designs/extensions-v2-architecture.md)：公共抽象、依赖方向、装配与生命周期、状态隔离、故障责任及待确认事项。
- [特性开发计划](docs/designs/extensions-v2-development-plan.md)：本期特性的拆分、开工条件、依赖、验收要求与进度台账。
- [启动装配与来源](docs/features/F_01_assembly-and-source.md)：单例装配顺序、宿主注册回调、启动激活与停机。
- [包加载与安装存储](docs/features/F_02_control-plane.md)：管理事件复用、安装副本、失败副本补偿与启动选版。
- [包管理门面](docs/features/F_03_registry-facade.md)：Registry 门面、安装、启动加载、卸载、重载、查询与停机。
- [发布与归属](docs/features/F_04_activation-and-attribution.md)：整包发布、可信包身份、重载与在途计数限制。
- [平台事件](docs/features/F_05_platform-events.md)：`ui.render`、上行 `ui.*`、`ops.build` 与 `ops.ui.render`。
- [渲染服务](docs/features/F_06_render-service.md)：校验、revision、实例登记、容量淘汰与下行帧。
- [界面项与交互](docs/features/F_07_ui-contributions.md)：`manifest.json ui` 段、槽位目录与 `webui.dispatch`。
- [前端渲染](docs/features/F_08_frontend-render.md)：前端运行时（协议校验、实例管理、挂载所有权）、槽位宿主、渲染注册表、上行交互与验证记录。
- [门面规约](docs/specs/S_01_extension-management-facade.md)：当前对外契约、不变量、错误码与验证索引。
- [后端管理入口](docs/features/F_09_backend-management-requests.md)：V2 包管理请求、Web `/extension` 命令及实际安装目录返回。
- [失败核对](docs/features/F_10_management-failure-recovery.md)：Core 回滚与没有 Report 时的状态查询。
- [管理串行](docs/features/F_11_management-serialization.md)：根写操作互斥、取消及退出通知。
- [停机撤销](docs/features/F_12_management-shutdown.md)：逆依赖撤销、失败保留及上下文释放。
- [后端包管理协议](docs/specs/S_02_backend-package-management.md)：`extensions.v2.*` 参数、响应及错误边界。
- [内置事件开发模板](docs/designs/builtin-event-development-guide.md)：面向 WebClient、Gateway 和 AgentServer 开发者，说明事件、Handler、Ops 登记与真实业务生效；第 3.7 节区分槽位交互与独立内置请求，列出前端、Gateway 转发和 AgentServer 适配器的完整登记入口，第 4.7 节说明 Gateway 渠道事件的往返链路。新增代码标为待实现模板。
- [系统插件包开发指南](docs/designs/system-plugin-package-guide.md)：面向插件包作者，说明包结构、`manifest.json` 字段写法、`README.md` 生效机制说明与交付前自检。
- [系统插件启用状态](docs/features/F_34_system-plugin-enable.md)：受管 manifest 的可选 `enable`、启用和禁用边界。
- [系统插件还原](docs/features/F_35_system-plugin-recovery.md)：启动期还原记录、独立回环恢复页和恢复边界。

新增内置事件或 Handler 时，先阅读内置事件开发模板，再根据涉及的模块查阅规约与特性方案；需要了解完整协作与生命周期时，再查阅详细架构。

已确认契约在主责 spec 中唯一维护，概览与架构引用该位置；具体方案与历史决策在 feature 中维护。
区分目标方案、已确认待实现契约、已实现行为和待验证前提，设计中的示意代码不代表最终接口或已实现能力。

### 代码入口

- [assembly.py](assembly.py)：AgentServer 进程内唯一装配入口，负责 EventBus、管理门面、WebUI 运行时、启动加载与退出顺序。
- [management/](management/)：包管理门面与本地后端；调用方只依赖 `ExtensionManager`。
- [webui/](webui/)：WebUI 事件、渲染校验、实例登记、下行传输与上行适配。
- [examples/auth_login_demo/](examples/auth_login_demo/)：登录与注册示例包，声明 `package.domain: gateway`，由 Gateway 加载；
  示例源码集中管理，包的运行宿主由 manifest 决定。
- [examples/token_weather_glass/](examples/token_weather_glass/)：上下文用量示例包，验证槽位声明、事件 Handler、
  运行时渲染树和 `ui.ready` 恢复。
- [examples/webui_workbench_v2/](examples/webui_workbench_v2/)：插槽与组件示例包，覆盖应用入口、侧栏、任务区与对话区域。

## 命名与编号

文档命名、编号与日期采用 [包级命名与编号规则](../AGENTS.md#命名与编号)。
本模块的 `docs/specs/` 和 `docs/features/` 分别作为编号作用域，不与已有扩展或其他模块混用编号。

需要开发计划时，计划条目使用 `EXTV2-NN`，与 feature 文件分别编号；在计划与 feature 中明确关联，
不默认编号相同。新建文档前核对已有文件、可追溯历史及已登记预留编号，合并前再次核对冲突。

## 模板与必要内容

模板与填写要求采用 [包级模板与必要内容要求](../AGENTS.md#模板与必要内容)，模板在
[公共模板目录](../docs/templates/)统一维护，不在本模块复制维护。

扩展相关文档须按涉及范围说明以下内容；未涉及的能力无需补造，未确定的行为标为待确认：

- 扩展的身份、公共接口、输入输出与配置来源，以及宿主、扩展管理机制和业务扩展各自的职责。
- 如涉及发现、加载、注册或启停，说明触发入口、执行顺序、依赖约束、重复操作语义及失败后的清理责任。
- 如涉及 Hook、事件或回调，说明注册与注销、调用上下文、执行顺序、异常传播和取消边界；
  与 Core 或其他基础设施交互时，明确使用的实际接口及双方责任，不复制外部模块的契约。
- 如涉及实例、会话、共享注册表或后台任务，说明状态作用域、并发与重入边界，以及资源创建和释放责任。
- 如涉及权限、文件、进程、网络或依赖安装，说明校验与副作用的发生顺序、失败语义及敏感信息处理。
- 如涉及旧扩展迁移，说明适用的扩展类型、导入与装配入口、配置和行为差异、兼容范围及验证方式；
  热更新、故障隔离、自动重试和回滚等能力须有明确设计与实现依据。

## 特性设计与契约同步

设计先于实现、契约同步及验证记录采用
[包级同步流程](../AGENTS.md#特性设计与契约同步)。公共契约变化同步其主责文档，模块专属契约在
`docs/specs/` 维护，具体方案和实施记录在 `docs/features/` 维护。

实现中需要调整已确认架构时，说明修改内容、原因、影响及验证方式，按已有授权和适用规则更新设计；
仅修正过时事实或引用时按公共同步规则处理。不将目标设计、参考示例或已有扩展行为直接写成 V2 实现事实。

涉及已有扩展迁移或宿主接入时，同步核对新旧入口、调用方、公开导出、配置模板、测试及用户文档。
跨模块共享契约保留唯一维护位置，本模块引用相关主责文档，不在多处复制接口定义。

WebUI 组件的完整属性、默认值、嵌套结构与事件以
[S_17 WebUI 组件 Props 权威规约](docs/specs/S_17_webui-component-props.md) 为准。
变更从该文件开始，按其中的功能影响清单同步三端和调用方；后端规则负责执行规范，不另行定义契约。

## 特性认领与进度维护

计划建立、特性认领、进度更新及验收依据采用 [包级进度维护要求](../AGENTS.md#开发计划与进度维护)。
需要计划时，统一在上述 Extensions V2 开发计划入口维护，概览、架构及 feature 引用该入口。
无独立计划时，按包级规则在相关 feature 中明确本特性进度和验收依据的唯一维护位置。

## 模块规则索引与阅读入口

所属及关联模块的阅读入口采用 [包级目录索引](../AGENTS.md#目录与开发入口)。
跨模块修改时读取目标目录的适用规则，不将其他模块的局部要求扩大应用，也不在本文复制公共索引。

核对已有扩展机制时，可从 [公开导出](../extensions/__init__.py)、[管理器](../extensions/manager.py)、
[加载器](../extensions/loader.py)、[注册表](../extensions/registry.py)及 [SDK](../extensions/sdk/)进入。
这些文件用于确认现状与迁移影响，不作为 V2 已确认方案。

## 引用与交付检查

提交及公共交付检查采用 [包级交付要求](../AGENTS.md#引用与交付检查)，模块重点检查如下：

- 检查文档相对路径、章节锚点及引用方，保持本模块设计入口有效；尚未建立的文档仅使用明确标注的建议路径。
- 核对概览、架构、spec、feature 和开发计划中的名称、职责、范围、依赖、状态及证据是否一致。
- 实现变化按 [包级测试要求](../AGENTS.md#测试与验证)验证，按实际影响覆盖装配、生命周期、错误清理、
  状态隔离及迁移兼容行为；只记录实际执行的验证及其限制，不将旧扩展测试通过视为 V2 验证通过。
- 核对 [已有扩展管理测试](../../tests/unit_tests/test_extension_manager.py)及
  [已有扩展测试目录](../../tests/unit_tests/extensions/)中的相关覆盖；V2 测试按实际职责与仓库组织建立，
  不假定已有测试覆盖新增入口。
- 纯文档修改检查内容、链接和差异，无需运行业务测试。分别运行 `git diff --check` 和
  `git diff --cached --check`，并单独检查这两条命令不覆盖的未跟踪新文件；`CLAUDE.md` 仅保留 `@AGENTS.md`。
