# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

""" /recap 命令的 prompt 模板与常量 """

RECENT_MESSAGE_WINDOW = 30


def build_recap_prompt(memory: str | None, language: str = "en") -> str:
    """构建 /recap prompt

    Args:
        memory: Session memory 内容（broader context），可为 None。
        language: 语言偏好，"zh" 系列输出中文，"en" 系列输出英文。
    """
    memory_block = f"Session memory (broader context):\n{memory}\n\n" if memory else ""

    if language and language.lower().startswith("zh"):
        return (
            f"{memory_block}"
            "用户正在请求当前会话的快速回顾。"
            "用恰好1-3个短句来回答。"
            "首先说明高层任务——他们正在构建或调试什么，不要涉及实现细节。"
            "接下来：具体的下一步操作。"
            "跳过状态报告和提交记录。"
        )
    return (
        f"{memory_block}"
        "The user is requesting a quick recap of the current session. "
        "Write exactly 1-3 short sentences. "
        "Start by stating the high-level task — what they are building or debugging, not implementation details. "
        "Next: the concrete next step. "
        "Skip status reports and commit recaps."
    )


def _build_btw_prompt(
    question: str,
    language: str = "en",
) -> str:
    """构建 /btw 侧问题 prompt。

    明确告知模型这是一个独立的侧问题，
    不能使用工具，只能基于对话上下文直接回答。

    主 agent 的 system prompt 通过 SystemMessage 传递，不在此处嵌入。

    Args:
        question: 用户侧问题
        language: 语言偏好
    """
    system_reminder = (
        "<system-reminder>\n"
        "This is a side question from the user. You must answer this question directly "
        "in a single response.\n"
        "\n"
        "IMPORTANT CONTEXT:\n"
        "- You are a separate, lightweight agent spawned to answer this one question\n"
        "- The main agent is NOT interrupted - it continues working independently\n"
        "- You share the conversation context but are a completely separate instance\n"
        "- Do NOT reference being interrupted or what you were 'previously doing'\n"
        "\n"
        "CRITICAL CONSTRAINTS:\n"
        "- You have NO tools available - you cannot read files, run commands, search, "
        "or take any actions\n"
        "- This is a one-off response - there will be no follow-up turns\n"
        "- You can ONLY provide information based on what you already know from the "
        "conversation context\n"
        "- NEVER say things like 'Let me try...', 'I'll now...', 'Let me check...', "
        "or promise to take any action\n"
        "- If you don't know the answer, say so - do not offer to look it up or "
        "investigate\n"
        "\n"
        "Simply answer the question with the information you have.\n"
        "</system-reminder>\n"
    )

    if language and language.lower().startswith("zh"):
        return (
            f"{system_reminder}"
            f"请用中文回答这个侧问题。\n"
            f"问题：{question}\n"
        )
    return (
        f"{system_reminder}"
        f"\nQuestion: {question}\n"
    )