---
name: trustscore
description: Compute a model's TrustScore consistency score for a question by asking the model once, generating paraphrased multiple-choice checks with distractors, and measuring whether the model can re-select its original answer. Use this skill whenever the user wants to evaluate answer consistency, trustworthiness, or self-consistency of an OpenAI-compatible chat model on a specific question.
---

# TrustScore

## 功能概述

TrustScore 用于评估某个 OpenAI-compatible chat model 对单个问题回答的一致性。它先让模型 A 回答原始 question，得到原始答案 `ans`，再生成多个 question paraphrase 和与 `ans` 相近但不同的 distractors，构造包含 `ans`、distractors 和 `unsure` 的 MCQ，最后再次询问模型 A 并统计模型选择原始答案的比例。

输出分数范围为 `0.0` 到 `1.0`：越接近 `1.0`，表示模型在改写问题和干扰选项下越稳定地坚持原始答案。

## 配置方式

本 skill 不把 `openai` 或其他 SDK 写入 JiuwenClaw 的默认依赖，避免影响主项目 CI 和其他用户环境。使用前请在自己的运行环境中安装：

```bash
pip install openai pydantic
```

配置 API key：

```bash
export OPENAI_API_KEY="your-api-key"
```

如果使用 OpenAI-compatible endpoint，可以额外配置：

```bash
export OPENAI_BASE_URL="https://your-provider.example/v1"
```

也可以在命令行里传入 `--api-key` 和 `--base-url`。默认使用
`TRUSTSCORE_API_KEY` / `OPENAI_API_KEY` 和 `TRUSTSCORE_BASE_URL` /
`OPENAI_BASE_URL`，同时兼容 JiuwenSwarm/agent 框架中常见的通用环境变量：
`API_KEY` / `MODEL_API_KEY` 和 `API_BASE` / `MODEL_API_BASE`。

## Quick Start

在 skill 目录下运行：

```bash
python scripts/run_trustscore.py \
  --question "What is the capital of Montana?" \
  --model gpt-4o-mini
```

常用参数：

- `--model`: 被评估的模型 A，也就是回答原始问题和 MCQ 的模型。
- `--generator-model`: 用来生成 paraphrases 和 distractors 的模型，默认读取 `TRUSTSCORE_GENERATOR_MODEL`，否则使用 `gpt-5-mini-2025-08-07`。
- `--mcq-num`: 生成并测试的 MCQ 数量，默认 `20`。
- `--distractor-num`: 生成 distractors 的数量，默认 `20`。
- `--base-url`: OpenAI-compatible API base URL。
- `--output`: 将 JSON 结果写入当前 workspace 内的相对路径文件。
- `--timeout-seconds`: 每次模型请求的超时时间，默认 `120`。
- `--max-model-calls`: 单次运行允许的最大模型调用次数，默认 `25`。
- `--max-question-chars`: question 最大字符数，默认 `4000`。
- `--self-test`: 不调用模型，只验证 MCQ 归一化和打分逻辑。

更多参数与输出字段见 [references/usage.md](references/usage.md)。

出于安全考虑，`--question-file` 和 `--output` 只接受当前 workspace
内的相对路径；绝对路径和包含 `..` 的路径会被拒绝。

## Workflow

1. 读取用户提供的 question。
2. 调用模型 A 生成原始答案 `answer`。
3. 调用 generator model 生成 question paraphrases。
4. 调用 generator model 生成与 `answer` 相近但不同的 distractors。
5. 将 paraphrased questions、`answer`、distractors 和 `unsure` 组合成 MCQ。
6. 调用模型 A 回答每个 MCQ，要求只输出选项字母。
7. 归一化模型输出并计算选择原始答案的比例。

## Notes

TrustScore 衡量的是“模型是否能在改写问题和候选答案扰动下保持与自己原始回答一致”，不是事实正确性评分。若原始答案本身错误，TrustScore 仍可能很高。
