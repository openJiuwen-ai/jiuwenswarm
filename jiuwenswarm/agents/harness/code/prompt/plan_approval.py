# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Plan approval definitions — templates and helpers.

Plan approval is text-only: ``exit_plan_mode`` tool_result shows the plan
plus a short marker; the next user message is interpreted by the server
(``_check_and_handle_pending_approval``) as approve or feedback.
"""

from __future__ import annotations

import re
from typing import Literal

# ── Legacy event type (wire compat only; no longer pushed to clients) ───

PLAN_APPROVAL_EVENT_TYPE = "plan.approval_required"

# Pushed to clients after the user approves and server restores normal mode.
PLAN_MODE_EXITED_EVENT_TYPE = "plan.mode_exited"

# ── Approve / Reject command prefixes ───────────────────────────────────
# The frontend sends a new user request with one of these as the query text
# after the user interacts with the approval dialog.

APPROVE_CMD_PREFIX = "plan.approve"
REJECT_CMD_PREFIX = "plan.reject"

# Set on request.params when the user explicitly approved the plan this turn.
# Consumed by ``_ensure_code_mode_state`` to allow plan → normal restoration.
PLAN_USER_APPROVED_FLAG = "_plan_user_approved"

# ── Plan approval skip（Web 专用："修改框留空 + 点击跳过"）─────────────────
# 语义：不退出 plan、不执行、也不继续修改，本轮到此结束。它复用现有的 reject
# 通道（``approved=False``，因此 ``exit_plan_mode`` 不会执行），额外通过
# ``PLAN_SKIP_PAYLOAD_KEY`` 告诉 rail 需要强制结束当前 turn，否则模型收到普通
# reject 后可能继续运行并再次弹出审批。
#
# TUI 不发送这些取值，其 approve / reject 行为完全不变。
PLAN_SKIP_PAYLOAD_KEY = "plan_skip"

# 只认 ``build_plan_approval_actions`` 实际下发的取值。不要放 "skip" / "跳过"
# 这类通用词：消费它的那段 if/elif 是所有确认类中断共用的选项映射，别的确认流
# 若出现一个"跳过"按钮会被误判成计划跳过。
PLAN_SKIP_OPTION_VALUES: frozenset[str] = frozenset({"plan_skip"})

PLAN_SKIP_FEEDBACK = {
    "cn": "用户选择跳过本次计划审批，暂不执行，也未提出修改意见。",
    "en": (
        "The user skipped this plan approval: do not implement yet, and no "
        "revision notes were given."
    ),
}

PLAN_SKIP_TURN_OUTPUT = {
    "cn": "已跳过本次计划审批。计划已保存，当前仍处于计划模式，可以继续补充意见。",
    "en": (
        "Plan approval skipped. The plan is saved and you are still in plan mode; "
        "send revision notes whenever you are ready."
    ),
}

# ── Plan approval execute（Web 专用："执行"按钮）────────────────────────────
# Web 的执行分成两次请求：本次只负责让 ``exit_plan_mode`` 真正跑完（退出 plan
# 模式），跑完立刻结束本轮、不再调模型；随后前端补发一条普通非 plan 消息，由那
# 条消息作为用户提问开启全新一轮来执行计划。
#
# 因此 Web 用独立取值 ``plan_execute``，而不是复用 ``approve``：TUI 发的仍是
# ``approve``，批准后照旧在同一轮里继续实现，行为完全不变。
PLAN_EXECUTE_PAYLOAD_KEY = "plan_execute"

PLAN_EXECUTE_OPTION_VALUES: frozenset[str] = frozenset({"plan_execute"})

# ``ctx.extra`` 标记：PlanApprovalInterruptRail 在恢复中断时设置，
# CodeAgentModeRail 在 exit_plan_mode 跑完后据此结束本轮。
PLAN_EXECUTE_CTX_KEY = "_plan_execute_deferred"

# ── Plan approval revise（Web 专用："下一步"）──────────────────────────────
# 语义：不退出 plan，按修改意见继续完善同一份计划。复用 reject 通道
# （``approved=False``），额外通过 ``PLAN_REVISE_PAYLOAD_KEY`` 让 rail 给假回执
# 包上「尚未批准、再调 exit_plan_mode」。
#
# TUI 仍发 ``reject``，不带这个键，假回执保持原文，行为不变。
PLAN_REVISE_PAYLOAD_KEY = "plan_revise"

PLAN_REVISE_OPTION_VALUES: frozenset[str] = frozenset({"plan_revise"})

# ── 进入 plan 时注入的提醒 ─────────────────────────────────────────────────
# 进入 plan 的那一轮会把一段 <system-reminder> 拼到 ``params["query"]`` 前面。
# 那段文字是给模型看的运行时上下文，不是用户说的话，所以写会话历史时必须还原
# 成用户原文，否则刷新页面 / 加载历史会把提示词当成用户提问显示出来。
PLAN_REMINDER_ORIGINAL_QUERY_KEY = "_plan_reminder_original_query"

# 三个按钮的结构与文案分开存放：结构（回传取值、是否需要输入）与语言无关，
# 只有 label 需要按语言取。
_PLAN_APPROVAL_ACTIONS: tuple[dict[str, str], ...] = (
    {"kind": "execute", "value": PLAN_EXECUTE_PAYLOAD_KEY, "requires_input": "no"},
    {"kind": "skip", "value": PLAN_SKIP_PAYLOAD_KEY, "requires_input": "empty"},
    {"kind": "revise", "value": PLAN_REVISE_PAYLOAD_KEY, "requires_input": "yes"},
)

_PLAN_ACTION_LABELS: dict[str, dict[str, str]] = {
    "cn": {"execute": "执行", "skip": "跳过", "revise": "下一步"},
    "en": {"execute": "Execute", "skip": "Skip", "revise": "Next"},
}


def plan_skip_feedback(language: str | None) -> str:
    """按语言取"跳过"时写给模型的反馈文案。

    Args:
        language: ``cn`` / ``zh`` / ``en``（大小写不敏感），其余取值按中文处理。
    """
    lang = (language or "").strip().lower()
    return PLAN_SKIP_FEEDBACK["en" if lang.startswith("en") else "cn"]


def build_plan_approval_actions(language: str) -> list[dict[str, str]]:
    """Build the Web plan-approval action descriptors.

    Web 弹窗由"执行"按钮 + 修改框 + 动态按钮组成：修改框为空时按钮是"跳过"，
    有内容时是"下一步"。三者都复用既有的 approve / reject 通道：

    - ``plan_execute``：批准并退出 plan，本轮到此结束；前端随后补发一条普通
      非 plan 消息来真正执行。
    - ``plan_skip``：留在 plan，结束本轮，不执行也不修改。
    - ``plan_revise`` + ``custom_input``：留在 plan，按修改意见继续完善同一份计划。

    TUI 不读取该字段，仍使用 ``questions[].options`` 的 approve / reject，
    批准后在同一轮里直接继续实现。

    Args:
        language: ``cn`` 或 ``en``。

    Returns:
        动作描述列表，供 Web 渲染按钮与决定回传的 ``selected_options``。
    """
    labels = _PLAN_ACTION_LABELS.get(language, _PLAN_ACTION_LABELS["cn"])
    return [
        {**action, "label": labels[action["kind"]]}
        for action in _PLAN_APPROVAL_ACTIONS
    ]

PlanUserIntent = Literal["approve", "revise"]

# ── Pending approval marker (fallback — appended to exit_plan_mode tool_result) ──

_PENDING_APPROVAL_MARKER_CN = (
    "\n\n---\n"
    "**仍在规划模式**，等待你确认计划。直接在输入框回复：\n"
    "- 同意开始执行：回复「好」「可以」「按计划实现」等（批准后将退出规划模式）\n"
    "- 需要修改：直接说明修改意见"
)

_PENDING_APPROVAL_MARKER_EN = (
    "\n\n---\n"
    "**Still in plan mode** — reply in chat to review this plan:\n"
    '- To approve and start implementation: "ok", "approve", "implement the plan", etc.\n'
    "- To revise: describe what to change"
)

PENDING_APPROVAL_MARKER = {
    "cn": _PENDING_APPROVAL_MARKER_CN,
    "en": _PENDING_APPROVAL_MARKER_EN,
}

# ── Approved notification (injected on the NEXT request when user approves) ──

_APPROVED_NOTIFICATION_CN = (
    "\n\n<system-reminder>\n"
    "用户已批准你的计划。立即开始执行。\n"
    "你现在处于 normal 模式，可以编辑文件、运行命令、进行修改。\n"
    "## 已批准的计划：\n{plan_content}\n"
    "</system-reminder>"
)

_APPROVED_NOTIFICATION_EN = (
    "\n\n<system-reminder>\n"
    "User has approved your plan. Proceed with implementation.\n"
    "You are now in normal mode. You can edit files, run commands, and make changes.\n"
    "## Approved Plan:\n{plan_content}\n"
    "</system-reminder>"
)

APPROVED_NOTIFICATION = {
    "cn": _APPROVED_NOTIFICATION_CN,
    "en": _APPROVED_NOTIFICATION_EN,
}

# ── Feedback injection (injected on the NEXT request when user gives feedback) ──

_FEEDBACK_INJECTION_CN = (
    "\n\n<system-reminder>\n"
    "用户要求修订计划（尚未批准执行）。请只修改计划文件，不要实现产品代码。\n"
    "你仍处于 plan 模式：禁止编辑计划文件以外的任何文件，禁止运行写操作。\n"
    "**修订完成后，再次调用 exit_plan_mode 提交审批。**\n\n"
    "**用户修订意见：**\n{user_message}\n"
    "</system-reminder>"
)

_FEEDBACK_INJECTION_EN = (
    "\n\n<system-reminder>\n"
    "The user wants plan revisions (implementation is NOT approved yet).\n"
    "You are still in plan mode — edit ONLY the plan file. Do NOT implement product code.\n"
    "Once revised, call exit_plan_mode again to submit for approval.\n\n"
    "**User revision request:**\n{user_message}\n"
    "</system-reminder>"
)

FEEDBACK_INJECTION = {
    "cn": _FEEDBACK_INJECTION_CN,
    "en": _FEEDBACK_INJECTION_EN,
}


def wrap_plan_revision_feedback(user_message: str, language: str = "cn") -> str:
    """Wrap a Web「下一步」reply so the model sees it was not approved.

    Only the Web revise path sets ``plan_revise``. TUI reject stays unwrapped.
    """
    lang = "en" if (language or "").strip().lower().startswith("en") else "cn"
    # 用户原文可能含花括号，不能走 str.format。
    return FEEDBACK_INJECTION[lang].replace("{user_message}", user_message or "")

# ── Intent detection helpers ─────────────────────────────────────────────

_APPROVAL_KEYWORDS_CN = frozenset({
    "好", "可以", "批准", "同意", "行", "没问题", "通过",
    "嗯", "好的", "可以了", "就这样", "ok", "okay", "approve",
})
_APPROVAL_KEYWORDS_EN = frozenset({
    "ok", "okay", "approve", "yes", "yeah", "yep", "good",
    "looks good", "approved", "go ahead", "proceed",
})

_REJECT_PREFIXES_CN = ("不行", "不好", "不要", "别", "不对", "不可以", "不同意")
_REJECT_PREFIXES_EN = ("reject", "no,", "no ", "don't", "do not")

_REVISION_SUBSTRINGS = (
    "修改", "改一下", "改成", "要改", "调整", "补充", "添加", "增加", "删除", "换成",
    "重新", "细化", "重写", "不够", "缺少", "有问题", "不满意", "换一个",
    "revise", "change", "modify", "update the plan", "add more", "remove",
    "instead", "rather than", "should also", "missing", "redo",
)

_IMPLEMENT_PATTERNS = (
    re.compile(r"按.{0,6}计划.{0,4}(实现|执行|做)"),
    re.compile(r"按.{0,6}方案.{0,4}(实现|执行|做)"),
    re.compile(r"开始(实现|执行|写代码|干活|做)"),
    re.compile(r"(可以|去|动手|直接)(实现|执行|做|开工)"),
    re.compile(r"^(实现|执行|开工)吧?[。.!]?$"),
    re.compile(r"就这样(做|执行|实现)"),
    re.compile(r"implement(\s+the)?\s+plan", re.IGNORECASE),
    re.compile(r"(start|go ahead|proceed)\s+(with\s+)?(implement|implementation|execution|building)", re.IGNORECASE),
    re.compile(r"^implement(\s+it)?[.!?]?$", re.IGNORECASE),
    re.compile(r"^(execute|build|ship)(\s+it|\s+the\s+plan)?[.!?]?$", re.IGNORECASE),
    re.compile(r"let'?s\s+(implement|build|execute)", re.IGNORECASE),
)


def classify_plan_user_intent(user_message: str) -> PlanUserIntent:
    """Classify the user's response after ``exit_plan_mode``.

    Returns:
        ``"approve"`` when the user wants to exit plan mode and implement.
        ``"revise"`` when the user wants to change the plan only.
    """
    text = user_message.strip()
    if not text:
        return "revise"

    if text.startswith(APPROVE_CMD_PREFIX):
        return "approve"
    if text.startswith(REJECT_CMD_PREFIX):
        return "revise"

    lower = text.lower()

    for prefix in _REJECT_PREFIXES_CN:
        if text.startswith(prefix):
            return "revise"
    for prefix in _REJECT_PREFIXES_EN:
        if lower.startswith(prefix):
            return "revise"

    if _has_revision_intent(text, lower):
        return "revise"

    if _has_implementation_intent(text):
        return "approve"

    if _is_pure_approval(lower):
        return "approve"

    return "revise"


def is_user_approving(user_message: str) -> bool:
    """Return ``True`` when the user message approves the plan."""
    return classify_plan_user_intent(user_message) == "approve"


def is_direct_plan_implement_request(user_message: str) -> bool:
    """True when the user clearly asks to implement an existing plan via chat.

    Stronger than bare ``好``/``可以`` — used when there is no pending
    ``exit_plan_mode`` gate but a plan file already exists in plan mode.
    """
    text = user_message.strip()
    if not text:
        return False
    if text.startswith(APPROVE_CMD_PREFIX):
        return True
    if classify_plan_user_intent(text) != "approve":
        return False
    return _has_implementation_intent(text)


def _has_revision_intent(text: str, lower: str) -> bool:
    for kw in _REVISION_SUBSTRINGS:
        if kw in text or kw in lower:
            return True
    return False


def _has_implementation_intent(text: str) -> bool:
    for pattern in _IMPLEMENT_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _is_pure_approval(lower: str) -> bool:
    for kw in _APPROVAL_KEYWORDS_CN | _APPROVAL_KEYWORDS_EN:
        if lower == kw or lower.startswith(kw + " ") or lower.startswith(kw + "。"):
            return True

    if len(lower) <= 12:
        for prefix in ("好", "可以", "行", "ok", "yes", "approve", "go", "yep", "proceed"):
            if lower.startswith(prefix):
                return True

    return False


def extract_feedback_from_reject(user_message: str) -> str:
    """Extract feedback text from a rejection command.

    Handles formats like: ``plan.reject <feedback>`` or plain feedback text.

    Args:
        user_message: The full user message.

    Returns:
        The extracted feedback text, or the full message if no prefix found.
    """
    text = user_message.strip()
    if text.startswith(REJECT_CMD_PREFIX):
        rest = text[len(REJECT_CMD_PREFIX):].strip()
        return rest if rest else text
    return text


__all__ = [
    "PLAN_APPROVAL_EVENT_TYPE",
    "PLAN_MODE_EXITED_EVENT_TYPE",
    "APPROVE_CMD_PREFIX",
    "REJECT_CMD_PREFIX",
    "PLAN_EXECUTE_CTX_KEY",
    "PLAN_EXECUTE_OPTION_VALUES",
    "PLAN_EXECUTE_PAYLOAD_KEY",
    "PLAN_REVISE_OPTION_VALUES",
    "PLAN_REVISE_PAYLOAD_KEY",
    "PLAN_SKIP_FEEDBACK",
    "PLAN_SKIP_OPTION_VALUES",
    "PLAN_REMINDER_ORIGINAL_QUERY_KEY",
    "PLAN_SKIP_PAYLOAD_KEY",
    "PLAN_SKIP_TURN_OUTPUT",
    "PLAN_USER_APPROVED_FLAG",
    "PlanUserIntent",
    "build_plan_approval_actions",
    "PENDING_APPROVAL_MARKER",
    "APPROVED_NOTIFICATION",
    "FEEDBACK_INJECTION",
    "wrap_plan_revision_feedback",
    "classify_plan_user_intent",
    "is_direct_plan_implement_request",
    "is_user_approving",
    "extract_feedback_from_reject",
    "plan_skip_feedback",
]
