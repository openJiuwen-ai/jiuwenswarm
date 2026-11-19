# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared bounded contract for structured ask-user interactions."""

MAX_STRUCTURED_QUESTIONS = 4

__all__ = ["MAX_STRUCTURED_QUESTIONS", "build_ask_user_interactive_input"]


def build_ask_user_interactive_input(request_id: str, answers: list[dict]):
    """Build the same native interrupt answer for ordinary and group chats."""
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    interactive_input = InteractiveInput()
    answers_dict = {}
    free_text_answer: str | None = None
    for answer in answers:
        if isinstance(answer, dict):
            question_text = str(answer.get("question", "") or "").strip()
            selected_options = answer.get("selected_options", [])
            custom_input = str(answer.get("custom_input", "") or "").strip()
            if selected_options and isinstance(selected_options, list):
                # Normalize each option to a stripped string, drop empties.
                cleaned_options = [
                    str(raw_option or "").strip()
                    for raw_option in selected_options
                    if str(raw_option or "").strip()
                ]
                if custom_input:
                    # "Other" is only a UI placeholder. Preserve normal
                    # multi-select choices and append the user's text.
                    normal_options = [
                        option for option in cleaned_options if option != "Other"
                    ]
                    if normal_options:
                        answer_value: Any = [*normal_options, custom_input]
                    else:
                        answer_value = custom_input
                elif len(cleaned_options) == 1:
                    # Bare "Other" without custom text is incomplete (#2330).
                    sole = cleaned_options[0]
                    answer_value = "" if sole == "Other" else sole
                elif cleaned_options:
                    # Multi-select: preserve real choices; drop placeholder-only Other.
                    normal_options = [
                        option for option in cleaned_options if option != "Other"
                    ]
                    answer_value = normal_options if normal_options else ""
                else:
                    answer_value = ""
            elif custom_input:
                answer_value = custom_input
            else:
                answer_value = ""
            if question_text and answer_value:
                answers_dict[question_text] = answer_value
            elif answer_value:
                free_text_answer = (
                    answer_value
                    if isinstance(answer_value, str)
                    else ", ".join(answer_value)
                )
    if not answers_dict and free_text_answer:
        answers_dict["__free_text__"] = free_text_answer
    interactive_input.update(request_id, {"answers": answers_dict})
    return interactive_input
