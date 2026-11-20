---
id: B_20261118_1127_tui-env-atomic-write
title: TUI 配置保存写回 .env 非原子且静默失败
type: B
author: taozhenyu
author_email: taozhenyu4@huawei.com
designs: []
design_note: 对齐上游 Web 路径既有原子写契约的小范围修复，无独立设计文档。
modules:
  - jiuwenswarm
  - jiuwenswarm/common/config_panel
  - tests/unit_tests/common
files:
  - jiuwenswarm/common/config_panel/tui_models_handlers.py
  - tests/unit_tests/common/test_tui_env_atomic.py
---

# TUI 配置保存 `.env` 非原子写与静默失败

## 现象与根因

TUI 的 `config.set` 路径中，`persist_env_updates` 仍以 `open(env_path, "w")` 直接覆盖 `.env`：打开即截断，随后才写入全部行。写入过程中出现 I/O 错误或进程退出，`.env` 会停留在截断或半截状态，API Key、模型地址等配置全部丢失。

该函数还把 `OSError` 捕获后仅记 warning，不向调用方抛出；`config_set_handler` 在调用前已把新值写入 `os.environ`，调用后仍无条件返回 `ok=True`。用户看到保存成功，但磁盘未更新，重启后配置回退。

上游提交 `79c71392e` 已把 Web/ConfigAdapter 路径改为 `atomic_write` + `config_write_lock` + 失败抛 `ConfigPanelInternalError`，但 TUI 路径未同步；发现时最新 `develop`（fdc28e35c）仍存在本缺陷，rebase 后的 `21119c03d` 也未修改该文件。已检索 Issue 与 open PR，未发现覆盖 TUI 路径的条目。

## 修复与不变量

- `persist_env_updates` 复用 `jiuwenswarm.common.config_persistence` 的 `atomic_write` 与 `env_content`，并在 `config_write_lock` 内完成读-改-写，与 Web 路径保持同一实现契约；保留原有父目录创建行为（`mkdir(parents=True, exist_ok=True)`）。
- `config_set_handler` 先落盘，成功后才 `os.environ.update(env_updates)`；`OSError` / `UnicodeError` 返回 `ok=False, code="INTERNAL_ERROR"` 并直接返回。
- 跨进程写锁与文件 IO 通过 `asyncio.to_thread` 执行，避免在 async handler 内阻塞事件循环。
- models/embed YAML 同步失败的分支保留“env 变更先落盘”的既有行为；env 写盘失败只记日志，不掩盖原始 YAML 错误，且仅在写盘成功后更新 `os.environ`。
- env 落盘失败但 config.yaml 已更新时，仍触发一次后台 reload，避免部分失败留下未生效的 YAML。
- 不变量：写入失败时原 `.env` 内容与权限保持不变；成功时替换是原子的；进程内环境变量只在磁盘写入成功后更新。

未修改 Web/ConfigAdapter 路径、共享的 `config_persistence` 实现或 `.env` 格式；TUI 的 handler 契约、错误码与响应字段保持不变。

## 行为验证

- 新增 `tests/unit_tests/common/test_tui_env_atomic.py`：
  - 成功路径保留原 key、写入新 key、无 staging 残留；
  - 注入 `OSError` 时异常上抛且原文件不变；
  - handler 层失败返回 `ok=False, INTERNAL_ERROR` 且不更新 `os.environ`；
  - 成功后才更新 `os.environ`；
  - env 落盘失败但 YAML 已更新时，仍触发一次后台 reload。
- `python -m py_compile`、`ruff check` 与 `git diff --check` 通过。
- `tests/unit_tests/common/test_tui_env_atomic.py`：**5 passed in 177.10s**（项目 `.venv`，Python 3.13.13，`uv sync` 安装的锁定依赖）。
- 回归 `tests/unit_tests/gateway/test_cli_channel_handlers.py`：**25 passed in 190.80s**。
- rebase 到 `21119c03d` 后，两个测试文件合并运行：**30 passed in 211.60s**，`ruff check` 通过。
- UI 级对照（源码构建 TUI，两侧前端一致）：
  - 旧代码（已安装 0.2.8，`ws://127.0.0.1:19001/tui`）：`.env` 只读时 TUI 仍提示“已更新”，`.env` SHA256 不变，日志 `[Errno 13] Permission denied`。
  - 修复后（源码实例，`ws://127.0.0.1:20001/tui`）：同一命令 TUI 提示 `Failed to persist .env`，`.env` SHA256 不变，日志 `[WinError 5] 拒绝访问`（原子替换阶段失败）。

## 交接限制

桌面版 `workswarm.exe` 未打包 CLI/TUI 通道（issue #5462），因此 UI 级验证使用源码构建的 TUI 前端分别连接已安装 0.2.8 后端（旧代码）与源码后端（含本修复）；两侧前端一致，仅后端代码不同。Web 界面的同类问题已由上游 `79c71392e` 修复，不在本次范围。另：验证中发现 TUI `config.get` 在新架构下被控制面拦截、schema 为空，导致 `/config set` 前端校验失败；该问题与本修复无关，建议单独跟踪。
