# JiuwenSwarm Extensions V2 特性开发计划

## 1. 这份计划怎么读和维护

- 计划范围：`jiuwenswarm/extensions_v2/` 负责的 V2 扩展机制——扩展包的装配、声明校验、加载生效与生命周期管理，平台事件（含 WebUI 渲染事件）的定义与触发，扩展UI界面项的发现、挂载与交互，以及 AgentServer 与 Gateway、Web 前端之间的上下行交接。
- 范围之外：agent-core 的 Events 与 Extension 公共机制实现；旧 `jiuwenswarm/extensions/` 机制的改造；Gateway 渠道能力与旧机制迁移；未列入本计划的工具、会话与模型业务功能。Server 特性包含其 Ops / 事件所需的平台业务适配。
- 所属模块：`jiuwenswarm/jiuwenswarm/extensions_v2/`。协作方为 AgentServer 运行时（`jiuwenswarm/server/`）、Gateway WebChannel（`jiuwenswarm/gateway/`）与 Web 前端（`jiuwenswarm/channels/web/frontend/`）。
- 交付分类：既有本地包装载与 WebUI 工作共 8 项，保留历史台账；后端管理接入 4 项，追加 Server 会话工作区、主模型、Git 仓库查询和历史读写 4 项。Gateway 渠道能力与旧机制迁移不在本轮。
- 既有安排：EXTV2-01～08 和 M1～M3 保留原范围与进度。本轮补充 EXTV2-09～12 四项后端接入，并在 EXTV2-09 追加 Web `/extension` 命令；Server 接入条目为 EXTV2-13～16，不新增管理页面。
- 本轮边界：复用 Core 已有的 Registry、热重载与失败候选清理；不增加已发布版本回退，不修改 Events 内部实现，不实现自动在途计数及旧代模块回收。
- 更新日期：2026-10-27。
- 设计依据：[设计概览](extensions-v2-overview.md)、[详细架构](extensions-v2-architecture.md)、agent-core [扩展包加载规约](../../../../../agent-core/openjiuwen/core/extension/docs/specs/S_01_package-loading.md)、agent-core [Events 公共契约](../../../../../agent-core/openjiuwen/core/events/docs/specs/S_01_events-contract.md)。
- 关键口径与待确认项：见第 1.2、1.3 节；未确认前，受影响的工作项不标注完成。
- 术语与全局约束：见第 1.1 节。
- 进度摘要：按第 4 节维护；状态、负责人、关联 PR 与进展说明只在该节登记。

先读本节确认范围和完成口径，再到第 2 节了解特性与验收要求，第 3 节判断开工条件和并行机会，第 4 节确认安排、认领和进度。

- 特性是有明确责任和验收边界的开发单元；开工硬前置只列独立实现必需的基础；交付节点表示若干特性与真实依赖汇合后的可演示结果。
- 新增或调整特性时同步第 2 节定义、第 3 节图和第 4 节表；认领、开发、验证或阻塞变化时更新表中负责人、状态、关联 PR、进展说明和日期。
- 状态统一使用“待开始、进行中、阻塞、待验收、完成”；完成须覆盖约定验收范围并有可追溯证据，子范围或模拟验证通过不自动代表整项完成。
- 代码开发前维护对应 feature 方案；契约变化同步 spec；遵循所属目录的 [AGENTS.md](../../AGENTS.md)。
- 具体维护规范：[特性开发计划模板](../../../docs/templates/feature-development-plan-template.md)。

### 1.1 术语与全局约束

| 术语 | 含义 |
| --- | --- |
| 扩展包 / 包根 | 含 `manifest.yaml` 的目录；V2 安装副本位于 `<install_dir>/<package.id>/<version>/` |
| 扩展单元 | `manifest.extensions` 的一项，指向包根下的一个 `.py` 入口文件 |
| 生效记录 | agent-core 控制面中该包已生效的事实，含版本、包根与 generation |
| Report | 一次管理操作的逐包结果，区分成功、失败与依赖未满足 |
| EX-03 / EX-04 | agent-core Extension 已交付项：Registry 管理入口；整包 Handler 发布与撤销 |
| 事件与 Handler | 一次处理中按 USER、BUILTIN、CORE 顺序执行的处理器链 |
| Ops | Handler 使用平台能力的入口，按命名空间与方法组织，例如 `ops.ui.render` |
| 扩展UI界面项 | 包向平台声明的一项界面内容，含标识、标题与允许挂载的 slot |
| 视图绑定 | 服务端保存的一次面板打开归属记录，用于核对交互来源与更新目标 |
| revision | 服务端为渲染实例分配的版本号，前端只应用更新的一帧 |

全局约束（依据架构与 agent-core 契约）：

- 扩展包只能登记 `user` 与 `builtin` 层级；`core` 由平台通过 `register_event_type` 登记。
- 一个包的 Handler 由管理门面集中登记，登记失败注销本次已登记项；每次业务调用使用固定注册快照，重载只影响切换后的新调用，卸载不等待在途调用。
- 界面内容只能使用平台白名单组件、props、action 与样式 token，不允许自由 HTML、CSS 或包内脚本。
- 渲染实例由会话、slot、包与实例标识四元组定位；revision 在该实例内单调递增。
- 平台只为当前 Handler 调用构建一次 Ops，Handler 不得缓存或跨调用复用。

### 1.2 本轮已定口径

| 主题 | 结论 | 影响条目 |
| --- | --- | --- |
| WebUI 渲染服务归属 | 迁入 `extensions_v2/webui/`，由 V2 模块提供，运行在 AgentServer 进程内 | EXTV2-06、EXTV2-07 |
| 包管理注册表 | 门面直接委托 agent-core `Registry`；控制面与发布不在宿主重建 | EXTV2-03、EXTV2-04 |
| 包身份 | 归属事实以 agent-core 生效记录为准；发布 Handler 时通过 `register_event_handler(..., package_metadata=...)` 写入注册记录，由 Events 在构建 Ops 时注入，不从事件输入或会话上下文读取；扩展的受限注册入口不接收包身份 | EXTV2-04 |
| 清理帧 | 独立下行事件 `webui.render.clear`，只携带目标与原因，不复用带 `mode` 的渲染帧 | EXTV2-06 |
| 界面项声明 | 写在 `manifest.yaml` 的 `ui` 段，字段与 `web.slot.contribution` 的 `UiEntryDeclaration` 同构；`renderer.kind` 在本方案中新增 `webui-tree`，前端需支持该渲染模式；agent-core 只解析已知字段，JiuwenSwarm 从生效记录的包根读取原始 `manifest.yaml` 并完成内容校验 | EXTV2-07 |
| revision 作用域 | 实例内单调；实例由会话、slot、包与实例标识定位，跨包排序由前端槽位模式处理 | EXTV2-06、EXTV2-08 |
| 重连恢复 | 服务端重发各活跃实例最近一帧，revision 不变；不新增同步接口，不要求扩展重新生成 | EXTV2-06 |

### 1.3 待确认项

| 主题 | 待确认内容 | 建议 | 责任方 |
| --- | --- | --- | --- |
| 真实业务触发点 | 回合准备、工具调用或两者 | 先接工具调用前后边界，与 mods 的 tool 事件对齐且演示稳定；回合准备随后接入 | 协调业务模块 |

## 2. 要开发的特性

每个特性按以下格式填写，并与第 3 节节点、第 4 节行一一对应。验收标准写预期；实际执行结果统一写第 4 节进展说明。来源验证点引用架构规则编号与章节，完整要求只在主责特性或对应 feature 文档中维护。

### EXTV2-01 启动装配与包来源

- **目标与范围：** AgentServer 启动时按固定顺序完成装配：准备事件总线、注册产品事件与 Handler、初始化包管理（由其触发一次 `ops.build`）并保存首次成功构建的能力目录、加载配置安装目录中的包；不再扫描和自动安装源码示例。同时保证与旧 `jiuwenswarm/extensions/` 机制互不扫描、互不共用配置。不包含具体事件定义、包管理门面实现与 UI 渲染。
- **责任与契约：** `extensions_v2/assembly.py` 提供唯一入口 `install_extensions(server)`，由 `jiuwenswarm/server/worker/lifecycle.py` 在装配屏障内调用。`extensions_v2/config.py` 读取配置键 `extensions.install_dir`（缺省 `~/.jiuwenswarm/extension_pkgs`），与旧机制的 `extensions.extension_dirs` 分离。装配失败不阻断运行时启动，扩展能力标记为不可用并记录原因。
- **开工硬前置：** 无。包管理门面由替身注入，`openjiuwen.core.events.ExtensionEventBus` 已可用。
- **独立验证入口：** `tests/unit_tests/extensions_v2/test_assembly.py`；旧机制共存扫描用例待补。
- **独立验证条件：** `ExtensionEventBus` 与门面替身即可，不需要 Gateway 与前端。
- **验收标准：**
  1. EXTV2-01-V-01：装配顺序为事件注册先于包管理初始化、`ops.build` 先于任何包安装，替身记录的调用序列符合该顺序。
  2. EXTV2-01-V-02：任一步骤抛错时运行时仍可启动，扩展能力被标记为不可用且原因被记录，不留下半装配状态。
  3. EXTV2-01-V-03：V2 扫描结果不含旧机制目录，旧机制扫描结果不含 V2 安装目录；同根重复路径去重，不按收集顺序覆盖同 ID 包。
- **来源验证点与共享场景：** 架构 EXTV2-A01 与第 4.1.1、4.1.2、10.3 节；主责 EXTV2-01，参与 EXTV2-M1。
- **前端独立验证：** 不适用。
- **联合交付与开放条件：** 作为 EXTV2-M1 的装配前提，不单独开放业务能力。
- **设计索引：** [详细架构](extensions-v2-architecture.md) 第 4.1.1、4.1.2、10.3 节；`jiuwenswarm/server/worker/lifecycle.py`、`jiuwenswarm/resources/config.yaml`。
- **特性文档：** `../features/F_01_assembly-and-source.md`。

### EXTV2-02 包声明与加载控制面接入

- **目标与范围：** 接入 agent-core 包加载控制面：读取并校验 `manifest.yaml`、写入安装副本、按依赖定序执行单包计划、创建候选模块、写入生效记录、判断卸载条件。JiuwenSwarm 不自建 manifest Schema、依赖排序或版本规则。不包含管理门面、总线发布与界面项。
- **责任与契约：** `ExtensionManager` 触发 agent-core 的 `package.install` 管理事件，解析、依赖定序、复制与加载全部由对端完成；宿主只读取 `PackageManager.list_installed()` 并补偿 prepare 失败留下的新副本。错误沿用 `EXTENSION_*` 号段，对上层返回逐包 `ActivationReport`。
- **开工硬前置：** 安装目录解析 `extensions_v2/config.py`（EXTV2-01 的配置子项，已提供）。
- **独立验证入口：** `tests/unit_tests/extensions_v2/management/test_registry_facade.py`。
- **独立验证条件：** 覆盖合法、依赖、冲突与入口逃逸的样例包；不需要总线与前端。
- **验收标准：**
  1. EXTV2-02-V-01：非法 manifest（`schemaVersion`、包标识、版本、入口逃逸、`runtime` 非 `backend`）整包拒绝，不产生安装副本。
  2. EXTV2-02-V-02：同批重复包标识整批失败，已生效同标识返回 `PACKAGE_EXISTS`，依赖缺失或成环分别返回 `DEPENDENCY_NOT_MET`、`DEPENDENCY_CYCLE`，均不部分安装。
  3. EXTV2-02-V-03：安装副本写入 `<install_dir>/<package.id>/<version>/`，已存在的版本目录拒绝覆盖；卸载判断覆盖被依赖（`PACKAGE_IN_USE`）与存在在途调用（`PACKAGE_BUSY`）两种拒绝。
- **来源验证点与共享场景：** agent-core [包加载规约](../../../../../agent-core/openjiuwen/core/extension/docs/specs/S_01_package-loading.md) 第 3、4 节；架构第 4.1.2 节；主责 EXTV2-02，参与 EXTV2-M1。
- **前端独立验证：** 不适用。
- **联合交付与开放条件：** 参与 EXTV2-M1；门面已对接 EX-03 交付的 `Registry`。
- **设计索引：** agent-core `openjiuwen/core/extension/{manifest,selection,ordering,orchestrator,runtime}.py`；架构第 4.1.2 节。
- **特性文档：** `../features/F_02_control-plane.md`。

### EXTV2-03 包管理门面与生命周期

- **目标与范围：** 提供 agent-core `Registry` 上的宿主门面，覆盖初始化、安装、卸载、重载、查询与停机，返回 agent-core 的 `ActivationReport` 与 `QueryResult`。包含安装目录配置、启动选版、失败副本补偿与停机顺序。不包含事件定义与 UI 渲染。
- **责任与契约：** `extensions_v2/management/registry.py` 为门面，方法为 `init(event_bus, *, host_version, host_event_types, host_handlers)`、`install(paths)`、`load_installed()`、`uninstall(package_id)`、`reload(package_id)`、`query(package_id=None)`、`shutdown()`；内部只持有 `Registry`，不缓存 manifest、Handler 或生效记录。
- **开工硬前置：** EXTV2-02。
- **独立验证入口：** `tests/unit_tests/extensions_v2/management/test_registry_facade.py`。
- **独立验证条件：** `ExtensionEventBus`、agent-core `Registry` 与样例包；不需要网络与前端。
- **验收标准：**
  1. EXTV2-03-V-01：五类操作返回逐包结果，成功、失败与依赖未满足可区分，单个包失败不影响同批独立包。
  2. EXTV2-03-V-02：未初始化时拒绝安装并返回可区分错误；卸载被依赖或存在在途调用时立即失败，不等待。
  3. EXTV2-03-V-03：包未生效但存在副本时，卸载删除全部版本目录并返回成功；既无生效记录也无副本时返回 `EXTENSION_PACKAGE_NOT_INSTALLED`。
- **来源验证点与共享场景：** 架构 EXTV2-A01、EXTV2-A09 与第 4.1.3、5.2.4、8.3 节；主责 EXTV2-03，参与 EXTV2-M1、M2。
- **前端独立验证：** 不适用。
- **联合交付与开放条件：** 参与 EXTV2-M1 与 M2；门面生命周期已按 EX-03 交付实现验证。
- **设计索引：** 架构第 4.1.3、5.2.4 节；agent-core `openjiuwen/core/extension/runtime.py`。
- **特性文档：** `../features/F_03_registry-facade.md`。

### EXTV2-04 包归属与 Handler 发布

- **目标与范围：** 候选加载成功后集中登记整包 Handler，登记完成后向业务入口开放；限制包只能登记 `user` 与 `builtin`；发布时把包身份写入 Handler 注册记录，由 Events 在构建 Ops 时注入。包含登记失败时注销本次已登记项。不包含包加载本身。
- **责任与契约：** agent-core `Runtime.prepare` 加载候选，`Runtime.publish` / `replace_published` / `unpublish` 完成整包登记与撤销。扩展入口只接收 `ExtensionRegistrar`，只收集 Handler 声明；`PackageMetadata` 由 `Runtime` 在登记时写入，不由扩展自报。
- **开工硬前置：** EXTV2-03。
- **独立验证入口：** `tests/unit_tests/extensions_v2/management/test_activation_publish.py`。
- **独立验证条件：** `ExtensionEventBus`；样例包分别含合法登记与越级登记。
- **验收标准：**
  1. EXTV2-04-V-01：登记完成后新调用可见本包 Handler，登记前不可见；每次调用使用固定注册快照，重载失败不撤销旧 Handler。
  2. EXTV2-04-V-02：包内登记 `core` 被拒（`EXTENSION_HANDLER_LEVEL_FORBIDDEN`）；登记失败时注销本次已登记项，不残留半套 Handler。
  3. EXTV2-04-V-03：Ops 构建能读到当前 Handler 的包信息，且不采信事件输入或会话上下文中的自报字段。
- **来源验证点与共享场景：** 架构 EXTV2-A03、EXTV2-A05 与第 4.3.1 节；主责 EXTV2-04，参与 EXTV2-M1。
- **前端独立验证：** 不适用。
- **联合交付与开放条件：** 参与 EXTV2-M1；在途计数接入后复核 `PACKAGE_BUSY` 场景。
- **设计索引：** agent-core [包加载规约](../../../../../agent-core/openjiuwen/core/extension/docs/specs/S_01_package-loading.md) 第 4.2 节；架构第 4.3.1 节。
- **特性文档：** `../features/F_04_activation-and-attribution.md`。

### EXTV2-05 平台事件、能力注册与业务触发

- **目标与范围：** 把 `ui.render` 与全部上行 `ui.*` 的事件类型、匹配函数与注册函数归入 `extensions_v2/event_types/webui.py`，渲染帧构造与下发 CORE 归入 `extensions_v2/webui/core_handlers/`；把 `ops.build` 的 BUILTIN Handler 与 `ops.ui.render` 归入 `extensions_v2/handler_ops/ui.py`；在真实业务链路接入触发点，验证 Handler 内和 AgentServer 业务逻辑都能主动更新界面。不包含渲染服务与上行入口实现。
- **责任与契约：** `register_webui_events(bus)` 注册 WebUI 事件，由产品注册函数在装配时调用；`bus` 为 `openjiuwen.core.events.ExtensionEventBus`，每个事件类型有且仅有一个 CORE Handler。Handler 内通过 `ops.ui.render(...)` 发起渲染；AgentServer 业务逻辑在核对上下文和目标后直接构造并触发 `ui.render` 事件。触发方不拼装渲染树、不直接下发消息；触发失败不影响业务主流程。业务触发位置见第 1.3 节。
- **开工硬前置：** EXTV2-01。
- **独立验证入口：** `tests/unit_tests/extensions_v2/webui/test_webui_runtime.py`；真实业务触发用例待补。
- **独立验证条件：** `ExtensionEventBus` 即可验证注册与能力；真实触发需要可运行会话。
- **验收标准：**
  1. EXTV2-05-V-01：注册后事件类型清单包含 `ui.render` 与全部上行事件，每个类型有且仅有一个 CORE Handler。
  2. EXTV2-05-V-02：任意 Handler 内调用 `ops.ui.render` 能进入渲染事件链并到达链尾，返回可区分结果。
  3. EXTV2-05-V-03：真实会话在约定触发点更新界面，前端对应槽位出现内容；触发点抛错时业务主流程不受影响且错误被记录。
- **来源验证点与共享场景：** 架构 EXTV2-A02、EXTV2-A03 与第 4.2、4.3、5.2.1 节；主责 EXTV2-05，参与 EXTV2-M1、M3。
- **前端独立验证：** 不适用。
- **联合交付与开放条件：** 参与 EXTV2-M1；真实业务触发范围由 EXTV2-M3 单独确认。
- **设计索引：** 架构第 4.2、4.3、5.2.1 节；[内置事件开发模板](builtin-event-development-guide.md)第 1.4、3.7、4.7、5.7 节说明真实触发与跨进程接入；`extensions_v2/event_types/webui.py`、`extensions_v2/webui/core_handlers/`、`extensions_v2/handler_ops/ui.py`、`extensions_v2/webui/runtime.py`。
- **特性文档：** `../features/F_05_platform-events.md`。

### EXTV2-06 渲染服务与下行帧

- **目标与范围：** 在 `extensions_v2/webui/` 提供渲染请求校验、视图绑定与 revision 分配、容量淘汰、下行发送，以及实例清理与重连重发。不包含界面项发现与上行交互入口。
- **责任与契约：** `extensions_v2/webui/` 下的 `models`、`schema`、`constants`、`view_registry`、`transport`。下行分为 `webui.render`、独立的 `webui.render.clear` 与 `webui.render.error`；clear 帧只携带目标与原因。视图绑定由会话、slot、包与实例标识定位，revision 在实例内单调。
- **开工硬前置：** EXTV2-05。
- **独立验证入口：** `tests/unit_tests/extensions_v2/webui/test_webui_runtime.py`；重连重发用例待补。
- **独立验证条件：** 传输层使用替身；不需要真实 WebSocket 与前端。
- **验收标准：**
  1. EXTV2-06-V-01：白名单之外的组件、props、token 或超出规模上限的树被整体拒绝，不下发部分内容。
  2. EXTV2-06-V-02：同一实例 revision 单调递增，旧帧被丢弃；超过容量上限时淘汰最久未更新实例并下发清除帧。
  3. EXTV2-06-V-03：会话删除或包卸载只清理目标范围并下发清除帧，其他包与其他会话不受影响。
  4. EXTV2-06-V-04：重连后服务端重发各活跃实例最近一帧且 revision 不变，前端空 store 直接应用，非空时只接受更大的 revision。
- **来源验证点与共享场景：** 架构 EXTV2-A06、EXTV2-A07、EXTV2-A09 与第 4.4、5.2.4、8.3 节；主责 EXTV2-06，参与 EXTV2-M1、M2。
- **前端独立验证：** 不适用（前端解析逻辑归 EXTV2-08）。
- **联合交付与开放条件：** 参与 EXTV2-M1、M2。
- **设计索引：** 架构第 4.4、5.2.4、8.3 节；`extensions_v2/webui/`。
- **特性文档：** `../features/F_06_render-service.md`。

### EXTV2-07 界面项声明、挂载与上行交互

- **目标与范围：** 从生效包派生可用的扩展UI界面项与允许槽位供前端发现；挂载上报按该目录校验，未声明的包或槽位被拒，没有界面贡献时不占位；上行交互进入事件链并返回可区分回执。
- **责任与契约：** `extensions_v2/webui/contributions.py` 从生效记录的包根读取 `manifest.yaml` 的 `ui` 段（`apiVersion` 加 `items`；字段与 `web.slot.contribution` 的 `UiEntryDeclaration` 同构，`renderer.kind` 新增 `webui-tree`），校验 `type`、`id`、`slot`、`order`、`panel`、`localeId`、`renderer` 与 `web.locale` 资源路径，产出可用界面项目录；语义校验全部在本模块完成，不依赖 agent-core 返回该段。`extensions_v2/webui/adapter.py` 是唯一上行入口，回执沿用 `event`、`accepted`、`status`、`code` 字段。
- **开工硬前置：** EXTV2-04、EXTV2-06。
- **独立验证入口：** `tests/unit_tests/extensions_v2/webui/test_webui_adapter.py`、`tests/unit_tests/extensions_v2/webui/test_webui_runtime.py` 与页面路径。
- **独立验证条件：** 至少一个含界面声明的样例包与一个纯后端包。
- **验收标准：**
  1. EXTV2-07-V-01：只有生效包声明的界面项可以挂载；未声明、包已停用或槽位不在声明内均被拒绝。
  2. EXTV2-07-V-02：纯后端包不产生界面项，页面不出现空占位。
  3. EXTV2-07-V-03：`apiVersion` 非 1、`type` 未知、同类型 `id` 重复、`localeId` 悬空、资源路径逃逸或 `renderer.kind` 不受支持时整包拒绝，不发布部分条目。
  4. EXTV2-07-V-04：按压、输入、开关、选择四类交互在活跃实例上返回处理结果；无 Handler 时回执为 `no_handler`，非活跃实例为 `inactive`，两者都不产生副作用。
- **来源验证点与共享场景：** 架构 EXTV2-A06 与第 4.4.1、4.4.2、5.2.2 节；主责 EXTV2-07，参与 EXTV2-M1、M2。
- **前端独立验证：** 槽位宿主在没有可用界面项时不渲染容器。
- **联合交付与开放条件：** 参与 EXTV2-M1、M2；声明来源固定为生效记录的包根，不依赖 agent-core 返回 `ui` 段。
- **设计索引：** 架构第 4.4.1、4.4.2 节；`extensions_v2/webui/contributions.py`、`extensions_v2/webui/adapter.py`。
- **特性文档：** `../features/F_07_ui-contributions.md`。

### EXTV2-08 前端槽位渲染与复杂面板

- **目标与范围：** 前端按界面项与渲染树渲染内容，样式只取平台 token，覆盖加载、空、成功、失败与重复操作状态；同一界面项支持多实例并存、按交互切换详情，内容由 Handler 在运行时生成。不包含服务端校验与下行发送。
- **责任与契约：** `jiuwenswarm/channels/web/frontend/src/features/webui/`。组件、props、action 与样式 token 白名单由服务端与前端共同维护同一份契约，字段保持一致；多实例使用会话、slot、包与实例标识四元组定位。
- **开工硬前置：** EXTV2-07。
- **独立验证入口：** `npm run test:webui-protocol`、`test:webui-runtime`、`test:webui-transport`、`test:webui-renderer`（`channels/web/frontend/tests/webui*.test.mjs`），旧链路回归 `npm run test:webui-store`；页面路径待入口迁移后补充。
- **独立验证条件：** 使用模拟帧即可验证；真实后端可选。
- **验收标准：**
  1. EXTV2-08-V-01：加载、空、成功、失败与重复操作五种状态都有明确呈现。
  2. EXTV2-08-V-02：白名单之外的组件或 token 不被渲染，且不影响同槽位其他实例。
  3. EXTV2-08-V-03：多实例各自独立更新、互不覆盖；交互后内容按 Handler 返回的数据变化。
- **来源验证点与共享场景：** 架构 EXTV2-A06、EXTV2-A07 与第 4.6、6.2 节；主责 EXTV2-08，参与 EXTV2-M1、M2。
- **前端独立验证：** 本项即前端特性，使用模拟帧完成交互验收。
- **联合交付与开放条件：** 参与 EXTV2-M1、M2。
- **设计索引：** 架构第 4.6、6.2 节；现状代码 `jiuwenswarm/channels/web/frontend/src/features/webui/`。
- **特性文档：** `../features/F_08_frontend-render.md`。

### EXTV2-09 后端包管理请求接入

- **目标与范围：** 开放 V2 包查询、安装、卸载和热重载请求，并接入 Web `/extension` 命令；Gateway 只转发，AgentServer 复用已装配的唯一 `ExtensionManager`。
- **责任与契约：** 新方法为 `extensions.v2.query/install/uninstall/reload`，与旧 `extensions.*` 分开；外部安装只接受现有 ZIP 文件的绝对路径，返回 Core Report。安装目录内的解压副本仍由启动加载。见 [S_02](../specs/S_02_backend-package-management.md)。
- **开工硬前置：** 已有 EXTV2-03 门面和 E2A 底座，不依赖本轮其他条目。
- **验收标准：** EXTV2-09-V-01 四类请求到达同一门面；V-02 非法输入及不可用运行时返回关联错误；V-03 热重载成功换代、失败保留旧 Handler；V-04 Gateway 完整交接参数、身份和回执。
- **独立验证：** 后端使用 `tests/unit_tests/extensions_v2/management/test_management_adapter.py`；Web 命令使用请求替身独立验证，并回归既有命令注册表。
- **特性文档：** [F_09](../features/F_09_backend-management-requests.md)。

本次命令工作按下表细化，均归 F_09，不按命令新建 feature；状态与负责人在第 4 节维护。

| 子项 | 工作内容与验收要求 |
| --- | --- |
| EXTV2-09-C01 命令入口 | V-05：`commands.list` 与 Web 注册表同时登记；新建对话无需先发送消息即可使用，裸命令显示帮助，非法参数不进入对话或发管理请求 |
| EXTV2-09-C02 安装命令 | V-06：只接受 ZIP 绝对路径，目录在加载前拒绝；保留空格和 Windows 反斜杠；成功展示 package_id、安装目录、版本及重载用法 |
| EXTV2-09-C03 查询命令 | V-07：支持全量和单包，区分生效记录、仅安装副本与空结果；明确展示 package_id、版本、状态和目录，查询失败不伪装为空 |
| EXTV2-09-C04 卸载命令 | V-08：包 ID 到达卸载接口，按 Report 显示成功或失败，不把请求接收成功视为卸载成功 |
| EXTV2-09-C05 热重载命令 | V-09：按包 ID 从安装副本热切换，展示 package_id 及操作结果或失败原因，不显示内部 generation，不先卸载 |
| EXTV2-09-C06 失败与回归 | V-10：保留结果未知、核对事实和错误；超时不重试；覆盖会话路由、四类命令及既有 `/plan`、`/goal` 回归 |

### EXTV2-10 失败回滚与结果未知核对

- **目标与范围：** 接收 Core 失败 Report，不重复回滚；只有异常导致拿不到 Report 时查询事实，不自动重放操作。
- **责任与契约：** 候选和模块清理由 Core 负责，Swarm 保留原始错误；查询失败不能解释成“未生效”，不开放自建 Plan 的回滚入口。
- **开工硬前置：** 已有 EXTV2-03，可用请求适配器替身独立验证，不依赖前端或本轮其他条目。
- **验收标准：** EXTV2-10-V-01 发布失败进入 Core 回滚且无半套 Handler；V-02 正常失败不补查；V-03 无 Report 时仅查询、不重试；V-04 查询失败及取消不误删原有包和来源。
- **独立验证：** `tests/unit_tests/extensions_v2/management/test_management_recovery.py`。
- **特性文档：** [F_10](../features/F_10_management-failure-recovery.md)。

### EXTV2-11 管理写操作串行

- **目标与范围：** 同一门面串行执行初始化、启动加载、安装、卸载、重载和停机，避免并发修改 Core 记录与安装副本。
- **责任与契约：** 锁只约束根管理操作，不进入 Core 子事件；查询和业务调用不占锁，通知在写操作完成后执行。
- **开工硬前置：** 已有 EXTV2-03，不依赖本轮其他条目。
- **验收标准：** EXTV2-11-V-01 写操作不重叠；V-02 排队取消无副作用；V-03 失败后锁可继续使用；V-04 回调可查询或发起管理请求而不发生锁重入等待。
- **独立验证：** `tests/unit_tests/extensions_v2/management/test_management_serialization.py`。
- **特性文档：** [F_11](../features/F_11_management-serialization.md)。

### EXTV2-12 停机撤销与上下文释放

- **目标与范围：** 依赖方在前撤销 Handler，保留安装副本；只通知已经停止生效的包，全部成功后解除 Core 上下文。
- **责任与契约：** 仅用 Core 公开接口；忙或撤销失败时保留可核对状态，不清空门面并声称成功；调用方先停止业务入口，门面不提供业务 drain。
- **开工硬前置：** 已有 EXTV2-03，不依赖本轮其他条目。
- **验收标准：** EXTV2-12-V-01 依赖链逆序撤销；V-02 BUSY 与失败包保留记录和视图；V-03 成功后解除上下文，重新装配使用新 Bus；V-04 安装副本与独立包不误删。
- **独立验证：** `tests/unit_tests/extensions_v2/management/test_management_shutdown.py`。
- **特性文档：** [F_12](../features/F_12_management-shutdown.md)。

### EXTV2-13 会话工作区查询

- **目标与范围：** 提供 `ops.session.cwd()`、`ops.session.root()`；在现有用量事件真实入口绑定执行目录与项目根快照，供当次 Handler 查询。
- **责任与契约：** [S_03](../specs/S_03_session-workspace-ops.md) 为唯一契约来源；能力和 Build Handler 放在 `handler_ops/`，登记接入 bootstrap，事实采集留在回合宿主入口。
- **开工硬前置：** 已有 EXTV2-01 的装配、EXTV2-05 的 Ops Build 接线与原生回合用量触发点，不依赖本轮管理增强项。
- **验收标准：** [F_13 第 5 节](../features/F_13_session-workspace-ops.md#5-验证设计执行记录与结论) 的 V-01～V-07；目录差异、缺失、非法值、并发与嵌套隔离、真实包装载和现有功能回归均须验证。
- **独立验证：** `tests/unit_tests/extensions_v2/test_session_ops.py`、`test_context_usage.py`；真实模型和网络联调单独记录。
- **前端与联合交付：** 本特性不增加前端入口；Worker 内扩展包可以独立验证，不据此完成 M1～M3。
- **特性文档：** [F_13](../features/F_13_session-workspace-ops.md)，对应 Server 开发计划 PR-01。

### EXTV2-14 会话主模型查询

- **目标与范围：** 提供只读 `ops.session.model()`，优先返回宿主绑定的运行模型快照，缺失时读取当前会话保存的稳定选择；不创建、切换或调用模型。
- **责任与契约：** [S_04](../specs/S_04_session-model-ops.md)；事实来源及验证见 [F_14](../features/F_14_session-model-ops.md)。
- **开工硬前置：** 已有装配与共享 Ops Builder；不依赖项目 Git 查询。
- **验收标准与独立验证：** F_14 的运行快照、保存选择、缺失、白名单、隔离与示例验证；`test_session_model_ops.py` 和原生 Adapter 回归。
- **前端与联合交付：** 无新增前端入口，不据此完成 M1～M3。对应 Server PR-02。

### EXTV2-15 会话 Git 仓库查询

- **目标与范围：** 提供只读 `ops.session.repo()`，由可信 cwd 查询最近 Git 工作树根、当前分支、完整 HEAD 与 detached 状态，支持 linked worktree；不执行 Git 写入。
- **责任与契约：** [S_05](../specs/S_05_session-repo-ops.md)；Ops 登记沿用 session Builder，Git 业务读取位于 `server/runtime/session/session_repo.py`。
- **开工硬前置：** EXTV2-13 的可信 cwd 与共享 session Ops 接线；不以 EXTV2-14 的模型资料为前提。
- **验收标准：** [F_15 第 5 节](../features/F_15_session-repo-ops.md#5-验证设计与执行记录) 的真实子目录、worktree、detached / unborn、缺失、错误、超时、并发隔离、用户包装载与原生入口验证。
- **独立验证：** `tests/unit_tests/extensions_v2/test_session_repo_ops.py`；示例 `examples/session_repo_info/`。
- **前端与联合交付：** 不新增查询 Event、业务 CORE 或前端入口；不据此完成 M1～M3。对应 Server PR-03。

### EXTV2-16 会话历史读写与追加前扩展

- **目标与范围：** 提供只读 `ops.session.messages(limit=50, cursor=None)`、写入 `ops.session.append(message)` 和追加前事件 `session.append`，实现主会话产品历史分页、用户审核 / 正文改写、默认 CORE 落盘后回执；接入原可等待主历史写入和 `history.append_record` RPC。不驱动 Agent、不注入模型上下文，不新增前端入口。
- **责任与契约：** [S_06](../specs/S_06_session-history-ops.md)；事件 / Handler / Ops 登记在 Extensions V2，记录构造、分页及写入服务留在 Server 历史模块。agent-core 不改造，EventBus 只在 Worker。
- **开工硬前置：** 已有 EXTV2-01 装配、EXTV2-05 共享 Ops Builder、宿主绑定的有效 SessionContext 和原历史服务；不依赖 cwd / model / repo 能力。
- **验收标准：** 用户包实际加载并调用两项 Ops；改写 / 显式拒绝 / 真写回执与原入口生效；稳定分页、重试去重与冲突、跨会话 / 包隔离、取消 / 失败 / generation 拒绝；已有历史回归和真实 Worker 链路。范围及证据入口见 [F_16 第 6 节](../features/F_16_session-history-ops.md#6-验收与未决问题)。
- **独立验证：** `tests/unit_tests/extensions_v2/test_session_history_ops.py`；用户示例 `examples/session_history_audit/`。
- **前端与联合交付：** 历史可通过既有读取入口恢复，本次不主动渲染 UI，不据此完成 M1～M3。对应 Server PR-04。

## 3. 特性依赖关系与并行开发

下图覆盖第 2 节全部特性。矩形为特性，六边形为交付节点，分组为工作线；实线表示开工硬前置，虚线表示联合验收参与。

```mermaid
flowchart TB
    subgraph WA["工作线 A 扩展机制基座"]
        direction TB
        A1["EXTV2-01 装配与来源"] --> A2["EXTV2-02 声明与加载"]
        A2 --> A3["EXTV2-03 管理门面"]
        A3 --> A4["EXTV2-04 归属与发布"]
    end

    subgraph WB["工作线 B 事件与 WebUI 服务"]
        direction TB
        B1["EXTV2-05 平台事件与触发"] --> B2["EXTV2-06 渲染服务"]
        B2 --> B3["EXTV2-07 界面项与交互"]
    end

    subgraph WC["工作线 C Web 前端"]
        C1["EXTV2-08 前端渲染"]
    end

    M1{{"M1 最小真实闭环"}}
    M2{{"M2 生命周期与恢复"}}
    M3{{"M3 真实业务触发"}}

    WA ~~~ WB
    WB ~~~ WC

    A1 --> B1
    A1 --> S13["EXTV2-13 会话工作区 Ops"]
    B1 --> S13
    A1 --> S14["EXTV2-14 会话主模型 Ops"]
    B1 --> S14
    S13 --> S15["EXTV2-15 会话 Git 仓库 Ops"]
    A1 --> S16["EXTV2-16 会话历史与追加 Hook"]
    B1 --> S16
    A4 --> B3
    B3 --> C1

    C1 -.->|"A4、B2、B3、C1 汇合"| M1
    B3 -.->|"A3 与清理、恢复子范围"| M2
    B1 -.->|"真实触发子范围"| M3

    classDef item fill:#f8fafc,stroke:#94a3b8,color:#0f172a;
    classDef node fill:#ecfdf5,stroke:#059669,color:#064e3b;
    class A1,A2,A3,A4,B1,B2,B3,C1 item;
    class M1,M2,M3 node;
```

本轮四项只依赖已有门面，可各自实现和验证；共用文件依次修改，不将另一项完成作为功能成立的前提。

```mermaid
flowchart LR
    BASE["已有 ExtensionManager 与 E2A 底座"] --> P9["09 管理请求与 Web 命令"]
    BASE --> P10["10 失败回滚与结果核对"]
    BASE --> P11["11 写操作串行"]
    BASE --> P12["12 停机撤销与上下文释放"]
```

读图要点：

- A 线上的顺序由包加载控制面决定：装配与来源先提供目录与启动上下文，声明与加载再产出候选与生效记录，门面汇总结果，最后发布 Handler。
- B1 依赖 A1 的装配入口；B2、B3 在事件注册之后接入渲染与交互；B3 同时依赖 A4 的生效包目录。
- C1 可用模拟帧先行开发，与 B3 的联调在界面项契约稳定后进行。
- 虚线只标注各节点的汇合入口，参与范围写在标签中：M1 覆盖 A4、B2、B3、C1；M2 覆盖 A3 与清理、恢复子范围；M3 覆盖 B1 的真实触发子范围。

| 工作线 | 条目 | 可并行前提 | 汇合节点 |
| --- | --- | --- | --- |
| A 扩展机制基座 | EXTV2-01 → 02 → 03 → 04 | 01 完成后 02 可独立开工，03、04 依次推进 | M1、M2 |
| B 事件与 WebUI 服务 | EXTV2-05 → 06 → 07 | 05 依赖 01；06 依赖 05；07 依赖 04 与 06 | M1、M2 |
| C Web 前端 | EXTV2-08 | 模拟帧可先行，联调依赖 07 | M1、M2 |
| Server 能力 | EXTV2-13～16 | 13、14、16 依赖已有装配 / Ops 接线；15 依赖 13 的可信 cwd | 各特性独立验收，不据此完成 M1～M3 |

顺序限制：`ops.build` 必须先于任何包安装；候选发布必须先于界面项发现；渲染服务必须先于上行回执校验。软依赖不写入硬前置，只在节点验收中体现。

## 4. 开发计划与进展

下表是唯一的进度台账。状态、负责人、关联 PR 与进展说明只在此处更新；未开工时保持空白，不填写计划日期以外的推断。

| 条目 | 类型 / 分类 | 既有安排 | 验收范围 | 优先级 / 批次 | 工作线 / 节点 | 计划时间 | 状态 | 负责人 | 关联 PR | 进展说明 | 最近更新 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| EXTV2-01 启动装配与包来源 | 特性 / 本期 | 纳入 | 第 2 节范围 | 高 | A / M1 | 未排期 | 进行中 | 李雷 | — | 装配只加载配置安装目录，空目录不补装示例，3 项装配测试通过；旧机制共存扫描待补 | 2026-10-24 |
| EXTV2-02 包声明与安装存储 | 特性 / 本期 | 纳入 | 第 2 节范围 | 高 | A / M1 | 未排期 | 待验收 | 李雷 | — | 管理事件复用与失败副本补偿已实现，单元验证通过 | 2026-10-22 |
| EXTV2-03 包管理门面与生命周期 | 特性 / 本期 | 纳入 | 第 2 节范围 | 高 | A / M1、M2 | 未排期 | 待验收 | 李雷 | — | Registry 门面与生命周期已实现，单元验证通过 | 2026-10-22 |
| EXTV2-04 包归属与 Handler 发布 | 特性 / 本期 | 纳入 | 第 2 节范围 | 高 | A / M1 | 未排期 | 待验收 | 李雷 | — | 整包发布与可信包身份已实现，7 项单测通过；在途计数待对端接线 | 2026-10-22 |
| EXTV2-05 平台事件、能力注册与业务触发 | 特性 / 本期 | 纳入 | 第 2 节范围 | 高 | B / M1、M3 | 未排期 | 进行中 | 李雷 | — | 事件注册与 ops.ui.render 已实现；真实业务触发待接入 | 2026-10-22 |
| EXTV2-06 渲染服务与下行帧 | 特性 / 本期 | 纳入 | 第 2 节范围 | 高 | B / M1、M2 | 未排期 | 进行中 | 李雷 | — | 校验、实例登记与下发已实现，6 项运行时用例通过；重连重发待补 | 2026-10-22 |
| EXTV2-07 界面项声明、挂载与上行交互 | 特性 / 本期 | 纳入 | 第 2 节范围 | 高 | B / M1、M2 | 未排期 | 待验收 | 李雷 | — | 支持组件事件、包与槽位归属校验，以及 Handler 执行前的实例、版本和交互数据校验，见 [F_07](../features/F_07_ui-contributions.md) | 2026-10-30 |
| EXTV2-08 前端槽位渲染与复杂面板 | 特性 / 本期 | 纳入 | 第 2 节范围 | 高 | C / M1、M2 | 未排期 | 进行中 | 李雷 | — | WebUI 运行时底座（协议校验、实例管理、React 宿主与传输适配）已实现，四组模块用例 50 项与旧 store 10 项通过；生产页面入口未接入，加载态与真实联调待补 | 2026-10-30 |
| EXTV2-M1 最小真实闭环 | 节点 / 本期 | 纳入 | EXTV2-04、06、07、08 的约定子范围 | 高 | A、B、C | 未排期 | 待开始 | 李雷 | — | — | — |
| EXTV2-M2 生命周期与恢复闭环 | 节点 / 本期 | 纳入 | EXTV2-03、06、07、08 的约定子范围 | 中 | A、B、C | 未排期 | 待开始 | 李雷 | — | — | — |
| EXTV2-M3 真实业务触发 | 节点 / 本期 | 纳入 | EXTV2-05 的触发子范围 | 中 | B | 未排期 | 待开始 | 李雷 | — | — | — |

节点不计入本期特性分母。子范围或模拟验证通过只记录为对应验收 ID 的证据，不代表整项完成；在途计数与跨进程联调在对端交付后单独登记。

本轮管理接入台账如下，含 EXTV2-09 追加的 Web 命令。负责人和状态只在本节维护，既有条目不因本轮通过而自动完成。

| 条目 | 状态 | 负责人 | 关联 PR | 进展与验证 | 最近更新 |
| --- | --- | --- | --- | --- | --- |
| EXTV2-09 后端包管理请求接入 | 完成 | 李志远 | — | 四类请求及 Web 命令已实现；新建对话可直接执行，每条命令只显示最终结果；后端 181 项、Web 86 项回归及生产打包通过，真实浏览器与部署联调未执行 | 2026-10-24 |
| EXTV2-10 失败回滚与结果未知核对 | 完成 | 李志远 | — | 11 项真实回滚、无 Report 核对、查询及编码异常测试通过 | 2026-10-23 |
| EXTV2-11 管理写操作串行 | 完成 | 李志远 | — | 7 项并发、取消与通知重入测试通过 | 2026-10-23 |
| EXTV2-12 停机撤销与上下文释放 | 完成 | 李志远 | — | 6 项逆依赖、BUSY、失败保留和清理重试测试通过 | 2026-10-23 |
| EXTV2-13 会话工作区查询 | 完成 | 刘瑜 | [!71](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/71) | V2 115 项、原生用量 3 项、无项目工作区 62 项测试通过；独立进程用户包调用链验证通过，见 F_13 | 2026-10-26 |
| EXTV2-14 会话主模型查询 | 完成 | 刘瑜 | [!79](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/79) | 扩展与用量 140 项、模型兼容 116 项测试通过；独立进程 ZIP 安装、用户 Handler 调用及卸载验证通过，见 F_14 / S_04 | 2026-10-26 |
| EXTV2-15 会话 Git 仓库查询 | 完成 | 刘瑜 | [!81](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/81) | 扩展与原生 Adapter 161 项测试通过；独立进程示例 ZIP 安装、普通仓库和 worktree 查询、宿主日志及卸载验证通过，见 F_15 / S_05 | 2026-10-27 |
| EXTV2-16 会话历史读写与追加前扩展 | 完成 | 刘瑜 | [!83](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/83) | 363 项定向回归（历史 Ops 65 项）、74 项模型兼容回归通过；真实 Front/Supervisor/Worker 两包两会话、平台上下文、平台写入不被拒绝、扩展可拒绝、改写/幂等/分页、卸载及进程端口清理通过，见 F_16 / S_06 | 2026-10-28 |

追加命令前，四项后端工作与既有 V2、Gateway E2A 回归合跑共 92 项通过，另有 AgentServer 请求分发回归 21 项通过。这是原后端范围的验证记录，不替代原 M1～M3 的产品验收。环境说明见 F_09。

EXTV2-09 追加命令的进度如下，原后端验证记录继续保留。

| 子项 | 状态 | 负责人 | 进展与验证 |
| --- | --- | --- | --- |
| EXTV2-09-C01 命令入口 | 完成 | 李志远 | 命令清单、注册表与新建对话首次操作通过测试；无需先发消息或创建聊天会话；真实菜单操作未验收 |
| EXTV2-09-C02 安装命令 | 完成 | 李志远 | ZIP 校验、空格路径、中英文输出、非 ZIP 加载前拒绝及返回目录修改后热重载通过 |
| EXTV2-09-C03 查询命令 | 完成 | 李志远 | 全量、单包、生效与未生效副本的 ID、版本、状态、目录，以及空结果和查询失败通过 |
| EXTV2-09-C04 卸载命令 | 完成 | 李志远 | API 映射、Report 成败展示及真实 Gateway 到适配器链路通过 |
| EXTV2-09-C05 热重载命令 | 完成 | 李志远 | API 映射、包 ID 与操作结果展示、失败保留旧 Handler 及安装副本修改生效通过 |
| EXTV2-09-C06 失败与回归 | 完成 | 李志远 | 未知结果和核对错误完整传递，超时不重试；后端 181 项、Web 86 项回归及 Vite 打包通过 |

命令子项按第 2 节的独立验证范围完成；验证命令和限制见 [F_09](../features/F_09_backend-management-requests.md#6-结论与遗留)。
浏览器真实操作和网络部署联调未执行，不纳入本次通过结论，也不据此完成原 M1～M3。

## 附录 A. 迁移与接入安排

现有穿刺实现用于验证链路可行性，不作为契约。下表给出它与目标结构的对应关系，具体实施按第 2 节条目执行。

| 现有位置 | 目标位置 | 改造动作 | 关联条目 |
| --- | --- | --- | --- |
| `server/runtime/webui/bootstrap.py` | `extensions_v2/assembly.py` 与 `management/registry.py` | 拆分职责：装配编排归装配入口，事件注册归事件目录，发布与归属归 agent-core `Runtime`；移除演示任务与 WebUI 模块内的包登记表 | EXTV2-01、04 |
| `server/runtime/webui/event_bus.py` | 删除 | 改用 agent-core `ExtensionEventBus`；只保留事件目录与注册函数 | EXTV2-05 |
| `server/runtime/webui/handlers/` | `extensions_v2/event_types/webui.py`、`extensions_v2/webui/core_handlers/` 与 `handler_ops/ui.py` | 事件定义与匹配归事件目录；依赖 WebUI 运行时的 CORE 归渲染服务；Build Handler 归能力目录，从 `ops.session_context` 读取会话 | EXTV2-05 |
| `server/runtime/webui/` 的模型、校验、常量、实例登记与传输模块 | `extensions_v2/webui/` | 搬迁；实例登记更名视图绑定；清理改为独立 `webui.render.clear` | EXTV2-06 |
| `server/runtime/webui/adapter.py` | `extensions_v2/webui/adapter.py` | 搬迁并补齐挂载校验与回执语义；注册点仍在 `server/agent_ws_server.py` | EXTV2-07 |
| `server/runtime/webui/demo.py` | 删除 | 由样例包与真实触发点替代 | EXTV2-05、08 |
| `server/worker/lifecycle.py` 的装配调用 | 只调用 `install_extensions(server)` | 收敛为单一入口，顺序由装配模块保证 | EXTV2-01 |
| `gateway/channel_manager/web/webui_handlers.py`、`common/schema/message.py`、`gateway/channel_manager/web/web_connect.py` | 保持现状 | 传输层只做白名单转发与负载透传，不新增职责 | — |
| `channels/web/frontend/src/features/webui/` 与页面中的槽位宿主 | 保持现状并增强 | 增加界面项驱动的挂载、状态呈现与样式 token 落地 | EXTV2-08 |
| `jiuwenswarm/extensions/` 旧机制 | 保持现状 | 不迁移、不改配置键、不共用安装目录 | EXTV2-01 |
| 新增 `extensions_v2/config.py`、`management/registry.py`、`webui/contributions.py` | 新建 | 承载安装目录、Registry 门面与界面项目录 | EXTV2-01 至 04、07 |

安装目录约定：配置键 `extensions.install_dir`，缺省 `~/.jiuwenswarm/extension_pkgs`，与旧机制的 `extensions.extension_dirs`（缺省 `jiuwenswarm/extensions`）区分使用。
