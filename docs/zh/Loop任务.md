# Loop 任务（Loop Engineering）

> **目标**：介绍 Loop Engineering 任务编排的两种入口（`jiuwenswarm-loop` CLI 与 `/loop` 斜杠命令）、全部参数与典型用法。
> **英文版：** [English Version](../en/LoopTasks.md)

---

## 1. 概述

Loop Engineering 是一种任务执行框架：用户只提供一句任务描述，系统自动完成「rubric 分解 → maker 执行 → 机器验证 → 独立 grader 验收 → gap 回炉」的循环，直到目标验证达成、预算耗尽或升级人工。

```
① Rubric 分解   任务 → 4-7 条二值可判定验收准则（冻结落盘）
② Maker 执行    jiuwenswarm 真实 harness 干活（与普通会话同能力：工具/skill/多模式）
③ 机器验证      --verify 命令（确定性、权威信号，输出显式标注退出码）
④ Grader 验收   独立 LLM 按准则逐条判定（保守原则：不能确认即 fail）
⑤ 回炉循环      未达标 → gap 清单反馈 maker 修复 → 重验（最多 N 轮）
```

「完成」是结构性保证：机器验证退出码为 0 ∧ grader 全准则通过 ∧ 一致性校验通过 ∧ 预算未耗尽，缺一不可。协议设计参照 LangChain deepagents 的 RubricMiddleware（verdict 五态、per-criterion gap、跨字段一致性校验、注入防御）。

## 2. 两种入口

| | 入口 1：独立 CLI | 入口 2：斜杠命令 |
|---|---|---|
| 触发 | 终端执行 `jiuwenswarm-loop ...` | 会话内输入 `/loop ...` |
| 依赖服务 | 不需要（进程内自建 Runtime） | 需要常驻服务在跑 |
| 交互体验 | 命令行日志 + 终局汇总报告 | 流式事件回传 + 终局 chat.final 汇总 |
| 参数丰富度 | 全参数 | 轻量子集（其余从会话继承） |
| 适用场景 | 脚本化/批量任务/CI | 日常会话中随手发起 |

## 3. 独立 CLI 参数

### 3.1 必填

| 参数 | 说明 |
|------|------|
| `task` | 任务描述；以 `@` 开头时读取该文件内容作为任务（如 `@task.md`） |

### 3.2 可选

| 参数 | 默认 | 说明 |
|------|------|------|
| `--cwd PATH` | 当前目录 | maker 工作目录 |
| `--project-dir PATH` | 取 `--cwd` | 项目标识目录 |
| `--trusted-dir PATH` | 取 `--cwd` | 信任目录（可重复；免权限审批白名单，务必包含工作目录） |
| `--mode MODE` | `agent.code.normal` | maker 模式：`code.normal`（代码）/ `agent`（常规任务）/ `team` 系（多代理） |
| `--max-iterations N` | `3` | 最大迭代轮数 |
| `--state-dir PATH` | `<cwd>/loop_state` | 状态输出目录 |
| `--round-timeout SECONDS` | `900` | maker 单轮超时秒数 |
| `--verify "CMD"` | 无 | 机器验证命令，退出码 0 视为通过（**强烈建议提供**） |
| `--diff-repo PATH` | 自动探测 | git diff 取证目录（显式 > cwd > 向下探测一层子目录 git 仓库） |
| `--evidence-file PATH` | 无 | 产物文件证据（可重复；非 git 任务必需） |

### 3.3 退出码

`0` = satisfied 且机器验证通过；`1` = 其他错误；`2` = 迭代上限；`3` = rubric 无法评估；`130` = 中断。

## 4. 斜杠命令参数

```
/loop [--verify "命令"] [--max-iterations N] 任务描述
```

`cwd`/`project_dir`/`trusted_dirs`/`mode` 从发起会话的参数自动继承。token 边界：仅 `/loop` 或 `/loop ...` 命中，`/loops`、"请解释 /loop"等仍按普通消息处理。

## 5. 典型用法

```bash
# 代码修复 + 测试验证（最典型）
jiuwenswarm-loop --cwd ~/myproject --trusted-dir ~/myproject \
  --verify "python -m pytest tests/ -q" \
  "修复 tests/ 下失败的三个测试用例对应的 bug"

# SWE-bench 式：任务文件 + 验证脚本
jiuwenswarm-loop --cwd /workspace --trusted-dir /workspace \
  --verify "bash /workspace/verify.sh" "@/workspace/task.md"

# 写作任务：agent 模式 + 产物证据（非 git 场景）
jiuwenswarm-loop --mode agent --cwd /out --trusted-dir /out \
  --evidence-file /out/文章.md \
  "写一篇 1000 字左右的文章，输出到 /out/文章.md"

# 会话内随手发起
/loop --verify "npm test" 把 README 的安装步骤更新为最新用法
```

## 6. 状态文件

每次运行在 `--state-dir` 生成 `loop_state.json`：冻结的 rubric、每轮机器验证与 grader 判定、逐条准则 pass/fail 与 gap、终态（`satisfied` / `failed` / `max_iterations_reached`）与升级记录。该文件即 loop 的外置状态，可审计、可断点检查。

## 7. 注意事项

1. **模型配置**：与 Web/CLI 共用 `~/.jiuwenswarm/config/config.yaml` 的 `models.defaults[0]`
2. **信任目录**：独立 CLI 必须把工作目录传给 `--trusted-dir`，否则 maker 操作会弹权限审批（无人值守无人应答）
3. **任务描述要完整**：协议含无人值守纪律（不提问不等待确认），约束、路径、验证方式需一次性写进任务
4. **`--verify` 的价值**：grader 的权威信号；缺失时 grader 只能保守判定，常导致多轮 needs_revision

## 返回导航

[返回文档首页](../README.md)
[返回项目首页](../../README_CN.md)
