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


def approval_cards(interaction):
    """Summarize a pending approval card for voice; None for voice-answerable questions."""
    interaction = interaction or {}
    if interaction.get("source") not in NATIVE_APPROVAL_SOURCES or interaction.get("state") != "pending":
        return None
    return [
        dict(header=str(q.get("header") or "")[:200], message=str(q.get("question") or "")[:500])
        for q in (interaction.get("questions") or [])[:3]
        if isinstance(q, dict)
    ]


def approval_option_labels(question):
    """The labels a card offers; a spoken answer must be exactly one of them."""
    labels = []
    for option in question.get("options") or []:
        label = option.get("label") if isinstance(option, dict) else option
        if isinstance(label, str) and label.strip():
            labels.append(label.strip())
    return labels


def voice_approval_answers(interaction, answers):
    """Map spoken answers onto the card's own options; anything else is refused.

    Whether the user clearly said yes or no is the voice prompt's job; this only
    guarantees that what reaches Core is an option the card itself offered.
    """
    native_approval_question(interaction)
    questions = interaction["questions"]
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ValueError("Answer every approval question exactly once")
    result = []
    for question, answer in zip(questions, answers):
        labels = approval_option_labels(question)
        if isinstance(answer, dict):
            picked = answer.get("selected_options") or []
            if answer.get("custom_input") or answer.get("answer"):
                raise ValueError("Approval answer must be one of the card options: " + " / ".join(labels))
        else:
            picked = [answer]
        picked = [str(p).strip() for p in picked if isinstance(p, str)]
        if len(picked) != 1 or picked[0] not in labels:
            raise ValueError("Approval answer must be one of the card options: " + " / ".join(labels))
        item = dict(question=question["question"], selected_options=picked)
        if isinstance(question.get("card_id"), str) and question["card_id"]:
            item["card_id"] = question["card_id"]
        result.append(item)
    return result


def validate_native_approval_answers(interaction, answers):
    native_approval_question(interaction)
    if not isinstance(answers, list) or len(answers) != len(interaction["questions"]):
        raise ValueError("Answer every approval question exactly once")
    result = []
    for question, answer in zip(interaction["questions"], answers):
        if not isinstance(answer, dict):
            raise ValueError("Approval answer does not match the pending question")
        answer_question = answer.get("question")
        if answer_question is None:
            answer_question = question["question"]
        elif answer_question != question["question"]:
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
        item = dict(
            question=question["question"], answer=text, selected_options=options
        )
        card_id = answer.get("card_id")
        if isinstance(card_id, str):
            card_id = card_id.strip()
            if card_id and len(card_id) <= 128:
                item["card_id"] = card_id
        result.append(item)
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
