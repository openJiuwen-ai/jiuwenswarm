---
id: B_20261127_1408_channel-web-log-dir-layout
title: channel-web 日志目录仍指向废弃的 profile/agent/.logs，导致 ws-dev.log 无法写入
type: B
author: liujiayu75
author_email: liujiayu29@huawei.com
modules:
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/channels/web/app_web.py
  - tests/unit_tests/channels/web/test_app_web_log_dir.py
designs: []
design_note: 纯 bugfix，不涉及新设计；仅把 web 进程的日志落点对齐既有 paths 契约（<数据根>/logs/agent），并为并发归位窗口补一次重试。无独立设计文档。
---

# channel-web 日志目录仍指向废弃的 profile/agent/.logs，导致 ws-dev.log 无法写入

Issue：#7212

## 现象与根因

启动时 web 进程打印：

```text
Failed to configure web file logging: [Errno 2] No such file or directory:
  '<数据根>\profile\agent\.logs\ws-dev.log'
```

web 端文件日志 `ws-dev.log` 不按规范落盘（该文件记录前端↔后端业务报文，含
`config.validate` 等 method 的 `model_params`，依赖 `SensitiveDataFilter` 脱敏）。

根因分两层：

1. **主因（确定性）**：`channels/web/app_web.py:150-151` 的 `_get_logs_dir()` 仍返回
   `_get_agent_root_dir() / ".logs"`，即 `<数据根>/profile/agent/.logs`（布局收敛前的旧落点）。
   现行契约是 `common/paths.py` 的 `get_agent_logs_dir()` → `<数据根>/logs/agent`，其
   docstring 明确要求「应用侧日志一律经本函数派发，不得自行拼接」。同文件的
   `workspace_root` 已按新布局计算（`app_web.py:3510-3513`），唯独 `logs_root` 漏改。
2. **次因（时序）**：`_setup_logger()` 先 `mkdir` 再 `FileHandler`（`app_web.py:3348-3374`）。
   而布局归位第 4 步会把 `profile/agent/.logs` 并入 `logs/agent` 后剪除该目录
   （`common/paths.py:1271-1276`）。`jiuwenswarm.app` 与 `channels.web.app_web` 是两个进程、
   启动时都会执行归位，因此存在「web 刚建好目录 → 被兄弟进程剪掉 → 打开文件失败」的窗口。

归位逻辑把 `profile/agent/.logs` 收进 `logs/agent` 属设计意图，本身不是缺陷；问题在于 web
仍站在这个已被收走的目录上。故根因是**路径契约过期**，时序只是暴露方式。

复现率实测：契约断言 100% 失败（`_get_logs_dir() != get_agent_logs_dir()`）；启动告警依赖
时序 —— 单独启动 `app_web` 2 次均未出现，`jiuwenswarm-start`（app + web 双进程）命中。

## 修复决策

1. `_get_logs_dir()` 改为复用 `common/paths.get_agent_logs_dir()`，删除本地旧实现；
   写入目标与归位目标桶一致后，目录不会再被搬走/剪除。
2. `_setup_logger()` 抽出 `_open_ws_dev_handler()`，对 `FileNotFoundError` **重试一次**
   （重建目录后再开文件）；第二次仍失败则向上抛出，由调用方按原逻辑降级。
3. 失败告警补充 `logs_root` 与 `agent_log_bucket` 两个目录，便于现场直接判断是否路径错位。
4. 附带收益：handler 的 `logs_root` 类属性（`app_web.py:561`，同时用于 `/file-api`
   allowed roots）随之指向真实日志桶 `logs/agent`，日志读取口径与落盘口径一致。

未做的取舍：不引入双写、不改 `common/paths.py` 的归位逻辑、不扩大 `/file-api` 的 allowed roots
覆盖到其它组件桶（保持本 PR 最小改动面）。

## 验证

- 契约断言（修复前 `False`，修复后 `True`）：

  ```text
  app_web._get_logs_dir()   = C:\jw-repro\logs\agent
  paths.get_agent_logs_dir() = C:\jw-repro\logs\agent
  ```

- 运行期（`JIUWENSWARM_DATA_DIR=C:\jw-repro`，单独启动 `app_web` 9s）：
  - `Failed to configure web file logging` 出现 **0** 次；
  - `logs/agent/ws-dev.log` 被新建/更新（893 B），不再写入 `profile/agent/.logs`。
- 单测：新增 `tests/unit_tests/channels/web/test_app_web_log_dir.py`（4 例，全绿）：
  1. `_get_logs_dir()` 等于 `get_agent_logs_dir()`、且不再是旧布局；
  2. `_setup_logger()` 把 `ws-dev.log` 写进目标桶；
  3. 首次 `FileHandler` 抛 `FileNotFoundError` 时重建目录并重试成功（调用计数 = 2）；
  4. 两次都失败时向上抛出，交由调用方降级。
- 回归：`tests/unit_tests/channels/web` 共 301 例，**300 passed / 1 failed**；唯一失败为
  `test_web_public_fallback_does_not_escape_static_root`（`WinError 1314`，Windows 建符号链接
  无权限），与本改动无关。

## 遗留

- 升级前若已生成 `<数据根>/profile/agent/.logs`，该空目录可能在数据根中残留（web 已不再写入）；
  由后续布局归位回收即可，不影响运行。
- 同族未修（建议单独跟踪，均属「布局收敛后路径未同步」）：
  - 前端 `LogsPanel/index.tsx:69` 仍读 `agent/.logs/ws-dev.log`；
  - 前端 `SessionsPanel/index.tsx:350` 仍用 `agent/sessions/<session_id>`；
  - `app_web.py:558-571` 的 `/file-api` allowed roots 仍指向 `agent`、`.agent_teams`、`agent/.logs`；
  - 上述组件在个人版当前无 UI 入口，属潜伏缺陷，接入后即会 403 / 取不到文件。
