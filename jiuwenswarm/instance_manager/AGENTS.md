---
description: jiuwenswarm instance_manager 多实例隔离模块说明书——给修改/检视本模块代码的 AI 助手与工程师：模块定位、统一术语、公开入口、模块地图、架构铁律、测试与提交约束，以及 7 份 spec 与 2 份设计叙事的索引。
---

# instance_manager 多实例隔离

让一台机器同时跑多个互不干扰的 jiuwenswarm 实例：**实例命名与注册**（instances.yaml）、
**端口分配与冲突探测**（千位段平移 + bind+listen 探测 + 整组回退）、**并发启动互斥**
（跨平台文件锁）、**进程身份与状态**（PID 文件 + 存活探测 + 进程控制）、**实例环境注入**
（bootstrap .env 三路径，含零依赖早期引导）。本包是**纯工具层**：不启动服务、不监听端口、
不持有常驻状态；编排者是 `jiuwenswarm-start`（start_services.py），本包只向它输出原子能力。
对仓库内依赖仅 `common.utils` 两个路径函数，不感知 server / gateway / channels 任何上层。

本文件是模块入口索引。**修改任何代码前先读对应 spec**（见「设计文档归档与双向同步」）；
深入细节跳转：

- `docs/specs/S_01_overview-and-boundaries.md` — 模块总览与包边界（子模块划分 / 聚合门面
  45 符号 / 依赖方向 / 私有豁免名单 / 消费者全景 / 已知遗留）
- `docs/specs/S_02_instance-naming-and-data-models.md` — 实例命名与核心数据模型
  （名称四规则 / 5 个保留名 / InstanceConfig·InstanceStatus·InstancesYamlError）
- `docs/specs/S_03_port-allocation-and-conflict-detection.md` — 端口分配与冲突探测
  （base+index×1000 公式 / env 覆盖 / bind+listen 不设 SO_REUSEADDR / ::1 双栈补探 /
  整组回退 / .env 端口持久化——**事故结晶最密集的领域**）
- `docs/specs/S_04_instances-yaml.md` — instances.yaml 配置管理（路径三件套 / 读取校验链 /
  原子写 / index=声明顺序 / 模板）
- `docs/specs/S_05_lock-and-pid.md` — 实例锁与 PID 管理（flock / 'x' 创建 + 30s stale 回收 /
  PID JSON 三字段 / 存活探测双实现）
- `docs/specs/S_06_status-and-process-control.md` — 状态查询与进程控制（具名认 PID、default
  端口探测兜底 / 查询只读停止清理 / taskkill·SIGTERM→SIGKILL 降级序列）
- `docs/specs/S_07_bootstrap-env-and-early-parsing.md` — bootstrap .env 与早期引导
  （三路径 / 两处映射表同步 / 零依赖约束 / None→SystemExit(1) 硬契约）

面向**理解系统**（而非改代码）的叙事文档在 `docs/designs/`：

- `docs/designs/instance_manager_overview.md` — 特性文档：instance_manager 的功能、原理与
  能力边界（概念层面，含术语表与快速上手）
- `docs/designs/architecture_cn.md` — 架构文档：架构总览图 / 端口·锁·状态三大机制 mermaid
  图 / 实例一生端到端时序 / 14 条设计决策表 / 公开 API 面

## 统一术语

```
default 实例（index 0）                具名实例（index 1, 2, 3…）
─────────────────────                ─────────────────────────
无 --name 启动，历史用法               --name alice 启动
workspace: ~/.jiuwenswarm/            workspace: ~/.jiuwenswarm-instances/alice/
端口: 基础端口组                        端口: 基础端口 + index×1000
不进 instances.yaml                    注册在 ~/.jiuwenswarm/instances.yaml
状态: PID 文件 + 端口探测兜底           状态: 只认 PID 文件
```

| 术语 | 含义 |
|---|---|
| **实例（instance）** | 一套独立运行环境 = 独立 workspace + 独立端口组（5 端口）+ 独立进程树 |
| **default 实例** | 不带 `--name` 的隐式实例，占 index 0 与基础端口，行为兼容历史（[[S_02]] N3） |
| **具名实例** | `jiuwenswarm-init --name X` 创建、`--name X` 启动的显式实例 |
| **index** | 端口分配序号：default 恒 0；具名 = instances.yaml **声明顺序** + 1（[[S_04]] N4） |
| **instances.yaml** | 具名实例注册表（`~/.jiuwenswarm/instances.yaml`），原子写（[[S_04]]） |
| **workspace** | 实例数据根：default `~/.jiuwenswarm/`，具名 `~/.jiuwenswarm-instances/<name>/` |
| **端口组 / 千位段平移** | 一实例的 5 端口（agent_server/web/gateway/frontend/http_api）= base + index×1000，同 index 整组平移、跨实例不交叠（[[S_03]] N1/N2） |
| **整组回退** | 冲突时向上扫连续 index，5 端口**全部**可用才整组换，不做单端口回退（[[S_03]] N7） |
| **base 端口覆盖** | `JIUWENSWARM_<TYPE>_PORT` 五个环境变量覆盖分配起点（Docker 用）；坏值静默忽略（[[S_03]] N3） |
| **实例锁** | workspace 内 `.instance.lock`：Unix flock / Windows 排他创建 + 30s stale 回收（[[S_05]]） |
| **PID 文件** | workspace 内 `.instance.pid`：JSON `{pid, started_at, name}`，实例属主凭证，读取宽容（[[S_05]] N5） |
| **bootstrap .env** | workspace 内 `.env`：DATA_DIR + INSTANCE + 五端口变量，`load_dotenv(override=True)` 注入（[[S_07]]） |
| **早期引导** | argparse 之前、任何 jiuwenswarm import 之前的兜底路径，**零 jiuwenswarm 依赖**（[[S_07]] N3） |
| **fail-open / fail-closed** | 探测类（PID 读损坏→None、yaml 收集失败→尽力而为）fail-open 不阻断；互斥类（拿不到锁→放弃启动、bootstrap 装载失败→SystemExit）fail-closed |

## 公开入口（public API）

**`instance_manager/__init__.py` 是聚合门面——与 common 的空 `__init__` 约定相反，这是
本包的有意特例**（终端工具包、无重初始化副作用，聚合降低消费方摩擦，见 [[S_01]] N3 /
设计决策 D14）。消费者一律 `from jiuwenswarm.instance_manager import ...`，聚合导出
**45 个符号**（`__all__` 维护全量清单，按域分组）：

| 域 | 数量 | 符号 |
|---|---|---|
| 数据类 | 3 | `InstanceConfig` / `InstanceStatus` / `InstancesYamlError` |
| 常量 | 7 | `BASE_PORTS` / `PORT_TYPES` / `INSTANCE_NAME_PATTERN` / `RESERVED_NAMES` / `PID_FILENAME` / `LOCK_FILENAME` / `STALE_LOCK_TIMEOUT` |
| 名称校验 | 2 | `validate_instance_name` / `is_valid_instance_name` |
| 端口管理 | 8 | `compute_auto_port` / `calculate_instance_ports` / `is_port_available` / `check_port_conflicts` / `collect_all_ports` / `find_available_ports` / `PORT_ENV_NAMES` / `PORT_ENV_OVERRIDES` |
| yaml 管理 | 8 | `get_instances_yaml_path` / `get_instances_dir` / `get_instance_workspace_path` / `load_instances_yaml` / `save_instances_yaml` / `create_instances_yaml_template` / `update_instances_yaml` / `get_instance_index` |
| 锁与 PID | 6 | `InstanceLock` / `write_pid_file` / `read_pid_file` / `delete_pid_file` / `is_process_alive` / `check_instance_running`（legacy） |
| 状态查询 | 6 | `get_instance_status` / `get_default_instance_status` / `list_all_instances` / `format_status_line` / `get_instance_config` / `load_all_instance_configs` |
| 进程控制 | 2 | `stop_process_by_pid` / `stop_instance_process` |
| bootstrap | 3 | `create_bootstrap_env` / `create_bootstrap_env_for_name`（legacy） / `load_instance_bootstrap_by_name` |

**新增公开符号**：先进子模块，再同步 `__init__.py` 的 import 块与 `__all__`——漏了
`__all__` 聚合入口就静默缺失。

**受控私有豁免**（下划线符号被包外/跨模块消费，登记于 [[S_01]] N4）：`_upsert_env_ports`
（config → start_services，default 实例端口持久化）、`_create_basic_bootstrap_env`
（bootstrap → dotenv_early，早期零依赖兜底）、`_get_system_executable`（config →
lock/status，System32 优先解析）。新增跨模块私有导入必须先登记，否则按违规处理。

## 模块地图

```
instance_manager/
├── __init__.py            # 聚合门面：45 符号按子模块分组 re-export（S_01 N3；docstring 引用的
│                          #   multi-instance-isolation-design.md 未随仓——designs/architecture_cn.md 接棒）
│
├── config.py              # 数据类 + 常量 + 名称校验 + 端口管理
│   ├── InstancesYamlError #    消息前缀 "[instances.yaml] " + 修复建议（S_02 N6）
│   ├── 常量               #    BASE_PORTS 五项 / PORT_TYPES / INSTANCE_NAME_PATTERN / RESERVED_NAMES×5 / PID_FILENAME
│   ├── InstanceConfig     #    __post_init__ expanduser+resolve；派生 PID / .env 路径（S_02）
│   ├── InstanceStatus     #    running=False ⇒ pid/started_at=None（S_02 N5）
│   ├── 名称校验           #    validate_instance_name 四规则单一真源（S_02 N1）
│   ├── PORT_ENV_OVERRIDES #    JIUWENSWARM_<TYPE>_PORT 覆盖 base；坏值忽略（S_03 N3）
│   ├── compute_auto_port  #    base + index×1000；index 0 保留 default（S_03 N1/N2）
│   ├── is_port_available  #    bind+listen 不设 SO_REUSEADDR + ::1 补探（S_03 N4/N5）
│   ├── find_available_ports #  整组上扫回退；scan_range=0 即 None（S_03 N7）
│   ├── PORT_ENV_NAMES     #    五端口 → .env 变量名；http_api 必须下发（S_03 N8）
│   └── _upsert_env_ports  #    default 实例端口幂等写 ~/.jiuwenswarm/config/.env（豁免符号）
│
├── yaml.py                # instances.yaml 管理
│   ├── 路径三件套         #    ~/.jiuwenswarm/instances.yaml + ~/.jiuwenswarm-instances/<name>/（S_04 N1）
│   ├── load_instances_yaml #   宽进严校：空态合法、坏条目必抛 InstancesYamlError（S_04 N2）
│   ├── save_instances_yaml #   原子写 temp+os.replace + 孤儿清理（S_04 N3）
│   ├── update_instances_yaml # 追加/覆盖全量条目 {workspace, ports}（S_04 N5）
│   └── get_instance_index #    声明顺序+1；新名字 len+1（S_04 N4）
│
├── lock.py                # 实例锁与 PID
│   ├── InstanceLock       #    Unix flock 轮询 / Windows 'x' 排他创建 + 30s stale 回收（S_05 N2/N7）
│   ├── write/read/delete_pid_file # JSON 三字段，原子写 / 宽容读（S_05 N5）
│   ├── is_process_alive   #    Windows tasklist /FI / Unix kill(pid,0)（S_05 N6）
│   └── check_instance_running #  legacy 直读接口，冻结（S_05 N8）
│
├── status.py              # 状态与进程控制
│   ├── get_instance_status      # 具名：只认 PID 文件（S_06 N1）
│   ├── get_default_instance_status # default：PID 优先 + 三端口探测兜底 + netstat/lsof 反查（S_06 N2/N7）
│   ├── list_all_instances       # default 优先 + yaml 全量，fail-open（S_06 N5）
│   ├── get/load_instance_config # yaml 条目 → InstanceConfig，端口五键补全（S_06 N3）
│   └── stop_process_by_pid / stop_instance_process # taskkill /T /F 或 SIGTERM→SIGKILL；停后清 PID（S_06 N4/N6）
│
├── bootstrap.py           # bootstrap .env 三路径
│   ├── create_bootstrap_env(_for_name) # 常规五变量全量（S_07 N1）
│   ├── _create_basic_bootstrap_env      # 早期零依赖兜底：静态 base、四变量（S_07 N3/N4；豁免符号）
│   └── load_instance_bootstrap_by_name  # 校验→配置→workspace→load_dotenv(override=True)；失败 None→SystemExit(1)（S_07 N5/N6）
│
└── docs/                  # 设计文档归档（specs/ 7 份 + designs/ 2 份 + 归档规约 AGENTS.md）
```

## 架构铁律

以下 10 条从各 spec 不变量中提炼（全量不变量以 spec 为准，条目可被 review 单条引用）：

1. **纯工具层、依赖单向**（[[S_01]] N1）。本包对仓库内依赖只有 `common.utils` 的
   `get_user_home` / `get_user_workspace_dir` 两个路径函数；不 import server / gateway /
   channels / start_services 任何上层。启动编排逻辑反向进本包 = 破坏分层。本包不启动
   服务、不监听端口、不持有常驻状态。
2. **早期引导零依赖**（[[S_01]] N2 / [[S_07]] N3）。`_create_basic_bootstrap_env` 运行
   在任何 jiuwenswarm import 之前，不得 import 本包任何子模块；home 解析、yaml 直读、
   端口计算全部就地实现，失败兜底 index=1。往早期路径加 import 是最隐蔽的破坏方式。
3. **端口公式唯一**（[[S_03]] N1/N2）。端口 = `_resolved_base_port + index×1000`；
   BASE_PORTS 恰 5 项，index 0 保留 default、具名从 1 起（声明顺序）。改公式 / 改表 =
   全量实例端口漂移，必须先过 spec 修订。
4. **探测必须 bind+listen 且不设 SO_REUSEADDR，IPv4 回环补探 ::1**（[[S_03]] N4/N5）。
   这是两起线上事故（10048 崩溃、strictPort 失败）的结晶：connect 探测漏判僵死监听、
   SO_REUSEADDR 在 Windows 掩盖占用、Vite 只听 `[::1]`。**禁止把探测改回 connect 或
   加回 SO_REUSEADDR**。
5. **冲突回退只做整组**（[[S_03]] N7）。5 端口全部可用才换组；`scan_range=0` 返回 None
   而不是静默扫一个。单端口回退会造成同实例端口撕裂散落。
6. **五端口全量下发、两处映射同步**（[[S_03]] N8 / [[S_07]] N2）。http_api 必须随组下发
   （否则开 HTTP 时回落 8766 隐性撞车）；`config.PORT_ENV_NAMES` 与 bootstrap.py 局部
   `port_env_mapping` 是有意的两份拷贝，改任一处必须同步另一处。
7. **锁先于写、写入必原子**（[[S_04]] N3 / [[S_05]] N1/N2/N7）。对同一实例的 PID 写 /
   yaml 读改写必须在 `InstanceLock` 临界区内；instances.yaml 与 PID 文件一律 temp+rename
   原子落盘；Windows 锁 release 必须删文件，崩溃残留靠 30s stale 回收。绕锁直写 =
   双属主或丢更新。
8. **查询只读、停止清理；具名认 PID、default 有兜底**（[[S_06]] N1/N2/N4）。状态查询
   绝不写删 PID 文件；`.instance.pid` 只由停止路径清理。具名实例不做端口兜底判定
   （端口可能被整组回退改过）；default 兼容无 PID 管理的历史启动，按三端口占用反推。
9. **名称校验单一真源**（[[S_02]] N1/N2）。名称合法性只由 `validate_instance_name`
   判定（模式 + 1-64 + 无前导点 + 5 保留名大小写不敏感）；所有入口（yaml 条目、CLI
   --name）都必须过它，禁止别处重写校验。
10. **错误与失败契约**（[[S_02]] N6 / [[S_07]] N5 / [[S_05]] N5/N6）。`InstancesYamlError`
    消息必带 `[instances.yaml]` 前缀与修复建议；bootstrap 按名装载失败返回 None，CLI
    调用方必须 `SystemExit(1)`（缺 DATA_DIR/端口的进程会跑错实例）；PID 文件读损坏 →
    None 不抛错（凭证不是账本）；存活探测失败按不存活处理。

## 生命周期 / 配置消费

- **进程启动序**（谁在哪儿调用）：CLI 组件早期解析（dotenv_early）→ 零依赖兜底
  `_create_basic_bootstrap_env`（仅当 .env 缺失）→ CLI main 解析 `--name` →
  `load_instance_bootstrap_by_name`（`load_dotenv(override=True)`）→ start_services
  编排（取配置 → 端口检查/整组回退 → 持锁 → 写 PID → 起服务 → 释放锁）。
- **实例生命周期**：init（名称校验 → 建 workspace → yaml 注册 → bootstrap .env）→
  start（如上）→ status（只读）→ stop（taskkill/SIGTERM→SIGKILL → 清 PID 文件）。
- **环境变量**：`JIUWENSWARM_DATA_DIR`（实例数据根，下游 common 路径中枢消费）、
  `JIUWENSWARM_INSTANCE`（实例名）、`JIUWENSWARM_HOME`（仅早期路径的 home 覆盖）、
  `JIUWENSWARM_<TYPE>_PORT`（base 端口覆盖，Docker 用）。
- **文件系统落点**：`~/.jiuwenswarm/instances.yaml`（注册表）、
  `~/.jiuwenswarm-instances/<name>/`（具名 workspace）、workspace 内 `.env` /
  `.instance.pid` / `.instance.lock`、`~/.jiuwenswarm/config/.env`（default 实例端口
  持久化，`_upsert_env_ports`）。

## 测试

pytest.ini 的 addopts 含全仓 coverage，日常跑请清掉（`-o addopts=""`）：

```powershell
# 模块主测试文件（19 个测试类：名称校验/端口分配/端口可用性/PID/锁/回退/env 覆盖/...）
.venv\Scripts\python.exe -m pytest tests/unit_tests/test_instance_manager.py -o addopts=""
```

- 测试路径：`tests/unit_tests/test_instance_manager.py`（直接位于 unit_tests 根，不在
  子目录——历史布局，本包单文件承载全部单测；新增测试进该文件）。
- **改端口探测 / 分配公式时，除单测外必须对照 [[S_03]] 边界 case 清单跑全场景矩阵**：
  活监听 / 僵死监听 / ::1-only / IPv6 禁用 / env 坏值 / scan_range=0 / 整组回退。
- **改锁 / 停止序列时**对照 [[S_05]] / [[S_06]] 边界 case 清单（并发抢锁 / stale 回收 /
  双 release / 已死进程 / SIGTERM 升级）。
- 修复类改动必须附**能复现原故障的测试**（先红后绿）——本包的探测语义多来自真实
  事故，回归测试就是事故档案。

## 代码风格

- 类型注解：PEP 585 内置泛型 + PEP 604 联合类型（`X | None`）；存量 `Optional[X]` /
  `Dict[K,V]` 写法维持现状，不强改（本包面向 3.9+ 兼容形态）。
- 注释 / docstring 英文为主，关键机制处注释写"为什么"而非"是什么"（config.py 的
  SO_REUSEADDR / ::1 注释、yaml.py 的原子写注释都是契约的一部分，清理时勿顺手删除）。
- 日志禁止 `print`；用户可见错误走 `InstancesYamlError` 消息或 logger.error（bootstrap
  装载路径），**不得用异常 traceback 直接糊给用户**。

## 设计文档归档与双向同步（本模块强制）

本模块设计文档**全部**落在 `docs/` 下，命名与结构规约见 [`docs/AGENTS.md`](docs/AGENTS.md)。
把 **`instance_manager/` 代码 + `docs/specs/` + 本 AGENTS.md** 当作一个一致性单元维护——
本包的端口公式、锁语义、yaml 结构是全仓多实例行为的地基，"只改一半消费方"或"改了
公式不改文档"是缺陷的头号来源。

**前置铁律——文档先是设计的输入，才是提交的产出。** 动手改任何代码**之前**，先读相关
`docs/specs/S_NN_*`（现有契约 / 不变量 / 修改检查单），拿它们校准方案；方案定了**当场**
同步修订对应 spec。先码后档、拖到提交前才补文档是把流程做反了。

三条强制约束，提交时必查：

1. **特性更新归档 feature 且三连续提交**：`docs/features/` 新增 `F_NN_<slug>.md`；特性代码
   （`feat(instance-manager)`）、测试（`test(instance-manager)`）、文档
   （`docs(instance-manager)`）拆成三个紧邻提交、同一 PR 落地。bugfix 触及 spec 描述的
   路径/不变量时同样三连（`fix`/`test`/`docs`）；纯缺陷修复归档
   `docs/bugfix/B_NN_<slug>.md` 时合并为**一个** `fix(instance-manager)` 提交。
2. **规约变动必更新 specs**：端口公式 / 保留名集合 / yaml 结构 / 锁超时 / .env 形状 /
   平台分支语义变化时，同步修订对应 `S_NN`（元信息"最近一次修订日期"填当天）；新规约
   主题 = 新 spec 文件。
3. **读到不一致当场修文档**：本文件 / specs 里与代码不符的接口名、常量数、路径，先
   grep 代码、以代码为准刷新文档，同一次改动落地；不把过时表述当新约束执行。点名
   数量的句子（"45 个符号"、"5 个保留名"、"5 端口整组"、"30 秒 stale"）都是契约的
   一部分。已知注释滞后清单（模板保留名、4-port 措辞）登记在 [[S_01]] / [[S_04]]
   遗留节。

## 提交约定

本模块改动的 commit message scope 固定用 `instance-manager`
（如 `feat(instance-manager): ...`）。

footer 用 `Refs: #<issue>` 格式关联 issue。issue 号若无法从当前上下文明确，必须先询问用户，不要臆造或留空。

涉及 `docs/features/F_*` / `docs/specs/S_*` 文档更新的特性改动，**特性代码、测试代码、文档
拆成三个连续提交**（提交 1 特性代码 `feat(instance-manager)`，提交 2 单测
`test(instance-manager)`，提交 3 文档 `docs(instance-manager)`）；归档 `docs/bugfix/B_*`
的缺陷修复例外，代码、测试、文档合并为一个 `fix(instance-manager)` 提交。细则见上文
「设计文档归档与双向同步」与 [`docs/AGENTS.md`](docs/AGENTS.md)。
