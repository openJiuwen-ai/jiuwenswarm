---
id: B_20261126_1818_log-migration-self-absorb-guard
title: 启动布局迁移自吸收每次清空数据根 logs 顶层日志
type: B
author: CharlesWang2030
author_email: 93426525@qq.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/paths.py
  - tests/unit_tests/common/test_paths_log_migration.py
designs: []
design_note: 对既有 profile 数据根布局迁移的局部缺陷修复，修复遵循"解析同路径即跳过"的既有守卫模式，不新增架构方案，无独立设计。
---

# 启动布局迁移自吸收清空数据根 logs 顶层日志修复记录

## 现象与影响

个人版每次启动都会触发 profile 数据根布局迁移（`migrate_profile_data_root_layout`）。
在新布局已稳定的实例上重复执行时，`_do_migrate_profile_data_root_layout` 第 4 步
"散落日志归位"中的 `stray_logs` 就是 `get_logs_dir()` 本身——本轮写在
`<root>/logs/` 顶层的日志文件（`agent_server.log`、`gateway.log`、`full.log` 及
20MB × 20 份轮转备份）会被当作"待归位的散落日志"处理，进入同名同内容判定后被
`unlink` 删除。表现为：上次运行产生的顶层日志与轮转备份在下次启动后全部消失，
且每次启动重复清空。

同类机理在 Windows junction 兼容链上放大：`_LEGACY_COMPAT_LINKS` 与
`_LOG_COMPAT_LINKS` 维护的老路径兼容链（如 `workspace`、`agent`、
`agent/.logs`、`logs/logs`）在 Windows 上以 junction 实现，`Path.is_symlink()`
恒为 False 而 `is_dir()` 为 True，迁移的"目录递归归并"会沿链进入链目标内部，
把链目标桶乃至整棵 profile 的正当内容当作"内容一致的重复"逐个删除——后果比
顶层日志更重，属业务数据丢失。

## 关联设计与当前契约

无独立设计。涉及三条既有契约：

1. **自吸收不变量**：迁移不得把任何路径吸收到它自身（`old.resolve() ==
   new.resolve()` 时必须跳过）。该不变量此前只在目录分支的"解析同路径即跳过"
   模式中部分存在，未形成全函数的一致约定。
2. **平台语义**：Windows junction 不是 symlink——`is_symlink()` 恒为 False、
   `is_dir()` 为 True。迁移代码里"目录且非软链才递归"的判据无法区分
   junction 兼容链与真实目录，所有遍历入口必须显式做 `resolve()` 身份比较。
3. **重复判定契约**：`is_duplicate`（同尺寸同内容即剪源）的语义是"两侧是同一
   文件的两个副本，保留 new 剪掉 old"；当 `old` 与 `new` 解析为同一路径时，
   "剪掉 old"退化为无条件自删，该契约对 self case 不适用，必须在进入判定前短路。

## 根因与修复决策

### 根因

缺陷不是单一遗漏，而是自吸收守卫面在不同代码路径上覆盖不一致，定位分三轮逐处补齐：

1. **第 1 轮——顶层文件分支缺失**：第 4 步普通文件分支直接进 `_absorb_file`，
   无任何 `old == new` 判定，`stray_logs == get_logs_dir()` 的自指导致
   "同内容重复"误判 + `unlink`。
2. **第 2 轮——兼容链挂载点全覆盖**：`_move_if_absent`（通用搬迁入口）与
   `_do_migrate_…` 内嵌 `_absorb_tree` 补 `old.resolve() == new.resolve()` 守卫，
   并对 `logs/logs`、`agent/.logs`、`agent/process_cli_logs`、租户
   `config/runtime_state`、`workspace` 五处挂载点补显式守卫（同时跳过
   `_prune_empty_tree`）。
3. **第 3 轮——递归归并分支对齐**：模块级 `_migrate_absorb`（`_move_if_absent`
   目标已存在时的递归归并）目录分支入口补同样的守卫。"部分迁移中断 + 兼容链
   已建"的复合场景可达：`root/agent` 仍是真目录（部分数据滞留）但树内 `.logs`
   已是前次运行留下的 junction，第 2 步 `_move(root/agent, profile/agent)` 进入
   递归归并，递归到 `.logs` 时沿链把 `logs/agent` 桶内正本当重复删除——顶层
   入口守卫挡不住递归内部的同名位置。

### 修复决策：守卫模式 vs 收敛遍历范围

备选方案是收敛第 4 步的遍历范围（`stray_logs` 排除已归位项），未采用，理由：

- 守卫是**不变量声明**（"迁移不得把路径吸收到自身"），覆盖面随兼容链数量自然
  扩展——未来在 `_LEGACY_COMPAT_LINKS`/`_LOG_COMPAT_LINKS` 新增挂载点时，
  `_move_if_absent`/`_absorb_tree`/`_migrate_absorb` 的通用守卫自动生效，无需
  逐处补丁；
- 收敛遍历范围会改变第 4 步对外可见的归位行为（哪些散落项参与归位），回归面
  更大，且对"沿链吸收"类变体无泛化能力；
- 守卫不改变正常归位语义：`resolve()` 相等仅在自指/链指自身时命中，真实数据
  迁移路径零影响。

配套的**单文件失败降级**：`_migrate_absorb_file`、内嵌 `_absorb_file`、
`_absorb_tree`、`_move_if_absent` 的 mkdir/move/unlink/archive 补 `except OSError`
捕获与 `[Migration]` 告警，对齐 `_prune_empty_tree` 的尽力而为语义——单文件失败
（如 Windows 文件被残留进程占用）不中断整轮幂等迁移，下次启动自动重试；
`FileNotFoundError` 仍静默（并发抢先搬迁视为对方获胜的既有语义）。

### 守卫挂载点清单

通用守卫（三处，覆盖全部递归路径）：

| 位置 | 守卫 |
|---|---|
| `_move_if_absent` 入口 | `old.resolve() == new.resolve()` → 静默跳过 |
| `_migrate_absorb` 目录分支入口 | 同上（与 `_absorb_tree` 对齐） |
| `_do_migrate_…` 内嵌 `_absorb_tree` 入口 | 同上 |

显式守卫（兼容链挂载点，命中时同时跳过 absorb 与 prune）：

| 挂载点 | 链目标 |
|---|---|
| `<root>/logs` 顶层文件 | `get_logs_dir()/<name>` 即自身 |
| `profile/agent/.logs` | `logs/agent` |
| `logs/logs` | `logs/core` |
| `profile/agent/process_cli_logs` | `logs/process_cli` |
| `workspace_{tk}/config/runtime_state` | 租户根 `runtime_state` |
| `<root>/workspace` | `profile` |

以上兼容链由 `_LEGACY_COMPAT_LINKS`（5 条业务链）、`_LOG_COMPAT_LINKS`（2 条
日志链）、`logs/logs`（core 链）与 `ensure_config_runtime_state_compat_link`
（运行态链）共同维护；新增挂载点时同步核对迁移守卫面。

## 回归证据与适用范围

基线 `develop@d256380e1`，修复位于分支 `fix/jw-86-log-migration-self-absorb`
（3 个提交：`9db7a9d3f` → `3bfb74a7c` → `19135dbde`）。

- 缺陷钉住验证（独立探针，非 pytest）：develop 基线源码上同一场景顶层日志全部
  被删（GAP REPRODUCED）；直接调用无守卫的 `_migrate_absorb` 时 profile 正本被
  沿 junction 链删除（GAP CONFIRMED）；完整迁移流程端到端复现桶日志被删
  （GAP STILL OPEN），补对称守卫后闭合（GAP CLOSED）。
- 新增回归测试 `tests/unit_tests/common/test_paths_log_migration.py`
  （10 个用例，291 行），以临时 `JIUWENSWARM_HOME` 隔离：顶层日志与轮转备份
  保留、重复启动幂等、桶日志保留、真实旧布局正常归位、junction 兼容链不自吸收
  （`mklink /J` 实建，非 Windows skip）、部分迁移中断递归守卫、业务数据与
  runtime_state 正本保留、`PermissionError` 降级、正常业务迁移不受影响。
- 定向回归：`pytest tests/unit_tests/common/test_paths_log_migration.py` →
  **10 passed**（Windows 11 / Python 3.13.14，exit 0）；相邻存量套件
  `test_utils.py`、`test_coding_memory_paths.py`、`test_windows_cli_paths.py`、
  `test_agentos_config_migration.py` → **170 passed**。
- `git diff d256380e1..HEAD --check` 通过；仓库约束禁止运行 Ruff/Pylint/mypy/
  格式化工具，未运行。

## 防错模式与资料同步

- 涉及软链/junction 的迁移、遍历、剪除逻辑，凡"处理 old 吸收到 new"的路径，
  必须在**每个递归入口**做 `resolve()` 身份比较，不能只依赖顶层入口守卫——
  递归会把兼容链内部重新暴露为一对新的 (old, new)。
- Windows 兼容链优先 junction（无需开发者模式），其 `is_symlink()` 恒为 False；
  任何"目录且非软链才进入"的判据在 Windows 上对 junction 失效，不得作为
  兼容链的唯一识别手段。
- "同尺寸同内容即剪源"的重复判定必须先短路 self case，再进入内容比较。

## 遗留与后续

- `_prune_empty_tree` 的 `rglob` 在链内剪空目录为可接受副作用（仅空目录消失，
  无数据丢失），记录为观察项不修；
- CI 侧 UT 失败为上游存量缺陷（`agent_observability.py` 缺 `import threading`，
  基线同样存在），与本修复无关，已在 PR 评论举证。
