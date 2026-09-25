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


NATIVE_APPROVAL_SOURCES = {"permission_interrupt", "confirm_interrupt"}


def native_approval_question(payload):
    if payload.get("source") not in NATIVE_APPROVAL_SOURCES:
        raise ValueError("Unsupported native approval source")
    request_id = payload.get("request_id")
    questions = payload.get("questions")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("Approval request ID is required")
    if not isinstance(questions, list) or not questions or len(questions) > 32:
        raise ValueError("Invalid approval questions")
    if any(not isinstance(q, dict) or not isinstance(q.get("question"), str)
           or not q["question"].strip() for q in questions):
        raise ValueError("Invalid approval questions")
    return dict(
        request_id=request_id,
        source=payload["source"],
        questions=deepcopy(questions),
        approval_schema=payload.get("approval_schema"),
        state="pending",
    )


def validate_native_approval_answers(interaction, answers):
    native_approval_question(interaction)
    if not isinstance(answers, list) or len(answers) != len(interaction["questions"]):
        raise ValueError("Answer every approval question exactly once")
    result = []
    for question, answer in zip(interaction["questions"], answers):
        if not isinstance(answer, dict) or answer.get("question") != question["question"]:
            raise ValueError("Approval answer does not match the pending question")
        text = answer.get("custom_input", answer.get("answer", ""))
        options = answer.get("selected_options", [])
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError("Invalid approval answer")
        if not isinstance(options, list) or len(options) > 32 or any(
            not isinstance(option, str) or not option.strip() or len(option) > 1000
            for option in options
        ):
            raise ValueError("Invalid approval answer")
        if not text.strip() and not options:
            raise ValueError("Approval answer is empty")
        result.append(dict(question=question["question"], answer=text, selected_options=options))
    return result


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
