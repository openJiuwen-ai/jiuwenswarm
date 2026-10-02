from jiuwenswarm.server.runtime.agent_adapter.sensitive_answers import (
    redact_sensitive_answers,
)


def test_redacts_secret_answer_values_but_keeps_question_for_history_pairing():
    answers = [
        {
            "question": "请输入 GitCode access token",
            "selected_options": ["mt_live_secret"],
            "custom_input": "client-secret-value",
            "card_id": "credential-card",
        }
    ]

    assert redact_sensitive_answers(answers) == [
        {
            "question": "请输入 GitCode access token",
            "selected_options": ["••••••"],
            "custom_input": "••••••",
            "card_id": "credential-card",
        }
    ]
    assert answers[0]["custom_input"] == "client-secret-value"


def test_keeps_non_sensitive_answers_unchanged():
    answers = [{"question": "请选择发布范围", "selected_options": ["公开"]}]
    assert redact_sensitive_answers(answers) == answers
