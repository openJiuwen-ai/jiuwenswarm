# B_07 — 深度研究首错被 stderr 尾部截断，SDK 日志缺少稳定定位

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_07 / deepresearch-sdk-logging |
| 实现日期 | 2026-11-13 |
| 本次交付适配日期 | 2026-11-18 |
| 范围 | `common/tools/deepresearch/tools.py`、`common/tools/deepresearch/diagnostics.py` 及相关诊断/stream/runtime/execute 回归 |
| 关联 spec | [S_24](../specs/S_24_deepresearch-sdk-diagnostics.md) |
| 测试基线 | 11 月 13 日源候选历史结果见下；11 月 18 日最新 `upstream/dev-stable` 隔离交付树通过 495 项 |

## 现象、根因与修复决策

本记录只覆盖主研究日志落盘、故障证据保留与定位。
`211800 outliner_generate_error` 是大纲生成阶段的分类码；本次增加可追溯证据，
不把该分类码下所有模型、参数或服务错误视作已修复。

原有 `deepresearch_stream` 仅在 error 结果附带末 20KB stderr，最终诊断又截短为
2048 字符；较早的模型/大纲异常会被大量 `graph_end` 等尾部日志挤走。成功、交互中断、
停止和取消不保存完整 stderr，因此仅凭 UI 分类码无法还原首错。

本次日志实现遵循 [S_24](../specs/S_24_deepresearch-sdk-diagnostics.md)：

- 通过既有 stdin config v1 下发 `OFFICE_CLAW_DEEPRESEARCH_LOG_DIR`，使用
  `get_logs_dir(service_id)/DeepResearch/output/logs`；在早期 SDK 导入前把实际
  子进程 cwd 固定到 `DeepResearch` 基目录。备用数据根仅来自可信宿主进程环境，
  不扩大 child-env 白名单，不采纳租户模型配置里的路径。
- 每次进程独占 `runner/<conversation>.<UTC>.<uuid>.stderr.log`，以二进制逐块保留
  原始 stderr 首尾。完整内容只存受限本机文件；宿主定位日志只有路径、
  request/conversation/attempt/pid、字节数与关闭/完整性状态，模型仍仅得到原有脱敏尾部。
- 文件 I/O 在线程执行。打开/写入/读取失败不覆盖研究终态；写磁盘与关闭均有
  1 秒等待界限，超时明确标记不完整并持有后台任务，继续排空子进程管道。
  取消与 spawn 等待中的取消都会清理本次 progress artifact，并完成或安排日志关闭。

## 2026-11-13 历史回归证据

- **首轮 RED**：新增配置帧目录、error/completed/interrupted/stopped 四终态完整文件、
  取消保存的 6 个入口回归，在原实现 **6 failed**。首个失败为缺少配置字段的
  `KeyError`，其余均为诊断文件不存在，非依赖或导入失败。
- **备用根 RED**：可信 `OFFICEACE_USER_DATA_ROOT`、`OFFICE_CLAW_DATA_DIR`、
  `OFFICE_CLAW_HOST_HOME` 三例在补充传输前 **3 failed / 1 passed**；失败均为
  config frame 缺字段。独立 Jiuwen 不伪造宿主根的缺省例已通过。
- **历史 GREEN**：2026-11-13 在源候选本地源码与 `.venv` 下执行，尚不代表本次交付基线已通过：

  ```bash
  .venv/bin/python -B -m pytest \
    tests/unit_tests/agentserver/test_deepresearch_diagnostics.py \
    tests/unit_tests/agentserver/test_deepresearch_stream_tool.py \
    tests/unit_tests/agentserver/test_deepresearch_runtime.py \
    tests/unit_tests/agentserver/test_deepresearch_execute_tool.py \
    -o addopts='--asyncio-mode=auto' -q --tb=short -o log_cli=false
  ```

  **480 passed**。包括两个并发真实 Python 子进程（不调用模型或运行服务），在
  Unicode/空格日志目录下经真实 spawn、stdin/stdout/stderr 管道保存首错、>20KB
  Unicode 内容和非法 UTF-8 尾字节，独立按 bytes 读取文件校验；模型结果仍是短尾部。
  另覆盖独占不覆盖、短写、目录/磁盘/读取失败继续业务与排空、慢写超时、慢关闭超时
  后后台最终关闭、所有终态、取消和 spawn 等待取消，以及 UUID 首帧/文件关联。

本机行为测试运行在 macOS；代码使用 Windows 兼容文件名、`O_BINARY` 与标准 Python
子进程接口，不依赖 Unix 命令，原生 Windows 文件权限/安装包仍需平台验收。
Relay runner 的实际 SDK LogManager 初始化与最终 ZIP 收集由对应消费者和跨仓集成
单独验证；本节不把上述单测结果当成安装包已更新、真实模型已恢复或 ZIP 已完整。

## 2026-11-18 本次交付验证

在 `fbd0bcdcea884ba72756ac02723a64469cf22ada` 基线隔离交付树重放日志改动后，
重新运行上列四个测试文件（另加 `-s` 以保留诊断输出），结果为
**495 passed in 9.67s**。解释器来自现有开发环境，但已核对
`jiuwenswarm.__file__` 指向本次隔离交付树，测试使用当前候选源码。

SDK runner 实际初始化、原始日志文件进入 ZIP，以及 Windows/macOS 的实际执行范围，
分别由消费者与跨仓集成验收记录补充。当前文档尚不宣称原生 Windows、
真实研究调用、运行进程加载新代码、远端 CI 或发布包已验证。

## 交付状态与重验条件

文档适配阶段尚未提交或推送；最终提交、远端分支与 CI 状态由交付步骤记录。
后续改变配置帧、日志根、stderr I/O、取消或导出策略时，重新运行对应入口回归。
