---
name: mhqa-rag-optimizer
description: Improve multi-hop question answering accuracy by reordering retrieved documents in forward reasoning-chain order before passing them to the LM. Based on findings from "Masking in Multi-hop QA" (ACL 2025). Use when a question requires synthesising evidence from multiple sources or when RAG answer quality is poor on compositional questions.
---

# MHQA RAG Optimizer

## 功能概述

MHQA RAG Optimizer 是一个面向 **多跳问答（Multi-hop QA）** 的 RAG 上下文编排技能。它在不修改模型、不微调的前提下，通过调整检索文档在提示词中的顺序，提升需要跨多篇文档推理的问题的准确率。

**核心能力：**

- **推理链排序**：将检索到的文档按「第一跳 → 第二跳 → … → 末跳」的推理顺序排列，使模型更容易沿证据链作答。
- **噪声隔离**：将无关文档推到上下文两端，缩短 gold 文档之间的距离，减少干扰。
- **多数投票（可选）**：对同一组文档随机置换顺序 `k` 次，分别调用模型（`temperature=0`），取出现次数最多的答案，作为对单次排序的补充稳健策略。

**适用场景：**

- 用户问题需要串联 2 篇及以上文档才能回答（例如：「电影 X 的导演出生在哪个国家？」）
- Agent 已检索到多篇文档，需要综合生成答案
- 组合型、链式推理类 RAG 问题准确率不稳定或偏低

**输出：**

- 基于重排上下文生成的最终答案
- 简要说明每个推理跳所依赖的文档（可选）

本技能是 **提示词与上下文编排层**，不替代检索系统；假设文档已由 RAG 或其他方式获取。

## 配置方式

### 1. 安装与启用

在 JiuwenSwarm 前端 **技能** 页面安装内置技能 `mhqa-rag-optimizer` 即可。本技能 **无需** 修改 `config/config.yaml` 或额外服务配置；加载后由 Agent 按下方 Workflow 执行上下文重排与作答。

### 2. 环境变量（使用 `permute_and_vote.py` 脚本时）

| 变量名 | 含义 | 默认值 |
|--------|------|--------|
| `OPENAI_API_KEY` | OpenAI 或兼容端点的 API Key | 无（必填） |
| `OPENAI_BASE_URL` | 可选，OpenAI 兼容 API 基地址 | 无 |
| `MHQA_MODEL` | 默认模型名称 | `gpt-4o-mini` |

Agent 直接在对话中重排文档并生成答案时，使用 JiuwenSwarm 已配置的模型即可，**不依赖** 上述环境变量。

### 3. 脚本依赖

使用多数投票脚本前安装 `openai` 包：

```bash
pip install openai
```

### 4. 脚本调用示例

将每篇检索文档保存为独立文本文件后执行：

```bash
python scripts/permute_and_vote.py \
  --question "What country is the birthplace of the director of Inception?" \
  --docs doc1.txt doc2.txt doc3.txt \
  --k 5 \
  --output result.json
```

| 参数 | 说明 |
|------|------|
| `--question` | 多跳问题（必填） |
| `--docs` | 检索文档路径，可多个（必填） |
| `--k` | 随机置换次数，每次一次 API 调用（默认 `5`） |
| `--seed` | 随机种子，便于复现 |
| `--model` | 模型名，默认读 `MHQA_MODEL` 或 `gpt-4o-mini` |
| `--api-key` | API Key，默认读 `OPENAI_API_KEY` |
| `--base-url` | 兼容端点地址，默认读 `OPENAI_BASE_URL` |
| `--output` | 可选，将 JSON 结果写入文件 |

脚本返回 `majority_answer`（`k` 次运行中出现次数最多的答案）及每次置换的明细。

## 背景与原理

本 skill 基于 ACL 2025 论文《Masking in Multi-hop QA: An Analysis of How Language Models Perform with Context Permutation》（Wenyu Huang et al.）的核心发现，将其转化为可在 JiuwenSwarm RAG 流程中直接使用的提示与上下文编排策略。

**核心发现：**

1. **文档顺序影响答案质量**：将检索文档按推理链顺序排列（第一跳文档在前，末跳文档在后）可显著提升模型的准确率。
2. **gold 文档之间的距离越近越好**：无关噪声文档应推至上下文两端，减少 gold 文档之间的间隔。

## When to Use

- User asks a question that requires chaining information from 2+ documents or sources (e.g. "What is the nationality of the director of [Film X]?")
- Agent has retrieved multiple documents and needs to synthesise them to answer
- Answer quality is poor or inconsistent on multi-hop or compositional questions

## Workflow

1. **Decompose the question** into ordered sub-questions (hops). Identify which piece of information must be found first to unlock the next.

2. **Retrieve documents** for each hop. For each sub-question, retrieve the most relevant document or passage.

3. **Reorder documents in forward reasoning-chain order**: place the document answering the 1st-hop question first, followed by 2nd-hop, and so on. Push noise/irrelevant documents to the beginning or end of the context, away from the gold documents.

4. **Construct the prompt** with the reordered document list and generate the answer.

5. **(Optional) Context permutation + majority voting**: randomly shuffle all documents `k` times, call the API once per shuffle (all with `temperature=0`), and pick the most common answer. This is a deterministic alternative to high-temperature sampling — instead of stochastic variation within one context, you vary the context order itself. Use `scripts/permute_and_vote.py` to automate this.

## Using the Script

Install the `openai` package if not already available:

```bash
pip install openai
```

Run with document files (one text file per retrieved document):

```bash
python scripts/permute_and_vote.py \
  --question "What country is the birthplace of the director of Inception?" \
  --docs doc1.txt doc2.txt doc3.txt \
  --k 5 \
  --output result.json
```

- `--k`: number of random shuffles to run (default: 5). Each shuffle is one API call.
- `--seed`: optional integer for reproducibility.
- `--model`: defaults to `MHQA_MODEL` env var or `gpt-4o-mini`.
- `--base-url`: optional, for OpenAI-compatible endpoints.

The script returns `majority_answer` (most common answer across all `k` runs) along with per-shuffle details.

## Prompt Template

```
You are given the following documents in the order relevant to answering the question step by step.

[Document 1 — answers sub-question 1]
{doc_1}

[Document 2 — answers sub-question 2]
{doc_2}

...

[Noise documents]
{noise_docs}

Question: {question}
Answer step by step, citing which document supports each reasoning step.
```

## Output

- The final answer generated from the reordered context.
- A brief explanation of which document supported each reasoning hop.

## Boundaries

- This skill is a **prompting and context-ordering layer only**; it does not require model fine-tuning or access to model internals.
- Not designed for single-hop retrieval or open-ended generation without a retrievable source.
- Does not replace a retrieval system; assumes documents have already been fetched.

See `references/acl2025-masking-mhqa.md` for detailed paper findings and experimental results.
