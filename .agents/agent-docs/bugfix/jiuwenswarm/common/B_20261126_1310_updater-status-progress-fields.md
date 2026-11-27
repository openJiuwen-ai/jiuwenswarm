---
id: B_20261126_1310_updater-status-progress-fields
title: pip 升级进度字段未声明于 UpdateStatus，被 asdict 静默丢弃，前端进度活动行永远空白
type: B
author: linghb
author_email: 957733934@qq.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/updater.py
  - jiuwenswarm/common/upgrade_executor.py
  - tests/unit_tests/common/test_updater_status_fields.py
designs: []
design_note: 修复 UpdateStatus dataclass 字段集与运行时 setattr 写入之间的静默不一致，不改变更新源配置、安装方式与前端消费契约；无独立设计。
---

# pip 升级进度字段被 asdict 静默丢弃，前端进度活动行永远空白

## 现象与根因

issue #6717：pip 模式升级时，更新面板（UpdatePanel）的进度活动行
（`current_activity`）始终空白，全程没有任何日志线索。

根因是 `UpdateStatus`（dataclass）的字段集与运行时动态写入之间的静默不一致：

1. `PipExecutor` 经 `_status_callback`（即
   `UpdaterService._executor_callback` → `_update_status`）持续推送
   `current_activity`（节流后的 pip 输出行）与 `pip_output`（失败诊断全文），
   执行器侧确实在推送。
2. `_update_status` 对任意 kwargs 无条件 `setattr`，未声明于 dataclass 的
   key 也会被静默接受，写在实例上。
3. `get_status()` 用 `dataclasses.asdict` 序列化，而 `asdict` 只序列化
   `__dataclass_fields__` 声明的字段，setattr 写入的动态属性被**无告警丢弃**。
4. 前端 `UpdatePanel` 读取 `status.current_activity`，于是永远读到空值。

即：推送端正常、落盘端正常、序列化端静默吞掉、消费端空白，链路每一环
单看都"没问题"，这就是该模式的迷惑性所在。

## 修复决策

- 在 `UpdateStatus` 中补声明 `current_activity: str = ""` 与
  `pip_output: str = ""`（与同为回调推送写入的 `installing` /
  `restart_command` 相邻），`asdict` 恢复携带两字段（PR !10999 主修复）。
- 检视补充（NTH-01）：`_update_status` 改为对不在
  `dataclasses.fields(UpdateStatus)` 中的 key 记 debug 日志并丢弃，
  不再无条件 setattr——把"未声明字段被静默丢弃"这一根因链路从静默失败
  转为可观测信号，后续 executor 新增推送字段而漏声明时能被发现。
- 检视补充（NTH-03）：`start_download` 进入 `upgrading` 时一并重置
  `current_activity=""`（`pip_output` 保留供诊断），消除上一轮失败残留的
  活动行在 executor 首次回调到达前毫秒级闪现的窗口。
- 不改变 executor 推送语义（成功时推 `current_activity=""` 清空、失败时
  保留最后活动行作诊断线索）、不改变前端消费契约。

## 证据

- 修复前：`executor` 推送 dict 含 `current_activity`，而 `get_status()`
  返回 dict 中无该 key（`asdict` 丢弃未声明字段）。
- 修复后新增测试（`tests/unit_tests/common/test_updater_status_fields.py`）：
  - `test_status_serializes_progress_fields`：`_update_status` 写入的两字段
    出现在 `get_status()` 结果中。
  - `test_executor_callback_routes_progress_fields`：`_executor_callback`
    的 `current_activity` / `downloaded_bytes` 正确路由到状态对象。
  - `test_default_status_has_empty_progress_fields`：初始状态两字段为空串。
  - `test_update_status_ignores_undeclared_fields`：未声明 key 被
    `_update_status` 拒绝并记 debug 日志（NTH-01 防错回归）。
  - `test_start_download_resets_leftover_pip_activity`：上一轮失败的
    `current_activity` 在 `start_download` 时被清空（NTH-03 回归）。
  - `test_pip_executor_success_clears_activity_and_pushes_output` /
    `test_pip_executor_failure_keeps_last_activity_line`：executor 级
    生命周期语义——成功时清空活动行并推 `pip_output` 全文、失败时保留最后
    活动行（NTH-02 回归，mock `subprocess.Popen`）。
- 静态核对 `upgrade_executor.py` 全部 `_status_callback` 推送 key 全集
  （`current_activity` / `downloaded_bytes` / `downloaded_path` / `error` /
  `installing` / `pip_output` / `restart_command` / `state` /
  `total_bytes`）与 `UpdateStatus` 声明字段逐一对照，无其他静默丢弃字段。

## 防错提示

为 executor 新增状态推送字段时，**必须同步在 `UpdateStatus` dataclass 中
声明该字段**，否则 `_update_status` 会将其丢弃（并记 debug 日志）。
`asdict` 只序列化 `__dataclass_fields__`，对 setattr 写入的动态属性无告警
丢弃——这是 dataclass 的固有行为，不是 bug，但会让"推送正常、前端空白"
的静默失败重演。

## 遗留

- `pip_output` 目前仅作诊断输出，尚无 UI 消费方。
