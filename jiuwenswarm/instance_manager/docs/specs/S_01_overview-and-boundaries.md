# 模块总览与包边界

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / overview-and-boundaries |
| 关联模块 | jiuwenswarm/instance_manager/（全包） |
| 最近一次修订日期 | 2026-10-14 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **instance_manager 包的整体结构**：子模块划分、`__init__` 聚合导出策略、
对 common 的依赖边界、消费者全景与已知遗留。各主题域的内部契约分别由
[[S_02]]–[[S_07]] 承载，本 spec 不重复。

**instance_manager 的定位**：多实例隔离（multi-instance isolation）的纯工具层——实例
命名、端口分配与冲突探测、instances.yaml 管理、实例锁与 PID 文件、状态查询与进程控制、
bootstrap .env 生成。它不启动任何服务、不监听任何端口；`jiuwenswarm-start`
（start_services.py）才是编排者，本包只提供它需要的原子能力。

## 不变量

- **N1 依赖方向单向且极窄**：instance_manager 对仓库内代码的依赖只有
  `jiuwenswarm.common.utils` 的两个路径函数（`get_user_home` / `get_user_workspace_dir`，
  status.py 中惰性导入）。不 import `server` / `gateway` / `channels` / `agents` /
  `start_services` 任何模块——启动编排逻辑反向进本包即破坏分层。
- **N2 早期引导零依赖**：`bootstrap._create_basic_bootstrap_env` 在 argparse 早期解析
  阶段被调用（dotenv_early.py，任何 jiuwenswarm 模块导入之前），因此它**不得依赖任何
  jiuwenswarm 模块**（含本包其它子模块）；instances.yaml 读取、home 解析、端口计算全部
  就地实现，失败兜底 index=1（细则见 [[S_07]]）。
- **N3 `__init__` 聚合导出是唯一兼容门面**：`instance_manager/__init__.py` 聚合导出 45 个
  公开符号（按子模块分组 re-export，`__all__` 维护全量清单）。消费者（start_services 等）
  一律 `from jiuwenswarm.instance_manager import ...`；新增公开符号必须同步 `__all__`，
  否则聚合入口静默缺失。
- **N4 私有符号的跨模块豁免名单**：`_upsert_env_ports`（config → start_services）、
  `_create_basic_bootstrap_env`（bootstrap → dotenv_early）、`_get_system_executable`
  （config → lock / status）三个下划线符号被包外或跨子模块消费，是**有意的受控豁免**；
  新增私有符号跨模块消费必须在此登记理由，未登记的跨模块私有导入按违规处理。
- **N5 数据类即契约**：`InstanceConfig` / `InstanceStatus`（见 [[S_02]]）是全包与消费者
  之间的唯一形状描述；跨函数传 dict 的旧式接口（`check_instance_running` /
  `create_bootstrap_env_for_name`）标注 legacy，不得新增同类接口。
- **N6 平台分支集中且成对**：Unix / Windows 双实现共四处（锁 [[S_05]]、进程存活
  [[S_05]]、进程停止 [[S_06]]、PID 反查 [[S_06]]），全部经 `platform.system().lower()`
  判定；改任一平台的语义必须同步检查其余三处的同构分支。

## 子模块划分

| 子模块 | 职责 | spec |
|---|---|---|
| `config.py` | 数据类（InstanceConfig / InstanceStatus / InstancesYamlError）、常量（BASE_PORTS / PORT_TYPES / 保留名 / 文件名）、名称校验、端口管理与探测、.env 端口持久化 | [[S_02]] [[S_03]] |
| `yaml.py` | instances.yaml 路径、读取校验、原子写、模板创建、条目更新、index 计算 | [[S_04]] |
| `lock.py` | InstanceLock 跨平台文件锁、PID 文件读写删、进程存活探测 | [[S_05]] |
| `status.py` | 状态查询（含 default 实例端口探测）、实例列表、配置装载、进程停止 | [[S_06]] |
| `bootstrap.py` | bootstrap .env 生成（常规 / 早期 / 按名装载） | [[S_07]] |

## 消费者全景

| 消费者 | 用法 |
|---|---|
| `jiuwenswarm/start_services.py`（jiuwenswarm-start） | 最大消费者：从包根导入 19 个符号（InstanceLock / find_available_ports / write_pid_file / stop_* 等），另直接 import `_upsert_env_ports`（默认实例端口持久化） |
| `jiuwenswarm/dotenv_early.py`（早期解析） | 调 `_create_basic_bootstrap_env`（早期兜底）；`load_instance_bootstrap_by_name` 的同名包装已 deprecated，委托本包 |
| `jiuwenswarm/init_workspace.py`（jiuwenswarm-init） | 创建实例：名称校验 + workspace 创建 + yaml 注册 |
| `jiuwenswarm/server/agent_http_server.py` | 实例环境（http_api 端口等）消费 |
| `jiuwenswarm/channels/desktop/desktop_app.py` | Desktop 通道的实例感知 |

## 已知遗留

- `__init__.py` docstring 引用的设计参考文档 `multi-instance-isolation-design.md` 未随仓
  归档；本目录 `designs/architecture_cn.md` 现在承担该设计叙事职责。清理时更新 docstring
  指向。
- `config.find_available_ports` docstring 写 "full 4-port group"，但端口组自 http_api 加入
  后实际是 **5 个端口**（BASE_PORTS 五键）；`yaml.create_instances_yaml_template` 模板注释
  的保留名只列了 `default, config, tmp`，实际 `RESERVED_NAMES` 是 5 个（另含
  `jiuwenswarm` / `all`）。两处文档性注释滞后于代码，修注释时同步本条。

## 修改检查单

- 新增公开符号 → 同步 `__init__.py` import 块与 `__all__`（N3），并判定归属哪个 spec
  （新主题 = 新 spec 文件，编号递增）。
- 新增子模块 → 在本 spec「子模块划分」表登记 + 模块 `AGENTS.md` 模块地图同步。
- 在本包内 import 新的仓库内依赖 → 过 N1（只允许 common.utils 两个路径函数）；
  早期引导路径另过 N2。
- 新增跨模块消费的私有符号 → 过 N4（登记豁免名单）。
- 新增平台分支 → 过 N6（检查其余三处同构分支）。
- 改 `InstanceConfig` / `InstanceStatus` 字段 → 过 [[S_02]] + grep 全部构造点（含
  start_services 的直接构造）。

## 与其它 spec 的关系

本 spec 是其余 6 份的公共前言：[[S_02]]–[[S_07]] 描述各主题域内部契约，均默认遵守
本文 N1–N6 的包级不变量。
