# 模块总览与包边界

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / overview-and-boundaries |
| 关联模块 | jiuwenswarm/common/（全包） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **common 包的整体结构**：分层依赖方向、`__init__` 导出策略、utils.py 路径与
日志中枢的职责边界。各主题域的内部契约分别由 [[S_02]]–[[S_14]] 承载，本 spec 不重复。

**common 的定位**：Gateway 与 AgentServer 双进程共用的公共基础层。判定"一段代码该不该
进 common"的标准：是否被 gateway / server / agents / channels / extensions 中**两个及以上
层级**消费，且不携带某一层的业务语义。单层私有的逻辑留在单层。

## 不变量

- **N1 依赖方向单向**：common 不 import `jiuwenswarm.gateway` / `jiuwenswarm.server` /
  `jiuwenswarm.agents` / `jiuwenswarm.channels` / `jiuwenswarm.extensions` 任何模块。
  允许的仓库内依赖：openjiuwen / openjiuwen_runtime SDK（e2a 子包除外，见 [[S_02]] N11）
  与标准三方库。跨进程共享逻辑下沉到 common，而不是让 common 反向感知消费者。
- **N2 空 `__init__` 约定**：`common/__init__.py` 保持为空（无聚合导出）；子包
  `__init__` 只做窄门面导出——`schema` 四件（Message / AgentRequest / AgentResponse /
  AgentResponseChunk）、`secrets` 一件（SecretStore）。其余子包（e2a / security /
  thinking / workspace）无门面导出。消费一律全路径 `jiuwenswarm.common.<module>`。
  **不要**在顶层 `__init__` 加聚合导出：那会让"import 即全量初始化"的副作用面失控。
- **N3 utils 的 import 副作用**：`utils.py` 导入即执行 `setup_logger()`。所有服务入口
  都会（直接或间接）先 import utils，因此日志体系在进程内天然就绪；这也意味着 utils
  的新增顶层副作用必须零参数、零外部状态、幂等。
- **N4 路径唯一 owner**：文件系统布局一律走 `utils.get_*` 路径函数族（`get_root_dir` /
  `get_logs_dir` / `get_agent_workspace_dir` / …），其内经 `path_provider` SPI 分发
  （多租户 / 会话重定向，见 [[S_08]]）。散落的 `Path("…")` 硬编码是违规。
- **N5 共享键常量集中**：跨模块约定的上下文字典键集中在 `context_keys.py`（当前仅
  `JIUWENSWARM_CHANNEL_CONTEXT_KEY = "__jiuwenswarm_channel__"`），防字面量漂移；
  新增共享键先进此文件再消费。
- **N6 monkeypatch 与单例的装配点**：进程级副作用（MCP 双 patch、thinking hook、
  MCP registry runtime、外部模型 client 装载、debug_dump 信号、SDK 日志引导、后台清理）
  各自只有一个生产装配点（见「执行路径」表）；装配点之外禁止二次装配。

## utils.py 职责分解（~4040 行，本包最大文件）

utils.py 是**路径管理中枢 + 日志体系装配 + 杂项通用工具**的复合体，按职责分五块：

| 块 | 关键符号 | 说明 |
|---|---|---|
| 路径中枢 | `get_root_dir` / `get_logs_dir` / `get_agent_workspace_dir` 等 `get_*` 族 | `JIUWENSWARM_DATA_DIR`（默认 `~/.jiuwenswarm`）为根的 runtime layout；经 path_provider 分发支持多租户/会话（[[S_08]]） |
| 日志装配 | `setup_logger` / `update_log_levels` / `mask_sensitive` / `install_source_record_masking` | 20MB×20 滚动；源头脱敏装在 `LogRecordFactory` 层（覆盖 openjiuwen / openai / httpx 等第三方 logger），失败仅计数 + 首报 stderr 不阻断（fail-open） |
| 工作区准备 | `prepare_workspace` / `init_user_workspace` | 会话/用户工作区目录初始化 |
| 模型参数修复 | `fix_json_arguments` | `json.loads` → `json_repair` → 手工补引号 的三级修复链 |
| 异步与探测 | `AsyncLRUCache` / `wait_for_tcp_port` / `wait_for_pid_exit` / `normalize_tenant_scope_id` | `normalize_tenant_scope_id` 禁 `__`（防与内部双下划线键冲突） |

**已知遗留**：`AsyncLRUCache` 在文件内定义了两次（约 L3712 与 L3922，后者覆盖前者且语义
更完善——滑动过期 / 可选限制 / touch_if_same），属历史遗留重复；消费方实际拿到的是后者。
清理前先确认无 import 时序依赖。

## 执行路径（进程级副作用的装配点）

| 副作用 | 装配点 | 细节 |
|---|---|---|
| 日志体系 | `import utils`（隐式） | N3 |
| debug_dump（USR1） | `app.py` / `app_gateway` / `app_agentserver` / `channels/web/app_web` 各服务入口 `install_async_dump_handler` | [[S_12]] |
| SDK 文件日志引导 | `app_gateway` / `app_agentserver` 的 `bootstrap_openjiuwen_logging` | workspace ready 后、导入触发 openjiuwen logger 的模块前 |
| 外部模型 client 装载 | `app_gateway` / `app_agentserver` 的 `load_extra_model_clients` | [[S_10]] |
| 后台清理 | `app_gateway` 的 `start_background_cleanup` | [[S_14]] |
| MCP 双 patch | `server/runtime/agent_adapter/interface_deep.py` DeepAdapter 构造 | [[S_09]] |
| MCP registry runtime | `app_agentserver` 启停 | [[S_09]] |
| thinking hook | `app_agentserver` 的 `register_thinking_hook` | [[S_06]] |

## 消费者全景

common 的消费方覆盖全仓：`server/`（app_agentserver、handlers、runtime）、`gateway/`
（app_gateway、message_handler、routing、channel_manager、cron、file_transfer_handler）、
`agents/`（harness、swarm）、`channels/`（web、tui、acp）、`extensions/`（yuanrong、
clawee、agentos）、`packages/jiuwenclaw-ee`（企业版）。其中 `utils`（100+ 文件）、
`local_env_config`（77 文件）、`config`（40+ 文件）是消费面最广的三个模块。

## 修改检查单

- 新增顶层文件 → 同步更新模块 `AGENTS.md` 模块地图 + 判定归属哪个 spec（新主题 = 新
  spec 文件，编号递增）。
- 新增子包 → 必须有子包 spec 承载其不变量；`__init__` 导出策略过 N2。
- 改 utils 路径函数 → 检查 `path_provider` 分发路径与 `tests/unit_tests/common/test_path_provider.py`。
- 在 common 内 import 新依赖 → 过 N1（禁反向依赖五层）；e2a 内另过 [[S_02]] N11。
- 新增进程级副作用 → 过 N6（登记装配点，禁止多点装配）。

## 与其它 spec 的关系

本 spec 是其余 13 份的公共前言：[[S_02]]–[[S_14]] 描述各主题域内部契约，均默认遵守
本文 N1–N6 的包级不变量。
