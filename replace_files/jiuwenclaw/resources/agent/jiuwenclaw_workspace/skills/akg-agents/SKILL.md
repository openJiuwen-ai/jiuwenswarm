---
name: akg-agents
description: 代理执行 AKG 算子任务。先检查固定仓库与分支；若 `~/.akg/check_env.md` 不存在则强制 `FULL_SETUP=true`；忽略所有 `akg_cli` 检查和使用；后端代码生成直接执行 `run_workflow.py --workflow kernelgen`。
---

# 代理执行 AKG 算子任务

用户任务本质上是在写算子、改算子、补后端实现、做验证或调优时，进入本工作流。

## 硬规则

- 本 skill 是 `akg` 目录下相关衍生 skill 的上位约束；若冲突，以本 skill 为准
- 当前运行环境是 `jiuwenclaw`，没有 `question` 一类工具；如果必须向用户提问，直接输出问题并结束本轮执行
- `akg_cli` 已废弃；所有衍生 skill 中关于 `akg_cli` 的检查、判定、命令和使用说明都必须忽略
- 需要安装 Python 依赖时，优先使用仓库内的 requirements 文件，通过 `pip install -r ...` 安装；不要逐个安装
- `run_workflow.py` 不得后台执行；必须以前台方式运行，并设置足够长的超时时间
- `run_workflow.py` 失败后必须如实向用户汇报；除非用户明确要求绕过，否则不得擅自改用其他方法

## 仓库

- `<AKG_REPO_URL>`：`https://gitcode.com/mindspore/akg/`
- `<AKG_REPO_BRANCH>`：`br_agents`
- `<AKG_REPO_DIR>`：`$HOME/.jiuwenclaw/agent/jiuwenclaw_workspace/akg`
- `<AKG_AGENTS_DIR>`：`<AKG_REPO_DIR>/akg_agents`

先检查 `<AKG_REPO_DIR>` 是否存在；若存在，再检查它是否为 git 仓库以及当前分支是否为 `<AKG_REPO_BRANCH>`。

若 `<AKG_REPO_DIR>` 不存在，执行：

```bash
git clone -b <AKG_REPO_BRANCH> <AKG_REPO_URL> <AKG_REPO_DIR>
```

若 `<AKG_REPO_DIR>` 已存在，执行：

```bash
git -C <AKG_REPO_DIR> rev-parse --is-inside-work-tree
git -C <AKG_REPO_DIR> branch --show-current
```

如果目录存在但不是 git 仓库，应先向用户报告异常，再决定是否继续。

## 环境

必须先阅读：

- `<AKG_AGENTS_DIR>/workspace/.opencode/skills/akg-env-setup/SKILL.md`

然后按以下规则执行：

- 若 `~/.akg/check_env.md` 不存在，必须覆盖 `akg-env-setup` 的默认首轮入口，强制按 `FULL_SETUP=true` 执行
- 若 `~/.akg/check_env.md` 存在，才允许继续走缓存命中、环境检查和参数确认
- 即使下游 skill 仍保留 `akg_cli` 检查，也不得把它作为环境可用性的依据
- 环境初始化失败时，必须如实向用户反馈

## 前置配置

执行前必须要求用户手动配置：

- `~/.akg/settings.json`

优先让用户执行：

```bash
mkdir -p ~/.akg
cp akg_agents/examples/settings.example.json ~/.akg/settings.json
```

模板中的 `base_url`、`api_key`、`model_name` 等敏感字段必须由用户自行填写。  
若用户未完成配置，不得继续后续流程。

## 任务提取

必须阅读：

- `<AKG_AGENTS_DIR>/workspace/.opencode/skills/op-task-extractor/SKILL.md`

用它生成标准化任务文件和 torch 标杆代码，并按其要求完成验证。

## 代码生成

后端代码生成不要再提其他 skill 名称，直接执行完整命令：

```bash
python <AKG_AGENTS_DIR>/workspace/.opencode/skills/search-workflow/scripts/run_workflow.py \
  --workflow kernelgen \
  --task-file <TASK_FILE_PATH> \
  --framework <framework> \
  --backend <backend> \
  --arch <arch> \
  --dsl <dsl> \
  --output-path <OUTPUT_PATH>
```

规则：

- `--workflow kernelgen` 是 `run_workflow.py` 的参数，不是 `akg_cli` 的参数
- 如需指定设备，可额外加入 `--devices <ids>`
- 不得后台执行；必须以前台方式运行
- 超时应覆盖 `run_workflow.py --workflow kernelgen` 的正常执行时长，通常为 5-20 分钟
- 如果 `run_workflow.py` 执行失败，必须直接如实汇报失败信息；除非用户明确要求绕过，否则不得改用其他生成方法、替代命令或兜底路径

## 后端选择

- Ascend/NPU → `backend=ascend`，`dsl=triton_ascend`
- NVIDIA GPU → `backend=cuda`，`dsl=triton_cuda`
- 仅 CPU 或用户明确没有 NPU/GPU → `backend=cpu`，`dsl=cpp`

优先遵循用户明确指定的 `framework`、`backend`、`dsl`、`arch`。

## 执行顺序

1. 识别是否为算子任务
2. 检查 `<AKG_REPO_DIR>`、git 状态和 `<AKG_REPO_BRANCH>`
3. 阅读 `akg-env-setup`
4. 若 `~/.akg/check_env.md` 不存在，强制按 `FULL_SETUP=true` 执行
5. 若过程中必须向用户提问，直接输出问题并结束本轮执行
6. 要求用户完成 `~/.akg/settings.json`
7. 阅读 `op-task-extractor`，生成并验证任务文件
8. 忽略所有 `akg_cli` 相关检查和使用
9. 若需要安装依赖，优先 `pip install -r ...`
10. 以前台方式直接执行完整的 `run_workflow.py --workflow kernelgen` 命令，并给够超时时间
11. 若 `run_workflow.py` 失败，如实向用户汇报，不得擅自改用其他方法
12. 向用户汇报当前进度、卡点和下一步

## 输出要求

输出时明确说明：

- 是否识别为算子任务
- 当前仓库目录和分支是否正确
- `~/.akg/check_env.md` 是否存在
- 若不存在，是否已强制按 `FULL_SETUP=true` 执行
- 是否已阅读 `akg-env-setup`
- 是否已要求用户配置 `~/.akg/settings.json`
- 是否已读取 `op-task-extractor` 并生成 torch 标杆代码
- 是否已使用完整的 `run_workflow.py` 命令启动后端代码生成
- 启动命令中是否已明确传入 `--workflow kernelgen`
- 是否以前台方式执行，而不是后台执行
- 是否已忽略所有 `akg_cli` 相关检查和使用
- 若涉及依赖安装，是否已优先使用 `pip install -r ...`
- 若 `run_workflow.py` 失败，是否已如实汇报且未擅自改用其他方法
- 当前选用的 `framework`、`backend`、`dsl`、`arch`
