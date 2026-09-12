"""Context permutation with majority voting for multi-hop QA.

Randomly shuffles retrieved documents k times and calls an OpenAI-compatible
chat API once per shuffle. Returns the most common answer (majority vote).

This is an API-only alternative to high-temperature sampling: instead of
sampling stochastically from one context, we deterministically vary the
context order and aggregate answers. Based on findings from:

  Huang et al. (ACL 2025) "Masking in Multi-hop QA: An Analysis of How
  Language Models Perform with Context Permutation"
  https://aclanthology.org/2025.acl-long.869
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import random
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _emit_result(rendered: str) -> None:
    result_logger = logging.getLogger("mhqa.result")
    if not result_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        result_logger.addHandler(handler)
    result_logger.setLevel(logging.INFO)
    result_logger.propagate = False
    result_logger.info("%s", rendered)


_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer the question based only on the "
    "provided documents. Be concise: output the answer and nothing else."
)

_USER_TEMPLATE = """\
{context}

Question: {question}
Answer:"""


def _read(path: str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8").strip()


def _build_context(docs: list[str], order: list[int]) -> str:
    parts = []
    for rank, idx in enumerate(order, 1):
        parts.append(f"[Document {rank}]\n{docs[idx]}")
    return "\n\n".join(parts)


def _shuffled_orders(n: int, k: int, seed: int | None) -> list[list[int]]:
    """Return k distinct random permutations of range(n)."""
    rng = random.Random(seed)
    orders: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    base = list(range(n))
    max_attempts = k * 20
    attempts = 0
    while len(orders) < k and attempts < max_attempts:
        perm = base[:]
        rng.shuffle(perm)
        key = tuple(perm)
        if key not in seen:
            seen.add(key)
            orders.append(perm)
        attempts += 1
    if len(orders) < k:
        logger.warning(
            "Only %d distinct permutations possible for %d docs; using %d.",
            len(orders), n, len(orders),
        )
    return orders


def _call_api(client, model: str, question: str, context: str) -> str:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_TEMPLATE.format(
                context=context, question=question
            )},
        ],
        temperature=0,
    )
    return response.choices[0].message.content.strip()


def _majority_vote(answers: list[str]) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = collections.Counter(answers)
    winner = max(counts, key=lambda a: counts[a])
    return winner, dict(counts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Majority voting via context permutation for multi-hop QA. "
            "Shuffles retrieved documents k times and picks the most common answer."
        )
    )
    parser.add_argument("--question", required=True, help="The multi-hop question.")
    parser.add_argument(
        "--docs",
        nargs="+",
        required=True,
        metavar="FILE",
        help="Paths to document text files (one per retrieved document).",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=5,
        help="Number of random shuffles to run (default: 5).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for reproducibility (default: random).",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("MHQA_MODEL", "gpt-4o-mini"),
        help="Model name. Defaults to MHQA_MODEL env var or gpt-4o-mini.",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("OPENAI_API_KEY"),
        help="API key. Defaults to OPENAI_API_KEY env var.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL"),
        help="Optional base URL for OpenAI-compatible endpoints.",
    )
    parser.add_argument("--output", help="Optional path to write JSON result.")
    parser.add_argument(
        "--pretty",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    args = parse_args()

    try:
        from openai import OpenAI
    except ImportError:
        logger.error("openai package not installed. Run: pip install openai")
        return 2

    if not args.api_key:
        logger.error("Missing API key. Set OPENAI_API_KEY or pass --api-key.")
        return 2

    client_kwargs: dict = {"api_key": args.api_key}
    if args.base_url:
        client_kwargs["base_url"] = args.base_url
    client = OpenAI(**client_kwargs)

    docs: list[str] = []
    for path in args.docs:
        try:
            docs.append(_read(path))
        except OSError as exc:
            logger.error("Cannot read document: %s", exc)
            return 2

    orders = _shuffled_orders(len(docs), args.k, args.seed)

    answers: list[str] = []
    runs: list[dict] = []
    for i, order in enumerate(orders):
        context = _build_context(docs, order)
        answer = _call_api(client, args.model, args.question, context)
        answers.append(answer)
        runs.append({"shuffle": i + 1, "order": order, "answer": answer})
        logger.warning("shuffle=%d  order=%s  answer=%s", i + 1, order, answer)

    winner, vote_counts = _majority_vote(answers)
    result = {
        "question": args.question,
        "majority_answer": winner,
        "vote_counts": vote_counts,
        "num_shuffles": len(orders),
        "runs": runs,
    }

    indent = 2 if args.pretty else None
    rendered = json.dumps(result, ensure_ascii=False, indent=indent)
    if args.output:
        Path(args.output).expanduser().write_text(rendered + "\n", encoding="utf-8")
    _emit_result(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
