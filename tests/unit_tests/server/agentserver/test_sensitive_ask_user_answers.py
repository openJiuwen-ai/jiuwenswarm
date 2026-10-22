from jiuwenswarm.server.runtime.agent_adapter.sensitive_answers import (
    redact_sensitive_answers,
)


def test_redacts_secret_answer_values_but_keeps_question_for_history_pairing():
    answers = [
        {
            "question": "请输入 GitCode access token",
            "selected_options": [],
            "custom_input": "client-secret-value",
            "card_id": "credential-card",
        }
    ]

    assert redact_sensitive_answers(answers) == [
        {
            "question": "请输入 GitCode access token",
            "selected_options": [],
            "custom_input": "••••••",
            "card_id": "credential-card",
        }
    ]
    assert answers[0]["custom_input"] == "client-secret-value"


def test_keeps_non_sensitive_answers_unchanged():
    answers = [{"question": "请选择发布范围", "selected_options": ["公开"]}]
    assert redact_sensitive_answers(answers) == answers


def test_keeps_token_topic_choices_but_masks_free_input():
    answers = [{"question": "用哪种方式更新令牌？", "selected_options": ["让你代登录"], "custom_input": "FAKE-4470"}]
    result = redact_sensitive_answers(answers)
    assert result[0]["selected_options"] == ["让你代登录"]
    assert result[0]["custom_input"] == "••••••"
    assert answers[0]["custom_input"] == "FAKE-4470"
