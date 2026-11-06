# 扩展包管理门面规约

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 | spec |
| 编号 / slug | `S_01 / extension-management-facade` |
| 关联特性 | [F_03](../features/F_03_registry-facade.md) |
| 所属模块 | `jiuwenswarm/extensions_v2/management/registry.py` |
| 最近一次修订日期 | 2026-11-06 |
| 方案状态 | 当前实现契约 |

## 1. 范围与责任

### 1.1 本规约定义

- `ExtensionManager` 的公开方法、参数和返回类型。
- 初始化、启动激活、卸载、重载、查询和停机的可观察行为。
- 宿主对 agent-core 错误的收敛方式。
- 门面与 agent-core `Registry` 的责任边界。

### 1.2 责任边界

| 事实 | 唯一来源 |
| --- | --- |
| manifest 合法性、依赖排序、版本条件 | agent-core 管理事件 |
| 安装副本与版本目录 | agent-core `PackageManager` |
| 生效包与 generation | agent-core `Runtime` |
| Handler 登记与包身份 | agent-core `Runtime.publish` |
| 安装目录、报告路径与等待配置、停机顺序 | `ExtensionManager` |
| 根目录发现、启动选版和跨域保护 | Core `PackageInstallReport` 与 `selection` |

门面不缓存上述事实，也不提供同义数据结构。

## 2. 术语

| 术语 | 含义 |
| --- | --- |
| 生效 | Handler 已写入 EventBus，且 `Runtime` 存在 `ActivePackageRecord` |
| 安装副本 | `<install_dir>/<package_id>/<version>/` 下的包目录 |
| generation | 同一包每次成功安装或重载递增的序号 |
| 未生效副本 | 安装目录中存在、但 `Runtime` 没有生效记录的包 |

## 3. 不变量

1. 同一 `ExtensionManager` 只初始化一次；重复 `init` 不产生第二次注册或构建。
2. `ops.build` 在 `Registry.init` 内只触发一次，门面与装配层不重复触发。
3. 写操作在初始化完成前一律拒绝。
4. 启动激活对每个 `package_id` 最多选择一个版本。
5. 卸载成功或停机完成后，该包 Handler 不再对新调用可见。
6. 停机不删除安装目录。

## 4. 数据类型

### 4.1 `ActivationReport`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `plan_id` | `str` | 本次计划标识；宿主补偿生成的结果为空串 |
| `package_id` | `str` | 包标识 |
| `operation` | `str` | `install` / `reload` / `uninstall` |
| `status` | `"SUCCESS" \| "FAILED"` | 结果状态，无 `SKIPPED` |
| `previous_generation` | `int \| None` | 操作前 generation |
| `current_generation` | `int \| None` | 操作后 generation；卸载后为 `None` |
| `error` | `OperationError \| None` | 失败原因，含 `code`、`message` 和参数 |

### 4.2 `QueryResult`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `active` | `tuple[ActivePackageRecord, ...]` | 当前生效包 |
| `installed` | `tuple[Path, ...]` | 安装目录中的版本目录 |
| `inflight` | `dict[str, int]` | 每个生效包的在途调用数 |
| `published` | `dict[str, tuple[HandlerId, ...]]` | 每个生效包登记的 Handler id |
| `packages` | `tuple[PackageInstallState, ...]` | 共享报告中经过存活判断的包状态 |

### 4.3 `ActivePackageRecord`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `package_id` | `str` | 包标识 |
| `version` | `str` | 生效版本 |
| `root` | `Path` | 生效副本根目录 |
| `generation` | `int` | 当前代号 |
| `requires_packages` | `tuple[PackageDependency, ...]` | 声明的包依赖 |
| `domain` | `str \| None` | 运行域 |

## 5. 接口契约

### 5.1 构造

```python
ExtensionManager(
    install_dir: Path,
    *,
    report_path: Path,
    poll_interval: float = 0.5,
    dependency_timeout: float = 300.0,
    lock_timeout: float = 5.0,
    store_dir: Path | None = None,
    state_dir: Path | None = None,
)
```

`install_dir` 和 `report_path` 展开为绝对路径。报告路径必传，三个时间参数使用秒。AgentServer 固定向 Core 传 `domain="agentserver"`、`allowed_domains=("gateway", "agentserver")`。实现目录仍为 `jiuwenswarm/extensions_v2`，启动入口不变。
传入 `store_dir` 时，构造 `openjiuwen.core.extension.data.JsonStore` 并在初始化时注入；不传则不装配 store。
传入 `state_dir` 时，构造 `openjiuwen.core.extension.data.JsonState` 并在初始化时注入；不传则不装配 state。
AgentServer 始终传入 `resolve_store_dir()` 与 `resolve_state_dir()`；二者读取
`resources/config.yaml` 的 `extensions.store_dir` / `extensions.state_dir`
（缺省分别为 `extension/store`、`extension/state`），相对路径锚定用户工作目录。

### 5.2 初始化

```python
async def init(
    event_bus: ExtensionEventBus,
    *,
    host_version: str | None = None,
    host_event_types: Callable[[ExtensionEventBus], None] | None = None,
    host_handlers: Callable[[ExtensionEventBus], None] | None = None,
    cleanup_callbacks: Sequence[PackageResourceCleanup] = (),
) -> None
```

- 已初始化时直接返回。
- 先调用 `Registry.init`，再 await `open_install_dir` 登记报告与运行锁；不返回包路径列表。
- `host_event_types` 只注册事件类型与 CORE Handler；`host_handlers` 只注册产品 Handler。
- 任一步失败：若 Registry 已绑定则解除上下文，重建空 Registry，并抛出原异常。
- cleanup_callbacks 交给 Core；宿主回调按顺序在 store 删除前执行，失败不能吞掉。
  该接口与成功后的 inactive callback 通知分开，后者保持既有兼容行为。

### 5.3 安装

```python
async def install(paths: Sequence[Path]) -> list[ActivationReport]
```

- 一次触发 `package.install`，`paths` 作为同一批来源。
- 返回顺序与 agent-core 报告顺序一致，不保证与输入顺序一一对应。
- 报告占用、依赖等待、复制和失败清理由 Core 处理；门面不做全目录差集删除。

### 5.4 启动激活

```python
async def load_installed() -> list[ActivationReport]
```

- 只委托 Core.load_installed；Core 从安装根目录完整发现，选择最高合法版本、检查依赖环并稳定排序，最后选择 `agentserver` 的包。
- 已在本地生效的包跳过；非法声明返回失败报告，独立包继续。全部队列为空时返回空列表。
- 同 ID 跨域拒绝；另一域依赖未就绪时按配置等待。

### 5.5 卸载

```python
async def uninstall(package_id: str) -> ActivationReport
```

- 生效包与未生效副本统一委托 `Registry.uninstall`。Core 完成撤销、资源回调、store 数据与安装副本删除。
- 仅全部完成才返回 SUCCESS，并在操作锁外通知 inactive callback；清理失败保留 Core 记录供重试。
- 既无生效记录也无副本：返回 agent-core 的 `EXTENSION_PACKAGE_NOT_INSTALLED`。
- 被依赖或在途时返回 agent-core 的拒绝结果，不删除文件。

### 5.6 重载

```python
async def reload(package_id: str) -> ActivationReport
```

只从安装副本重载，不接受新来源。包未生效时返回 `EXTENSION_PACKAGE_NOT_INSTALLED`。

### 5.7 查询

```python
async def query(package_id: str | None = None) -> QueryResult
```

触发 `package.query`。active/installed 是本地事实，packages 是共享报告快照；指定 ID 时只返回该包，误送其它域会拒绝。无参数时适配器把全部有效成功包 ID 编码为 state.package_ids。

### 5.8 生效包快照

```python
def active_packages() -> tuple[ActivePackageRecord, ...]
```

同步只读，供界面目录等消费者使用；顺序按 `package_id` 排序。

### 5.9 停机

```python
async def shutdown() -> None
```

- 先调用 Registry.begin_shutdown，登记 stopping，拒绝新写入并结束管理任务。随后按依赖方在前的顺序检查并撤销本地 Handler。
- 撤销时保留记录，等待 `cleanup_package_resources(..., reason="shutdown")`，然后移除记录。
- 不删除安装副本或 store 数据。
- 单个包失败记录日志后继续处理独立包；只为成功撤销的包发送退出通知。
- 仍有失败包时抛出 `ExtensionFailure` 并保留门面及上下文，允许查询和再次关闭。
- 全部成功后 Registry.close 释放报告运行锁，再解除 Core 上下文；重新装配使用新 Bus。

store 宿主集成说明与验证见 [F_03](../features/F_03_registry-facade.md#91-store-宿主集成)。

## 6. 错误码

| 错误码 | 触发条件 |
| --- | --- |
| `EXTENSION_SOURCE_INVALID` | 来源不存在、zip 非法或路径逃逸 |
| `EXTENSION_MANIFEST_INVALID` | manifest 字段非法 |
| `EXTENSION_DUPLICATE_PACKAGE_ID` | 同批来源包标识重复 |
| `EXTENSION_DEPENDENCY_NOT_MET` | 依赖缺失或同批依赖失败 |
| `EXTENSION_DEPENDENCY_CYCLE` | 依赖成环 |
| `EXTENSION_HOST_VERSION_TOO_LOW` | 宿主版本不满足 `requires.host` |
| `EXTENSION_INSTALL_COPY_FAILED` | 同版本副本已存在或复制失败 |
| `EXTENSION_LOAD_FAILED` | 模块加载或初始化失败 |
| `EXTENSION_HANDLER_LEVEL_FORBIDDEN` | 包登记 `core` 或其他越级 Handler |
| `EXTENSION_PACKAGE_NOT_INSTALLED` | 包未生效或副本不存在 |
| `EXTENSION_PACKAGE_IN_USE` | 仍被其他生效包依赖 |
| `EXTENSION_PACKAGE_BUSY` | 存在在途调用；当前链路不可达 |

## 7. 并发与部署限制

- 门面锁用于初始化、卸载和停机。安装、启动、重载直接委托 Core，等待依赖时不占门面锁。
- Core 报告登记包操作及目标依赖，重复操作返回忙碌；实际文件提交和发现共用短期报告锁。包退出通知在门面锁外。
- 一个 EventBus 只能绑定一个 Registry；同一进程可以有多个独立 EventBus。
- 安装目录不提供跨进程锁，多 AgentServer 部署需外部串行化。
- agent-core 的 `begin_calls/end_calls` 尚未接入事件调用路径，`inflight` 当前恒为 0。

## 8. 验证索引

| 覆盖范围 | 测试文件 |
| --- | --- |
| 门面生命周期 | `tests/unit_tests/extensions_v2/management/test_registry_facade.py` |
| 发布与归属 | `tests/unit_tests/extensions_v2/management/test_activation_publish.py` |
| 装配顺序 | `tests/unit_tests/extensions_v2/test_assembly.py` |
| 安装目录配置 | `tests/unit_tests/extensions_v2/test_config.py` |
| 管理写操作串行与取消 | `tests/unit_tests/extensions_v2/management/test_management_serialization.py` |
| 停机依赖、失败保留与解绑 | `tests/unit_tests/extensions_v2/management/test_management_shutdown.py` |

## 9. 兼容与变更

- 返回值以 agent-core 公共类型为准；对端新增字段不影响调用方。
- `init` 的 `host_*` 回调必须保持幂等，`Registry` 每次初始化只调用一次。
- 若 agent-core 改为显式 `ops.build`，门面需在 `open_install_dir` 后补一次调用，并同步 F_01 与 F_03。
