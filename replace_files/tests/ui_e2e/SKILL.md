---
name: ui_e2e
description: 运行 JiuwenClaw Web UI 端到端测试并收集截图、日志、report.md、report.json。用于验证 Todo 和 Cron Web UI 流程、复现浏览器交互问题、选择运行解释器、准备 Playwright 环境，或返回可操作的失败证据时。
---

# UI E2E

复用本目录现成脚本，不要临时重写浏览器测试流程。

## 使用脚本

- `todo_ui_report.py`：验证待办创建、状态更新、Tool Panel 展示。
- `cron_ui_report.py`：验证定时任务面板、结构化提醒、预览、立即执行、开关、删除。
- `run_suite.py`：顺序执行多个场景并汇总结果。

## 准备环境

- 选择用于启动 `jiuwenclaw.app` 和 `jiuwenclaw.app_web` 的 Python 解释器。
- 在该解释器里安装项目依赖和 `.[e2e]`。
- 确保 `jiuwenclaw/web` 已安装前端依赖。
- 确保本机可用 Chrome/Chromium；没有时再安装 Playwright 浏览器。

常用命令：

```bash
export JIUWENCLAW_E2E_PYTHON=.venv/bin/python
"$JIUWENCLAW_E2E_PYTHON" -m pip install -e ".[e2e]"
"$JIUWENCLAW_E2E_PYTHON" -m playwright install chromium
```

## 解释器选择

1. `--runtime-python`
2. 环境变量 `JIUWENCLAW_E2E_PYTHON`
3. `./.venv/bin/python`
4. 当前解释器

优先使用仓库自己的虚拟环境，不要硬编码个人机器路径。

## 执行

运行完整套件：

```bash
python3 -m tests.ui_e2e.run_suite --build
```

运行单个场景：

```bash
python3 tests/ui_e2e/todo_ui_report.py --build
python3 tests/ui_e2e/cron_ui_report.py --build
```

指定解释器或输出目录时，显式传参：

```bash
python3 -m tests.ui_e2e.run_suite \
  --build \
  --runtime-python "$JIUWENCLAW_E2E_PYTHON" \
  --report-root /tmp/ui-e2e-suite
```

```bash
python3 tests/ui_e2e/cron_ui_report.py \
  --build \
  --runtime-python "$JIUWENCLAW_E2E_PYTHON" \
  --report-dir /tmp/cron-ui-report
```

默认使用临时 `HOME` 做冒烟验证；只有确认真实工作区行为时，再显式传入真实 `--home`。

## 产物

- `report.md`
- `report.json`
- `backend.log`
- `ui.log`
- 若干截图

默认产物目录在 `tests/ui_e2e/artifacts/`。

## 场景

- `todo_ui_report.py`：启动真实 `jiuwenclaw.app`，验证待办工具链和 Tool Panel。
- `cron_ui_report.py`：启动真实 `jiuwenclaw.app`，验证 Cron 面板和结构化提醒。

## 输出结论

- 实际执行的命令
- 使用的运行时解释器
- 报告目录
- 每个场景的通过或失败状态
- 第一处可操作的失败信息
- 对应证据文件名，例如截图或日志
