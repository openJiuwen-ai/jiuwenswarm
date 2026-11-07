# Gateway 包存储与数据目录设计方案

## 1. 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | feature |
| 编号 / slug | `F_09 / package-store-and-data` |
| 建档日期 | 2026-11-05 |
| 计划条目 | [EXTV2-GW-08](../designs/extensions-v2-development-plan.md) |
| 整体设计 | [应用级界面能力设计概览](../designs/application-surfaces-overview.md) |
| 方案成熟度 | 已确认，已实现 |
| 关联规约 | [S_01 运行时](../specs/S_01_gateway-extension-runtime.md)、[S_06 浏览器桥接](../specs/S_06_local-browser-bridge.md) |
| 验证摘要 | 单元验证通过（2026-11-05）；在途调用与激活校验修订于 2026-11-07，单元验证通过 |

## 2. 背景、目标与范围

### 2.1 背景与问题

Gateway 只有进程内的 `UiStateStore`，进程重启即丢失。agent-core 已提供按包隔离的 `JsonStore`、
`configure_store` 绑定以及 `ops.store.get/set/delete/keys`，AgentServer 已经接入，Gateway 尚未接入。
外观类插件还需要保存用户导入的较大文件（如主题包解压后的图片、视频、页面），并让其中经插件校验的部分
能像包内素材一样被前端读取；JSON 存储不适合承载这类文件。

### 2.2 目标与关键场景

| 场景 | 预期行为 |
| --- | --- |
| Gateway 包调用 `ops.store.*` | 按调用包隔离持久化；Gateway 重启、包重载与升级后保留 |
| 包调用 `ops.data.root()` | 得到本包专属的可写目录 |
| 包将校验后的文件放入数据目录的 `public/` 并调用 `ops.ui.asset_url(path, source="data")` | 得到当前激活代次下的只读地址，可经素材路由读取 |
| 卸载包 | 存储与数据目录一并删除 |

### 2.3 本次范围与非目标

范围：Gateway 侧的存储装配、`extensions.gateway.data_dir` 配置、`ops.data.root()`、数据素材地址与读取、卸载清理。

非目标：导出与备份（由插件基于存储和数据目录自行实现）；在途调用计数；AgentServer 侧存储语义不变。

## 3. 设计约束、假设与待决问题

- Gateway 只依赖 agent-core 与本模块，不 import `jiuwenswarm.extensions_v2`；因此不复用 AgentServer 的
  `resolve_store_dir`，由 Gateway 配置自行解析。
- 平台已确认“卸载即删除”：卸载时删除包的存储与数据目录，插件通过导出与备份弥补（见设计概览第 4 节）。
- 包身份来自 Handler 的 `package_metadata`，插件不能指定其他包。
- 无待决问题。

## 4. 总体方案与设计取舍

### 4.1 方案概览

Gateway 运行时在 `ops.build` 之前把 agent-core 的 `JsonStore` 绑定到自己的事件总线。agent-core 的
`ExtensionEventBus` 构造时已注册 `store.*` 事件类型与 `ops.store`、`ops.package` 命名空间，绑定后 Gateway 包即获得
与 AgentServer 相同的 `ops.store.*`。
另提供 `ops.data.root()` 返回包数据目录；`public/` 子目录内的文件可通过 `ops.ui.asset_url(path, source="data")`
得到素材地址。卸载时先清理存储与数据目录，再删除安装副本。

```text
<extensions.gateway.data_dir>（默认 <用户工作区>/extension/gateway）
├── store/<package_id>.json          JsonStore（含 .lock 旁路锁）
└── packages/<package_id>/           ops.data.root()
    └── public/                      经插件校验后对前端发布的文件
```

### 4.2 关键决策

#### D-01 复用 agent-core 的存储与 Ops，不另建接口

`ops.store.*` 由 agent-core 的 `handler_ops.store` 注册，经 `store.*` 事件链执行，USER Handler 可按 Core 契约观察。
Gateway 只负责目录、绑定与清理。

#### D-02 Gateway 使用独立的数据根目录

Gateway 与 AgentServer 是两个宿主、两套实例；独立目录使清理责任明确，不依赖对方的配置解析。
包 ID 在两侧不重复，即便配置成同一目录也不会冲突，但默认分开。

#### D-03 数据素材使用独立的代次标记

数据素材地址为 `/extension-assets/<package_id>/<generation>.data/<path>`。`.data` 后缀使其与包内素材
`/extension-assets/<package_id>/<generation>/<path>` 在路由层面不可混淆，不占用包内任何文件名；
沿用素材路由的代次校验、沙箱响应头与 `app_web` 转发，无需新增路由。

#### D-04 卸载时先清理数据，再删除安装副本

清理失败时返回失败报告且保留安装副本，再次执行卸载可以重试清理；重载、升级与停机不清理。

#### D-05 数据调用绑定激活，卸载遇在途调用即返回忙（2026-11-07 修订）

PR #10763 检视指出：卸载只注销 Handler，卸载前派生的任务仍可在清理后调用 `ops.store.set()`，重新建出存储文件。
修订后：

- 运行时记录每个 Handler 调用拿到的 Ops 属于哪一次激活。存储调用经 agent-core `configure_store` 的
  `begin_calls` / `end_calls` 钩子进入运行时，`ops.data.root()` 走同一计数；Ops 所属激活不是该包的当前激活时拒绝，
  返回 `EVENTS_OPS_INVALID`。
- 卸载先检查当前激活的在途 Handler 与该包的在途数据调用，非零即返回 `EXTENSION_PACKAGE_BUSY`，什么都不改；
  关闭开场需要等待，因此注销前再检查一次。检查与注销之间没有等待，释放之后不会再有该激活的数据调用被接受。
- 已释放的激活不再开始新的 Handler 调用：释放前已收集该 Handler 的事件链直接交给下一个 Handler。

与 agent-core 的差别：agent-core 只接受仍在运行的 Handler 的 Ops（Handler 返回即失效）。Gateway 包在渲染“进行中”
页面后把导入、恢复等长任务放到后台，任务里仍要读写存储，因此 Gateway 以激活为界，而不以单次 Handler 为界。

### 4.3 替代方案与排除理由

| 方案 | 排除理由 |
| --- | --- |
| R-01 把文件编码后放入 `JsonStore` | 单包上限 4 MiB，且大文件反复整体读写，无法按需读取或分段传输 |
| R-02 数据素材使用新路由前缀 | 需要同步修改 `app_web` 转发与前端约定，没有额外收益 |
| R-03 在包内素材路径中保留 `data/` 等前缀 | 占用包作者可能使用的目录名，存在冲突 |
| R-04 卸载时保留数据 | 与已确认的平台语义不一致 |
| R-05 卸载时先注销，再限时等待在途调用结束 | 超时后包已注销但数据与副本仍在，状态难以说明；卡住的 Handler 还会拖住管理操作 |
| R-06 Ops 只在 Handler 运行期间有效（agent-core 规则） | Handler 返回后的后台任务无法保存结果，见 D-05 |

## 5. 详细设计

### 5.1 模块责任

| 模块 | 修改 |
| --- | --- |
| `config.py` | `GatewayExtensionConfig.data_dir`，读取 `extensions.gateway.data_dir`，默认 `extension/gateway`，相对路径锚定用户工作区 |
| `bootstrap.py` | 注册 `ops.data` 的 `ops.build` BUILTIN |
| `runtime.py` | 启动时 `configure_store`（总线已含 agent-core 的 `store`、`package` 能力，并传入数据调用计数钩子）；停机时 `unconfigure_store`；`is_active_package()`、`package_data_dir()`、`cleanup_package_data()`；Ops 与激活的对应、`package_data_call()`、`require_idle()` 与 `stop_package()`（D-05） |
| `handler_ops/data_ops.py` | `ops.data.root()`：校验调用包处于活动状态且 Ops 属于当前激活，创建并返回包数据目录 |
| `webui/bridge.py` | `ops.ui.asset_url(path, *, source="package")` 支持 `source="data"`；`asset_file` 解析 `.data` 代次标记 |
| `management/registry.py` | 卸载时调用 `cleanup_package_data`，失败则保留安装副本并返回失败；有在途调用时返回 `EXTENSION_PACKAGE_BUSY`（D-05） |

### 5.2 接口定义

```python
# 由 agent-core 提供（Gateway 新接入）
await ops.store.get(key, default=None) -> Any
await ops.store.set(key, value) -> bool
await ops.store.delete(key) -> bool
await ops.store.keys() -> list[str]

# Gateway 新增
await ops.data.root() -> pathlib.Path
await ops.ui.asset_url(path, *, source="package") -> str   # source: "package" | "data"
```

- `ops.data.root()` 只对活动包可用；目录不存在时创建。包数据目录或其上级若为符号链接则拒绝。
- `source="data"` 时，`path` 相对于 `ops.data.root() / "public"`，须为其中的普通文件；越界、目录、
  通过符号链接指向目录外的文件均被拒绝。

### 5.3 生命周期

| 操作 | 存储 | 数据目录 |
| --- | --- | --- |
| 重载、升级（安装更高版本） | 保留 | 保留 |
| Gateway 停机与重启 | 保留 | 保留 |
| 卸载 | 删除 `<package_id>.json` | 删除 `packages/<package_id>/` |

## 6. 影响面与实施设计

修改点按第 5.1 节；同步 [S_01](../specs/S_01_gateway-extension-runtime.md)（配置键、Ops、卸载语义）与
[S_06](../specs/S_06_local-browser-bridge.md)（数据素材地址）。用户文档本次无变化。

## 7. 兼容与迁移

未声明使用存储的包不受影响；新增配置键有默认值。既有 `UiStateStore` 保持不变。

## 8. 验证设计、执行记录与结论

### 8.1 验证设计与通过条件

| 编号 | 场景 | 通过条件 |
| --- | --- | --- |
| F_09-V-01 | Handler 内 `ops.store.set/get/keys/delete` | 读写一致，落盘于 `store/<package_id>.json` |
| F_09-V-02 | 新运行时、重载后读取 | 值仍在 |
| F_09-V-03 | 两个包写同名键 | 互不可见 |
| F_09-V-04 | `ops.data.root()` | 返回 `packages/<package_id>`，可写 |
| F_09-V-05 | `asset_url(source="data")` 与读取 | 地址带 `.data` 代次标记，可读取 `public/` 内文件，响应头同包内素材 |
| F_09-V-06 | `public/` 外文件、符号链接越界、非法 `source` | 拒绝 |
| F_09-V-07 | 经管理门面卸载 | 存储与数据目录被删除；重载不删除 |
| F_09-V-08 | 既有 Gateway 扩展用例 | 全部通过 |
| F_09-V-09 | Handler 运行中或数据调用在途时卸载 | 返回 `EXTENSION_PACKAGE_BUSY`，包仍活动；调用结束后卸载成功，存储不再出现 |
| F_09-V-10 | 卸载或重载后，用旧 Ops 调用存储与 `ops.data.root()` | 被拒绝，文件不被重新创建；激活未变时后台任务可继续使用 |

### 8.2 实际执行与证据

环境同 [F_08](F_08_application-packages-and-assets.md) 第 8.2 节。用例位于
`tests/unit_tests/gateway/extensions_v2/test_gateway_package_data.py`：

| 编号 | 用例 | 结果 |
| --- | --- | --- |
| F_09-V-01、V-02、V-03 | `test_store_is_per_package_and_survives_reload_and_restart` | 通过 |
| F_09-V-04、V-05、V-06 | `test_data_root_and_data_assets`、`test_symlinked_package_data_directory_is_refused` | 通过 |
| F_09-V-07 | `test_reload_keeps_and_uninstall_removes_package_data`、`test_failed_data_cleanup_keeps_the_installed_copy_for_retry` | 通过 |
| 配置 | `test_data_dir_is_configurable_and_defaults_under_the_workspace` | 通过 |
| F_09-V-08 | `tests/unit_tests/gateway/extensions_v2/` 全部用例 | 331 通过 |

新增 6 个用例在恢复修改前的 Gateway 源码后全部失败。

2026-11-07 修订（D-05）：

| 编号 | 用例 | 结果 |
| --- | --- | --- |
| F_09-V-09 | `test_uninstall_is_busy_while_a_handler_of_the_package_runs`、`test_uninstall_is_busy_while_a_detached_data_call_runs` | 通过 |
| F_09-V-10 | `test_ops_of_a_finished_handler_cannot_write_data_after_uninstall`、`test_detached_ops_work_until_their_activation_is_replaced`、`test_a_chain_collected_before_uninstall_skips_the_released_handler` | 通过 |

修改前前两类场景可以复现：Handler 在途时卸载返回 SUCCESS，之后旧 Ops 写入成功并重新建出存储文件。

实现中确认：agent-core 的 `ExtensionEventBus` 构造时已注册 `store.*` 事件类型与 `ops.store`、`ops.package`
命名空间，Gateway 只需绑定 `JsonStore`，无需重复注册。

### 8.3 验证结论

单元范围内，第 2.2 节场景全部成立；真实 Gateway 进程中的存储读写与卸载随后续条目联调。

## 9. 风险与已知遗留

### 9.2 已知遗留

- 在途调用计数已于 2026-11-07 实现（D-05）。仍不覆盖的是包用 `ops.data.root()` 返回的路径直接读写文件：
  卸载时若包的后台线程仍在写文件，可能留下残余文件；包应在自己的任务里收尾。
