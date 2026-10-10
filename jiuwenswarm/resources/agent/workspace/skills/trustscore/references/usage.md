# TrustScore Usage

## 输入

`scripts/run_trustscore.py` 至少需要：

- `--question` 或 `--question-file`: 要测试的问题。
- `--model`: 被评估的 OpenAI-compatible chat model 名称。
- API key: `--api-key`、`TRUSTSCORE_API_KEY`、`OPENAI_API_KEY`、
  `API_KEY` 或 `MODEL_API_KEY`。

## 输出

脚本输出 JSON，核心字段包括：

- `trustscore`: 一致性分数，等于 MCQ 中选择原始答案的比例。
- `model`: 被评估模型。
- `generator_model`: 用于生成 paraphrases 和 distractors 的模型。
- `question`: 原始问题。
- `answer`: 模型 A 对原始问题的回答。
- `mcq_count`: 实际测试的 MCQ 数量。
- `correct_count`: 选中原始答案的次数。
- `predicted_options`: 每道 MCQ 归一化后的选项结果。
- `answer_options`: 每道 MCQ 的正确选项。
- `mcq_predictions`: 模型 A 对每道 MCQ 的原始输出。
- `mcq_questions`: 生成的 MCQ 文本。
- `distractors`: 生成的干扰项。

## 示例

```bash
python scripts/run_trustscore.py \
  --question "What is the capital of Montana?" \
  --model gpt-4o-mini \
  --mcq-num 20 \
  --seed 0
```

保存结果：

```bash
python scripts/run_trustscore.py \
  --question-file question.txt \
  --model gpt-4o-mini \
  --output trustscore-result.json
```

For safety, `--question-file` and `--output` must be relative paths inside the
current workspace. Absolute paths and paths containing `..` are rejected.

OpenAI-compatible endpoint：

```bash
python scripts/run_trustscore.py \
  --question "What is the capital of Montana?" \
  --model your-model-name \
  --base-url "https://your-provider.example/v1"
```

`--base-url` 也可以从 `TRUSTSCORE_BASE_URL`、`OPENAI_BASE_URL`、`API_BASE`
或 `MODEL_API_BASE` 读取。`API_KEY`、`MODEL_API_KEY`、`API_BASE` 和
`MODEL_API_BASE` 用于兼容 JiuwenSwarm/agent 框架里的通用模型配置环境变量。

Safety limits:

```bash
python scripts/run_trustscore.py \
  --question "What is the capital of Montana?" \
  --model gpt-4o-mini \
  --timeout-seconds 120 \
  --max-model-calls 25 \
  --max-question-chars 4000
```

`--timeout-seconds` bounds each model request. `--max-model-calls` caps the
planned call count for one run; the default `--mcq-num 20` plans 23 calls.
`--max-question-chars` prevents accidental large prompts from increasing cost.

## 本地自检

不调用 API，只测试答案归一化和一致性打分：

```bash
python scripts/run_trustscore.py --self-test
```

预期输出中的 `trustscore` 为 `0.6666666666666666`。

## 解释分数

- `1.0`: 模型在所有 MCQ 中都选择了自己的原始答案。
- `0.5`: 模型大约一半情况下能保持一致。
- `0.0`: 模型从未在 MCQ 中选择自己的原始答案。

该分数不判断原始答案是否事实正确，只判断模型在 paraphrase 和 distractor 测试下是否与原始回答一致。
