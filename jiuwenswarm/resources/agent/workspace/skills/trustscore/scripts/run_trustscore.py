"""Run TrustScore consistency evaluation for an OpenAI-compatible chat model."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import string
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any


DEFAULT_GENERATOR_MODEL = "gpt-5-mini-2025-08-07"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_MODEL_CALLS = 25
DEFAULT_MAX_QUESTION_CHARS = 4000
CHOICE_LABELS = ["A", "B", "C", "D", "E"]

logger = logging.getLogger("trustscore")
result_logger = logging.getLogger("trustscore.result")


@dataclass(frozen=True)
class DistractorRequest:
    question: str
    answer: str
    model: str
    count: int


def configure_logging() -> None:
    logging.basicConfig(format="%(levelname)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    result_logger.propagate = False
    if not result_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        result_logger.addHandler(handler)
    result_logger.setLevel(logging.INFO)


def first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def read_text(value: str | None, file_path: str | None, label: str) -> str:
    if value and file_path:
        raise ValueError(f"Pass either --{label} or --{label}-file, not both.")
    if file_path:
        return safe_workspace_path(file_path, f"{label}-file").read_text(encoding="utf-8")
    if value:
        return value
    raise ValueError(f"Missing input: pass --{label} or --{label}-file.")


def safe_workspace_path(raw_path: str, label: str) -> Path:
    root = Path.cwd().resolve()
    path = Path(raw_path)
    if not raw_path or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe --{label} path: {raw_path!r}")
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"--{label} path escapes the current workspace: {raw_path!r}") from exc
    return candidate


def normalize_answer(text: str) -> str:
    def white_space_fix(value: str) -> str:
        return " ".join(value.split())

    def handle_punc(value: str) -> str:
        exclude = set(string.punctuation + "".join(["‘", "’", "´", "`"]))
        return "".join(char if char not in exclude else " " for char in value)

    return white_space_fix(handle_punc(text.lower().replace("_", " "))).strip()


def f1_score(prediction: str, ground_truth: str) -> float:
    prediction = re.sub(r"\b(an|the)\b", " ", prediction)
    ground_truth = re.sub(r"\b(an|the)\b", " ", ground_truth)
    prediction = prediction.replace("i d be", " ")
    prediction_tokens = normalize_answer(prediction).split()
    ground_truth_tokens = normalize_answer(ground_truth).split()
    if not prediction_tokens or not ground_truth_tokens:
        return 0.0

    common = Counter(prediction_tokens) & Counter(ground_truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0

    precision = num_same / len(prediction_tokens)
    recall = num_same / len(ground_truth_tokens)
    return (2 * precision * recall) / (precision + recall)


def unique_items(items: list[str], forbidden: set[str] | None = None) -> list[str]:
    forbidden = forbidden or set()
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        cleaned = " ".join(str(item).split())
        normalized = normalize_answer(cleaned)
        if cleaned and normalized not in seen and normalized not in forbidden:
            seen.add(normalized)
            unique.append(cleaned)
    return unique


def parse_option_prediction(prediction: str, mcq_question: str) -> str:
    options = ["a", "b", "c", "d", "e"]
    option_lines = mcq_question.splitlines()[-5:]
    normalized_lines = [normalize_answer(line) for line in option_lines]
    option_contents = [normalize_answer(line[2:]) for line in normalized_lines]
    pred_option = prediction.strip().lower()
    pred_option_answer = normalize_answer(pred_option[2:])

    if pred_option == "":
        return options[option_contents.index("unsure")]
    if pred_option in options:
        return pred_option
    if pred_option in normalized_lines:
        return options[normalized_lines.index(pred_option)]
    if pred_option in option_contents:
        return options[option_contents.index(pred_option)]
    if pred_option_answer in option_contents:
        return options[option_contents.index(pred_option_answer)]
    if pred_option[:2].strip() in options:
        return pred_option[:2].strip()

    f1s = [f1_score(pred_option, line) for line in normalized_lines]
    best = max(f1s)
    if best > 0 and f1s.count(best) == 1:
        return options[f1s.index(best)]
    return options[option_contents.index("unsure")]


def compute_consistency_score(
    mcq_predictions: list[str],
    mcq_questions: list[str],
    mcq_answers: list[str],
) -> tuple[float, list[str]]:
    if not mcq_predictions:
        raise ValueError("Cannot compute TrustScore with no MCQ predictions.")
    predicted_options = [
        parse_option_prediction(prediction, mcq_questions[index])
        for index, prediction in enumerate(mcq_predictions)
    ]
    correct = sum(
        1
        for index, predicted_option in enumerate(predicted_options)
        if predicted_option == mcq_answers[index].lower()
    )
    return correct / len(predicted_options), predicted_options


def chat_text(client: Any, model: str, messages: list[dict[str, str]]) -> str:
    completion = client.chat.completions.create(model=model, messages=messages)
    content = completion.choices[0].message.content
    if isinstance(content, str):
        raw = content.strip()
    elif isinstance(content, list):
        raw = "\n".join(str(part) for part in content).strip()
    else:
        raw = str(content).strip()
    return " ".join(raw.split())


def require_parsed_message(completion: Any, output_name: str) -> Any:
    message = completion.choices[0].message
    parsed = message.parsed
    if parsed is None:
        refusal = getattr(message, "refusal", None)
        detail = refusal or "structured output parsing failed"
        raise ValueError(f"Generator model refused or failed to produce {output_name}: {detail}")
    return parsed


def generate_paraphrases(
    client: Any,
    question: str,
    model: str,
    count: int,
    base_model: type[Any],
) -> list[str]:
    class ParaphraseResponse(base_model):  # type: ignore[valid-type, misc]
        paraphrases: list[str]

    system_prompt = (
        "You generate precise paraphrases for model consistency evaluation. "
        "Keep the original meaning and answer unchanged."
    )
    user_prompt = (
        f"Generate {count} unique paraphrases of this question. "
        "Return only the structured paraphrases list.\n\n"
        f"Question: {question}"
    )
    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format=ParaphraseResponse,
    )
    parsed = require_parsed_message(completion, "paraphrases")
    return unique_items(parsed.paraphrases)


def generate_distractors(
    client: Any,
    request: DistractorRequest,
    base_model: type[Any],
) -> list[str]:
    class DistractorResponse(base_model):  # type: ignore[valid-type, misc]
        distractors: list[str]

    system_prompt = (
        "You are a precise assistant that generates multiple-choice distractors. "
        "Each distractor must be plausible, similar in form to the answer, and "
        "distinct from the answer."
    )
    user_prompt = (
        f"For the question-answer pair below, generate {request.count} unique distractors. "
        "Return only the structured distractors list.\n\n"
        f"Q: {request.question}\n"
        f"A: {request.answer}"
    )
    completion = client.beta.chat.completions.parse(
        model=request.model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format=DistractorResponse,
    )
    parsed = require_parsed_message(completion, "distractors")
    return unique_items(parsed.distractors, forbidden={normalize_answer(request.answer)})


def generate_mcqs(
    questions: list[str],
    original_answer: str,
    distractors: list[str],
    count: int,
    random_seed: int,
) -> tuple[list[str], list[str]]:
    if len(distractors) < 3:
        raise ValueError("At least 3 unique distractors are required.")
    combination_count = len(distractors) * (len(distractors) - 1) * (len(distractors) - 2) // 6
    if combination_count < count:
        raise ValueError("Not enough unique distractor combinations for requested MCQ count.")

    rng = random.Random(random_seed)
    mcq_questions: list[str] = []
    mcq_answers: list[str] = []
    used_choices: set[tuple[str, ...]] = set()
    question_pool = questions or []
    if not question_pool:
        raise ValueError("At least one question or paraphrase is required.")

    while len(mcq_questions) < count:
        selected = rng.sample(distractors, 3)
        selected_key = tuple(sorted(normalize_answer(choice) for choice in selected))
        if selected_key in used_choices:
            continue
        used_choices.add(selected_key)

        choices = selected + [original_answer, "unsure"]
        rng.shuffle(choices)
        answer_label = CHOICE_LABELS[choices.index(original_answer)].lower()
        question = question_pool[len(mcq_questions) % len(question_pool)].strip()
        mcq = "\n".join(
            [
                question,
                f"A. {choices[0].strip()}",
                f"B. {choices[1].strip()}",
                f"C. {choices[2].strip()}",
                f"D. {choices[3].strip()}",
                f"E. {choices[4].strip()}",
            ]
        )
        mcq_questions.append(mcq)
        mcq_answers.append(answer_label)

    return mcq_questions, mcq_answers


def answer_mcqs(client: Any, model: str, mcq_questions: list[str]) -> list[str]:
    predictions: list[str] = []
    system_prompt = "Answer each multiple-choice question with only one letter: A, B, C, D, or E."
    for mcq_question in mcq_questions:
        predictions.append(
            chat_text(
                client,
                model,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": mcq_question},
                ],
            )
        )
    return predictions


def run_trustscore(args: argparse.Namespace) -> dict[str, Any]:
    try:
        from openai import OpenAI
        from pydantic import BaseModel
    except ImportError as exc:
        raise RuntimeError("Install runtime dependencies with `pip install openai pydantic`.") from exc

    client_args: dict[str, Any] = {"api_key": args.api_key, "timeout": args.timeout_seconds}
    if args.base_url:
        client_args["base_url"] = args.base_url
    client = OpenAI(**client_args)

    question = read_text(args.question, args.question_file, "question")
    try:
        answer = chat_text(
            client,
            args.model,
            [
                {"role": "system", "content": "Answer the user's question directly and concisely."},
                {"role": "user", "content": question},
            ],
        )
        paraphrases = generate_paraphrases(
            client,
            question,
            args.generator_model,
            max(args.paraphrase_num, args.mcq_num),
            BaseModel,
        )
        distractors = generate_distractors(
            client,
            DistractorRequest(
                question=question,
                answer=answer,
                model=args.generator_model,
                count=args.distractor_num,
            ),
            BaseModel,
        )
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"TrustScore model call failed: {exc}") from exc

    question_pool = unique_items([question] + paraphrases)
    mcq_questions, mcq_answers = generate_mcqs(
        question_pool,
        answer,
        distractors,
        args.mcq_num,
        args.seed,
    )
    try:
        mcq_predictions = answer_mcqs(client, args.model, mcq_questions)
    except Exception as exc:
        raise RuntimeError(f"TrustScore MCQ answering failed: {exc}") from exc
    trustscore, predicted_options = compute_consistency_score(
        mcq_predictions,
        mcq_questions,
        mcq_answers,
    )
    correct_count = sum(
        1
        for index, predicted_option in enumerate(predicted_options)
        if predicted_option == mcq_answers[index]
    )
    return {
        "trustscore": trustscore,
        "model": args.model,
        "generator_model": args.generator_model,
        "question": question,
        "answer": answer,
        "mcq_count": len(mcq_questions),
        "correct_count": correct_count,
        "predicted_options": predicted_options,
        "answer_options": mcq_answers,
        "mcq_predictions": mcq_predictions,
        "mcq_questions": mcq_questions,
        "distractors": distractors,
    }


def self_test_result() -> dict[str, Any]:
    class FakeCompletions:
        @staticmethod
        def create(**_kwargs: Any) -> SimpleNamespace:
            message = SimpleNamespace(content="Barack\nObama")
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    multiline_answer = chat_text(fake_client, "fake-model", [])
    if multiline_answer != "Barack Obama":
        raise ValueError("Self-test failed: chat_text did not normalize multiline answers.")

    fake_message = SimpleNamespace(parsed=None, refusal="blocked")
    fake_completion = SimpleNamespace(choices=[SimpleNamespace(message=fake_message)])
    try:
        require_parsed_message(fake_completion, "paraphrases")
    except ValueError as exc:
        if "paraphrases: blocked" not in str(exc):
            raise ValueError("Self-test failed: parsed refusal detail was not preserved.") from exc
    else:
        raise ValueError("Self-test failed: parsed None did not raise ValueError.")

    mcq_questions = [
        "\n".join(
            [
                "Q: Who was the President of the United States in 2010?",
                "A. Barack Obama",
                "B. George W. Bush",
                "C. Bill Clinton",
                "D. Donald Trump",
                "E. Unsure",
            ]
        ),
        "\n".join(
            [
                "Q: What is the capital of France?",
                "A. Berlin",
                "B. Madrid",
                "C. Paris",
                "D. Rome",
                "E. Unsure",
            ]
        ),
        "\n".join(
            [
                "Q: Which planet is known as the Red Planet?",
                "A. Earth",
                "B. Venus",
                "C. Mars",
                "D. Jupiter",
                "E. Unsure",
            ]
        ),
    ]
    mcq_answers = ["a", "c", "c"]
    mcq_predictions = ["obama", "A", "c Mars"]
    trustscore, predicted_options = compute_consistency_score(
        mcq_predictions,
        mcq_questions,
        mcq_answers,
    )
    return {
        "trustscore": trustscore,
        "mcq_count": len(mcq_questions),
        "correct_count": sum(
            1 for index, option in enumerate(predicted_options) if option == mcq_answers[index]
        ),
        "predicted_options": predicted_options,
        "answer_options": mcq_answers,
        "self_test": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute TrustScore for an OpenAI-compatible model.")
    parser.add_argument("--question", help="Question to evaluate.")
    parser.add_argument("--question-file", help="Read the question from a UTF-8 text file.")
    parser.add_argument("--model", help="Model A to evaluate.")
    parser.add_argument(
        "--generator-model",
        default=os.environ.get("TRUSTSCORE_GENERATOR_MODEL", DEFAULT_GENERATOR_MODEL),
        help="Model used to generate paraphrases and distractors.",
    )
    parser.add_argument(
        "--api-key",
        default=first_env("TRUSTSCORE_API_KEY", "OPENAI_API_KEY", "API_KEY", "MODEL_API_KEY"),
        help=(
            "OpenAI-compatible API key. Defaults to TRUSTSCORE_API_KEY, "
            "OPENAI_API_KEY, API_KEY, or MODEL_API_KEY."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=first_env("TRUSTSCORE_BASE_URL", "OPENAI_BASE_URL", "API_BASE", "MODEL_API_BASE"),
        help=(
            "OpenAI-compatible API base URL. Defaults to TRUSTSCORE_BASE_URL, "
            "OPENAI_BASE_URL, API_BASE, or MODEL_API_BASE."
        ),
    )
    parser.add_argument("--mcq-num", type=int, default=20, help="Number of MCQs to evaluate.")
    parser.add_argument(
        "--paraphrase-num",
        type=int,
        default=20,
        help="Number of paraphrases to request from the generator model.",
    )
    parser.add_argument(
        "--distractor-num",
        type=int,
        default=20,
        help="Number of distractors to request from the generator model.",
    )
    parser.add_argument("--seed", type=int, default=0, help="Random seed for MCQ construction.")
    parser.add_argument(
        "--output",
        help="Optional JSON output path under the current workspace.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("TRUSTSCORE_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))),
        help="Per-request timeout for OpenAI-compatible API calls.",
    )
    parser.add_argument(
        "--max-model-calls",
        type=int,
        default=int(os.getenv("TRUSTSCORE_MAX_MODEL_CALLS", str(DEFAULT_MAX_MODEL_CALLS))),
        help="Maximum planned model calls allowed for one TrustScore run.",
    )
    parser.add_argument(
        "--max-question-chars",
        type=int,
        default=int(os.getenv("TRUSTSCORE_MAX_QUESTION_CHARS", str(DEFAULT_MAX_QUESTION_CHARS))),
        help="Maximum question length for cost control.",
    )
    parser.add_argument(
        "--pretty",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Pretty-print JSON output.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run local scoring self-test.")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.self_test:
        return
    if not args.model:
        raise ValueError("Missing input: pass --model.")
    if not args.api_key:
        raise ValueError(
            "Missing API key. Set TRUSTSCORE_API_KEY, OPENAI_API_KEY, API_KEY, "
            "MODEL_API_KEY, or pass --api-key."
        )
    if args.mcq_num <= 0:
        raise ValueError("--mcq-num must be greater than 0.")
    if args.paraphrase_num <= 0:
        raise ValueError("--paraphrase-num must be greater than 0.")
    if args.distractor_num < 3:
        raise ValueError("--distractor-num must be at least 3.")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be greater than 0.")
    if args.max_model_calls <= 0:
        raise ValueError("--max-model-calls must be greater than 0.")
    if args.max_question_chars <= 0:
        raise ValueError("--max-question-chars must be greater than 0.")
    planned_model_calls = args.mcq_num + 3
    if planned_model_calls > args.max_model_calls:
        raise ValueError(
            "TrustScore run exceeds the configured model-call limit: "
            f"{planned_model_calls} calls > {args.max_model_calls}. "
            "Reduce --mcq-num or pass --max-model-calls to raise the limit intentionally."
        )
    question = read_text(args.question, args.question_file, "question")
    if len(question) > args.max_question_chars:
        raise ValueError(
            "Question is too large for this safety limit: "
            f"{len(question)} chars > {args.max_question_chars}. "
            "Pass --max-question-chars to raise the limit intentionally."
        )


def emit_json(result: dict[str, Any], output: str | None, pretty: bool) -> None:
    indent = 2 if pretty else None
    rendered = json.dumps(result, ensure_ascii=False, indent=indent)
    if output:
        try:
            safe_workspace_path(output, "output").write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"Failed to write output file: {exc}") from exc
    result_logger.info(rendered)


def main() -> int:
    configure_logging()
    args = parse_args()
    try:
        validate_args(args)
        result = self_test_result() if args.self_test else run_trustscore(args)
        emit_json(result, args.output, args.pretty)
    except (RuntimeError, ValueError) as exc:
        logger.error(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001
        logger.exception("TrustScore evaluation failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
