---
id: B_20261124_1602_issue-4327-observability
title: issue 4327：统一可观测性配置并分离轨迹存储与导出
type: B
author: hh0205
author_email: huanghao206@huawei.com
date: 2026-11-24
designs: []
design_note: 依据 GitCode jiuwenswarm issue 4327，统一 observability 开关并分离轨迹存储、演进采集和可观测性导出的职责，无独立 D 文档。
modules:
  - jiuwenswarm/common
  - jiuwenswarm/agents/harness
  - jiuwenswarm/observability
  - jiuwenswarm/resources
files:
  - jiuwenswarm/common/observability_config.py
  - jiuwenswarm/common/config.py
  - jiuwenswarm/agents/harness/observability_runtime.py
  - jiuwenswarm/agents/harness/agent_observability.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/observability/runtime.py
  - jiuwenswarm/resources/config.yaml
  - tests/unit_tests/common/test_observability_config.py
  - tests/unit_tests/agentserver/test_observability_hot_reload.py
  - tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py
  - tests/unit_tests/agentserver/test_debug_trace.py
  - tests/unit_tests/agentserver/test_ttse_plugin_runtime.py
  - docs/en/DebugTrace.md
  - docs/zh/调试追踪.md
  - pyproject.toml
  - uv.lock
  - docker/Dockerfile.claw
  - docker/openshift/Dockerfile.claw
---

# issue 4327：统一可观测性配置并分离轨迹存储与导出

## 问题与目标

[issue #4327](https://gitcode.com/openJiuwen/jiuwenswarm/issues/4327) 涉及可观测性配置的职责划分：确认 `telemetry` 的使用位置，将 Agent 和 Team 的可观测性配置统一，并保证 `trajectory_ui` 只控制前端轨迹数据的生成，不因打开轨迹诊断而额外启用可观测性导出。

Agent 和 Team 在同一进程中共用 OpenTelemetry provider 和 exporter。分别配置两套 exporter，实际只有先初始化的配置生效；分别保留两个导出开关，也容易让用户误以为它们可以独立控制同一进程的导出行为。

轨迹 UI 和演进同样需要 provider 产生 span，但它们消费的是进程内轨迹数据。如果把“需要采集 span”直接当成“需要导出 span”，用户只打开 UI 或演进，后台也会写 JSONL 或向 OTLP/Langfuse 发送数据。反过来，关闭导出时直接关闭 provider，又会使 UI 和演进失去数据。

方案分别管理三个意图：是否采集 span、是否向可观测性后端导出、是否把前端轨迹写入 SQLite。

## 配置职责

### telemetry 保留独立用途

`telemetry` 由 agent-gateway 的 `agent_gateway.app_gateway` 消费，经 `configure_gateway_telemetry(full_cfg)` 配置 Gateway 的请求观测。Gateway 默认读取数据目录下的 `infra/config/config.yaml`，可由 `JIUWENSWARM_GATEWAY_CONFIG_DIR` 覆盖；本地启动只有在独立 Gateway 配置不存在时才回退到 Swarm 配置。它使用独立 provider，不能因为 jiuwenswarm 包内没有直接读者就认定配置无用。两份部署模板说明实际配置来源及 `OTEL_ENABLED` 的优先级，避免用户修改了未被 Gateway 加载的文件。

### Agent 与 Team 共用 observability

配置统一为：

```yaml
observability:
  enabled: true
  exporter: file
  endpoint: http://localhost:4317
  service_name: jiuwenswarm
```

模板的 `observability.enabled` 默认 `true`，统一控制同一 AgentServer 进程中的导出。显式提供 `observability` mapping 时，省略 `enabled` 仍沿用默认开启；整个段缺失、为 null 或非 mapping 时则视为关闭。否则，跳过模板迁移的最小配置会被解析成空 mapping，再默认启动 OTLP，意外向 `localhost:4317` 发送数据。解析器将“未配置”和“已配置但省略字段”分开处理，保留模板默认值及旧配置迁移规则。

Code 是已有的编程模式，复用 Agent 的生命周期，没有独立的第三个开关或 owner。

`common/observability_config.py` 集中解析配置。旧的 `agent_observability`、`team_observability` 和 `observability.agent/team` 开关迁移到统一配置；显式填写的新配置优先。旧开关折叠时保留任一开启的意图。共享字段冲突优先保留用户修改值，其次考虑启用的一方，仍无法区分时取 Team。告警只记录字段和取值来源，不输出凭据。

模板迁移使用同一规则；跳过模板迁移的环境在读取时通过内存副本兼容旧配置。

### trajectory_ui 负责本地轨迹数据

`trajectory_ui.enabled` 与 `trajectory_ui.diagnosis_enabled` 任一为真时，共享 record processor 将 span 记录交给 SQLite sink，供前端查询。UI 的采集需求不申请 exporter 导出；两者均为 false 时停止写库组件。

## 实现方案与原因

### 将 provider 需求和 exporter 需求分开

Agent、Team 在同步配置时分别计算：

- `export_wanted`：统一 observability 开关是否开启；Agent 还保留显式 debug force 的导出需求。
- `want_enabled`：需要导出、UI 需要轨迹、或演进/插件需要采集，满足任一项就保留 provider。

通过 Core 的 `acquire_observability(config, export=export_wanted)` 申请资源。只有 UI、Skill evolution、Symphony experience、TTSE 或插件采集需求时，传入 `export=False`。provider 继续生成 span，轨迹处理器正常工作，导出闸门保持关闭。

这使关闭 observability 只停止导出，不会拆掉演进或 UI 所需的 provider；关闭 UI 也只停止 SQLite 写入，不会影响已启用的导出。底层导出闸门由 Core 提供，Swarm 通过公开接口表达需求。`pyproject.toml` 与 `uv.lock` 已锁定包含这些 API 的 Core `31171fb02eb0a03c717b6f558291df274becd1dd`，不存在等待升级依赖后才能生效的步骤。

`docker/Dockerfile.claw` 与 `docker/openshift/Dockerfile.claw` 的 `AGENT_CORE_COMMIT` 默认值同步指向该提交。容器构建通过此参数显式安装 Core，保留旧值会绕过项目依赖锁定，使镜像缺少 `export=` API。既有 `test_dependency_and_container_defaults_pin_the_same_core_commit` 同时约束依赖声明、锁文件和两份容器默认值，防止源码与容器运行版本漂移。

演进通过 `TrajectorySpanProcessor` 订阅数据，不消费 JSONL 或远端导出结果，因此拉起 provider 的目的就是采集；需要在 Langfuse 或 JSONL 查看演进过程时，另行打开 `observability.enabled`。

显式 `debug_trace.<mode>.otel_enabled` force 沿用既有行为，同时申请采集与导出；本地文本调试落盘不要求 observability 后端。在线 RL 自带的 exporter override 也保留其独立意图。不能把演进采集、debug force 和外部 exporter 都等同为 UI 的仅采集需求。debug 导出意图不在单次请求结束时撤销，以免进程级闸门误伤并发请求；按 trace 的隔离策略属于后续范围。

`agent_observability.py` 同时将 `LANGFUSE_*` 常量导入改为 `exporters.langfuse`。基线 Core `0b1aa21` 的 `semconv.py` 已不存在这些常量，因此这是纠正既存导入错误，并非升级本次依赖才新增的兼容要求。

### 统一配置策略，保留各自的资源生命周期

统一开关必须对 Swarm 已持有的全部需求生效。如果只修改当前入口的 demand，曾使用过但暂时空闲的另一入口会保留旧导出意图，进程级 OR 闸门仍可能保持开启。

`observability_runtime.py` 记录成功 acquire 的 Swarm owner 及独立 debug override。Agent 或 Team 任一入口读取配置后，调用 Core 公开 `set_export_demand` 更新这些 owner 的导出需求。申请成功后才记录 owner，释放成功后移除，避免失败请求留下虚假状态，或已退出的入口被后续配置同步重新激活。

provider 的占用仍由各入口 acquire/release 管理。更新导出策略不会强行初始化空闲模式，也不会清空 Core 全局 demand。外部直接初始化的 provider 保留自己的导出意图和生命周期；debug force 保留既有的粘滞语义。

Agent、Team、插件 lease 和退出路径共用一把 Swarm `RLock`，配置读取也在锁内。这样可以避免两个入口并发读取不同配置后，由迟到的旧值覆盖新策略。span 结束时仍走 Core 的无锁闸门读取，不需要获取这把 Swarm 生命周期锁。

### 根据 UI 配置停止共享写库组件

SQLite sink 是进程共享组件，Agent 和 Team 都可以使用。关闭全局 UI 配置时，`sync_trajectory_runtime` 统一执行 unregister、drain、close，清理旧 UI demand，使之后的新会话不再写库。仅在查询 API 返回 disabled 无法停止后台保存数据。

正常退出某个入口时，`shutdown_trajectory_runtime(demand=...)` 仍按 owner 释放资源。只退出 Team 时，Agent 仍在使用的 sink 会保留，反向同理。这区分了“全局不允许再写入”和“某一个使用方不再需要资源”。

停止时允许已入队记录排空，保留已有轨迹文件；重新开启 UI 时重新注册写库组件。不通过删除历史数据库来模拟开关关闭。

## 各开关组合的行为

下表的 UI 表示两个 UI 字段解析后的有效值，不包含 debug force 或外部 provider 等独立导出需求。

| observability | UI | 演进采集 | provider | exporter 导出 | SQLite 写入 |
| --- | --- | --- | --- | --- | --- |
| 关 | 关 | 关 | 无其他需求时不启动 | 无 | 无 |
| 关 | 开 | 关 | 启动 | 无 | 有 |
| 关 | 关 | 开 | 启动 | 无 | 无 |
| 关 | 开 | 开 | 启动 | 无 | 有 |
| 开 | 关 | 任意 | 启动 | 有 | 无 |
| 开 | 开 | 任意 | 启动 | 有 | 有 |

修改配置后，在任意模式的下一次请求同步时应用到共享组件。无需再次访问另一种模式，但仅保存文件不会同步停止没有请求的空闲进程。

## 未采用的方案

- 从 provider 启动条件中删除 UI 或演进需求：会使这些功能收不到 span。
- 关闭 observability 时直接 shutdown provider：会同时中断 UI、演进或插件采集。
- 只更新当前入口的导出需求：无法让统一开关约束另一入口保存的旧状态。
- Agent 与 Team 相互调用同步函数：增加循环依赖、重复初始化和交叉锁风险。
- 关闭时清空 Core 所有 demand：会破坏不属于 Swarm 配置的独立导出意图。
- 只隐藏轨迹查询入口或删除旧文件：不能证明后台已停止写入，还可能损坏历史数据。
- 将 sampler 从采集阶段移到 exporter：会改变 UI、演进的采样契约，不属于本次职责分离。历史 SDD-0025 §9 R5 记录的“仅存储模式”缺口由采集需求与导出需求分开表达来解决，采样位置保持不变。

## 验证

环境为 Windows、Python 3.11.9，Core 为本地 `5cf464bd9`，使用现有 venv 和 PYTHONPATH 加载源码。原始验证的 Swarm 基线为 `b94f4ca12`；候选对齐到 `445e130fe` 后，重新运行下述定向、配置测试及上游 Team 注册、停止流程测试，合计 **503 passed**，日志为 `logs/issue4327-rebase-tests.log`。依赖合并保留上游 computer-use `c0e29a0f` 与包含导出闸门的 Core `31171fb0`，`uv lock --check` 通过。依赖锁定版本的相关代码已另行比对，发布包未单独运行。

定向测试覆盖配置迁移、Agent/Team wiring、真实导出和 SQLite、debug、插件、失败 acquire、资源释放及并发同步，合计 **181 passed**。其中实际文件管线测试使用真实 SDK provider、配置生成的 file exporter 和 SQLite sink，独立读取最终文件和表行数，未用闸门布尔值代替行为证据。未配置 observability 的用例还验证：没有任何需求时不创建 exporter；UI 或演进仍能采集但不导出；显式 debug force 仍能导出。

配置模板、资源配置、脚本、AgentOS 迁移和配置读写测试另有 **229 passed**，日志为 `logs/issue4327-review-config-tests.log`。依赖配置锁定的 Core `da30a02e` 与本地验证版本在 observability、Agent/Team observability 和 trajectory 目录的代码一致。

容器依赖一致性用例在修复前复现失败，两个 Dockerfile 默认版本对齐后，该用例与 `tests/unit_tests/test_release_dependencies.py` 合计 **53 passed**，日志为 `logs/issue4327-ut-pin-targeted.log`。扩测整个 Symphony 配置文件与发布依赖测试得到 81 项通过、7 项失败；这 7 项均为 Windows 路径包含反斜杠而断言要求 `/symphony/runtime` 后缀，不将该扩测写成全通过，也未在本次修改这些无关断言。未实际构建容器镜像。

磁盘 YAML 实验覆盖 Agent、Team、Code 三种入口、有无演进采集，以及全开、仅关导出、仅关 UI、全关和重新开启六个阶段，**36 个检查点全部通过**。关闭导出后 SQLite 继续写入；关闭 UI 后导出继续工作；两者均关闭时没有新导出和新轨迹库，演进开启时仍能通过真实订阅收到 span。

Runner 实验使用脚本模型传输和真实工具：仅开启 Agent 演进时，Skill rail 捕获 3 个 LLM/tool span 并保存 1 条演进轨迹，导出和 SQLite 均为 0；仅开启 Team UI 时，真实 Team root 与 DeepAgent 生成 7 条 SQLite 记录，导出为 0。

本地验证入口与产物：

- `tests/unit_tests/agentserver/test_observability_hot_reload.py`：实际导出、写库和生命周期用例。
- `logs/issue4327-review-regression.log`：定向回归测试结果。
- `.tmp/issue4327_hot_reload_fixed.py` 及 `.tmp/issue4327-hot-reload-fixed/20261124-155933/results.json`：磁盘配置实验与 36 项结果。
- `.tmp/issue4327-bugfix-runner/`：Runner、工具执行和演进轨迹结果。

上述 logs 和 .tmp 路径相对本地工作区 `D:/openjiuwen-extension`。完整测试过程独立保存在转测报告中，本文说明解决方案及其验证范围。

## 兼容性与边界

- 导出策略仍按进程生效，debug force 可以允许同一 provider 上的 span 导出。本方案没有实现每 trace 的独立策略，也没有改变 span 跨开关切换时的判断时机。
- `sample_rate` 和脱敏仍在采集阶段生效，会影响 UI 与演进看到的数据。
- provider 已存在时沿用首次创建的 exporter；切换 exporter 类型、endpoint 等配置不等同于切换 enabled，本方案不承诺在线重建 exporter。
- UI 关闭允许在途队列排空，不删除历史文件；彻底关闭写入需两个 UI 字段均为 false。
- 分布式 `config.team.distributed.*.yaml` 没有新增 UI 段；部署仍需按各进程需要配置观测，不能将一个进程的策略视为跨进程全局策略。
- 扩展到整个 `tests/unit_tests/observability/` 的既有验证曾得到 152 项通过及 2 项 Windows 失败（retention fixture 换行和 sweep 路径），同一失败在基线可复现；上述定向通过不表示该整目录已全部通过。
- 前后端已重新构建，5173 页面、18092 Front RPC 与 19000 Gateway 握手正常。真实远端模型调用已进入 LLM 阶段，但 90 秒内未返回最终答复，不能视为真实模型会话通过。未验证远端 OTLP/Langfuse 后端；Runner 的 Team 实验不代表完整团队编排验证。

配置模板和中英文 Configuration、DebugTrace 文档同步说明统一开关、采集与导出的关系、UI 生效条件及配置应用时机。本文统一维护 issue #4327 的解决方案；[早期配置统一记录](../../features/F_20261111_1528_unified-observability-config.md)保留两级开关阶段的历史决策及验证，不作为当前配置契约。
