# Masking in Multi-hop QA: An Analysis of How Language Models Perform with Context Permutation

**Authors:** Wenyu Huang, Pavlos Vougiouklis, Mirella Lapata, Jeff Z. Pan  
**Venue:** ACL 2025 (Long Papers), pages 17781–17795  
**Paper:** https://aclanthology.org/2025.acl-long.869  
**Code:** https://github.com/hwy9855/MultiHopQA-Reasoning

---

## Overview

This paper investigates how Language Models (LMs) reason over multi-hop questions when prompted with multiple retrieved documents. The central finding is that the **causal mask** in decoder-only Transformer models systematically hinders cross-document reasoning, and that document ordering within the context window has a measurable effect on multi-hop QA accuracy.

---

## Key Findings

### 1. Document Order Matters (Forward vs. Backward)

- **Forward order**: gold documents are placed in the same order as the reasoning chain (1st-hop doc first, last-hop doc last).
- **Backward order**: reverse of forward.
- **Finding**: Fine-tuned decoder-only models (Qwen 2.5, Llama 3.x) consistently perform better in the **forward** setting, even though training data is presented in no particular order. This emergent behaviour is amplified by fine-tuning.
- **Flan-T5**: the forward advantage is even cleaner for encoder-decoder models thanks to bidirectional attention.

### 2. Distance between Gold Documents Matters

- Inserting more noise documents between gold documents degrades performance for all non-fine-tuned models.
- Fine-tuned models (especially with bidirectional attention) are more robust to increased distance.
- **Practical implication**: cluster relevant documents together and push irrelevant ones to the ends of the context.

### 3. Bidirectional Attention Helps

- Replacing the causal mask with a prefix (bidirectional) mask and fine-tuning improves accuracy and robustness to permutations.
- On MuSiQue dev set, Qwen 2.5 7B improves from 58.05% (FT) to 62.96% (FT+Bi) in forward setup.
- Models with bidirectional attention show smaller performance variance across permutations.

### 4. Encoder-Decoder Models are Better Zero-Shot MHQA Solvers

- Flan-T5 xl (3B) outperforms all decoder-only models under 8B parameters in zero-shot and CoT settings.
- Bidirectional attention allows later documents to attend to earlier ones, critical for chained reasoning.

### 5. Attention Weights Correlate with Correctness

- When a model answers a multi-hop question correctly, it tends to assign a **higher peak attention score** to at least one relevant document.
- **Sampling heuristic**: generate answers with several document permutations; select the answer produced when the model's peak attention score (Information Contribution score) is highest.
- Applied to Qwen 2.5 7B: accuracy improves from 28.6% → 33.7% using this heuristic alone (no fine-tuning).

---

## Experimental Setup

- **Dataset**: MuSiQue (2–4 hop questions, 20 documents per question, 2–4 gold + noise)
- **Models evaluated**:
  - Encoder-decoder: Flan-T5 (small/80M → xxl/11B)
  - Decoder-only: Qwen 2.5 (0.5B–14B), Llama 3.2 (1B, 3B), Llama 3.1 (8B)
- **Setups**: Answer Only, CoT Zero-shot, Finetuned, Finetuned + Bi (bidirectional mask)
- **Permutations tested**: Original, Forward, Backward, Forward\_i (i=0–5 noise docs between gold), Remove First

---

## Information Contribution (IC) Score

The paper introduces the **IC score** to measure how much each document contributes to the model's answer:

```
IC_l(d) = (1 / |A| |H|) * Σ_h Σ_a GAW_l,h(a, d)
```

Where `GAW` is the grouped attention weight from answer tokens `a` to document token group `d`, averaged over attention heads `H` and answer tokens `A`.

**Key observation**: the IC score for the final-hop document peaks sharply in the last layers when the model answers correctly.

---

## Implications for JiuwenSwarm RAG Pipeline

| Finding | Actionable Implication |
|---------|----------------------|
| Forward ordering improves accuracy | Decompose the question, retrieve per hop, reorder docs in chain order |
| Distance degrades performance | Place gold docs adjacent; push noise to context borders |
| Attention peak → correct answer | Use permutation sampling + IC heuristic for low-confidence answers |
| Encoder-decoder better zero-shot | Prefer Flan-T5 family for MHQA if fine-tuning is not available |
| Bidirectional attention helps | Fine-tune with prefix mask for best robustness |

---

## Citation

```bibtex
@inproceedings{huang-etal-2025-masking,
    title = "Masking in Multi-hop {QA}: An Analysis of How Language Models Perform with Context Permutation",
    author = "Huang, Wenyu  and
      Vougiouklis, Pavlos  and
      Lapata, Mirella  and
      Pan, Jeff Z.",
    booktitle = "Proceedings of the 63rd Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers)",
    month = jul,
    year = "2025",
    address = "Vienna, Austria",
    publisher = "Association for Computational Linguistics",
    url = "https://aclanthology.org/2025.acl-long.869",
    pages = "17781--17795",
}
```
