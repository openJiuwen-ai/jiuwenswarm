"""Validate observed information questions without granting approval authority."""

from copy import deepcopy


def information_question(payload):
    source = payload.get("source", "")
    questions = payload.get("questions")
    request_id = payload.get("request_id")
    error = "This interaction requires the existing non-voice interaction channel"
    if (
        source not in {"", "ask_user", "ask_user_interrupt"}
        or payload.get("approval_schema")
        or payload.get("evolution_meta")
    ):
        raise ValueError(error)
    if not isinstance(request_id, str) or not request_id:
        raise ValueError(error)
    if not isinstance(questions, list) or not questions:
        raise ValueError(error)
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError(error)
        text = question.get("question")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(error)
    return dict(
        request_id=payload["request_id"],
        source=source,
        questions=deepcopy(questions),
        state="pending",
    )


def validate_answers(interaction, answers):
    """Bind ordered voice answers to Core's original observed questions."""
    information_question(interaction)
    if not isinstance(answers, list) or len(answers) != len(interaction["questions"]):
        raise ValueError("Answer every observed question exactly once")
    resolved = []
    for question, answer in zip(interaction["questions"], answers):
        if isinstance(answer, str):
            answer = {"question": question["question"], "answer": answer}
        if (
            not isinstance(answer, dict)
            or set(answer) - {"question", "answer", "selected_options"}
            or answer.get("question") != question["question"]
        ):
            raise ValueError("Answer does not match the observed question")
        text, options = answer.get("answer", ""), answer.get("selected_options", [])
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError("Invalid information answer")
        if not isinstance(options, list) or len(options) > 32:
            raise ValueError("Invalid information answer")
        for option in options:
            if not isinstance(option, str) or not option.strip() or len(option) > 1000:
                raise ValueError("Invalid information answer")
        if not (text.strip() or options):
            raise ValueError("Invalid information answer")
        resolved.append(dict(answer))
    return resolved
