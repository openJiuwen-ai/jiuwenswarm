# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A2UI v0.8 protocol adapter and public protocol facade."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.common_modifiers import remove_strict_validation
from a2ui.schema.constants import A2UI_CLOSE_TAG, A2UI_OPEN_TAG, VERSION_0_8
from a2ui.schema.manager import A2uiSchemaManager

from jiuwenswarm.server.runtime.a2ui.parser import (
    coerce_message_list,
    may_contain_a2ui_content,
    parse_a2ui_response,
)
from jiuwenswarm.server.runtime.a2ui.prompt_instructions import (
    build_a2ui_autonomy_instruction,
    build_a2ui_browser_workflow_instruction,
)
from jiuwenswarm.server.runtime.a2ui.stream_guard import A2UIStreamGuard
from jiuwenswarm.server.runtime.a2ui.text_formatter import format_for_text_channel
from jiuwenswarm.server.runtime.a2ui.types import A2UIExample, A2UIResponsePart, A2UIValidationResult
from jiuwenswarm.server.runtime.a2ui.validator import (
    validate_a2ui_messages,
    validate_a2ui_response,
)


A2UI_ACTIVE_PROTOCOL_VERSION = VERSION_0_8
A2UI_CLIENT_EVENT_TYPE = "a2ui.client_event"
logger = logging.getLogger(__name__)

_SDK_JSON_OBJECT_WORKFLOW_LINE = (
    "- The JSON part MUST be a single, raw JSON object (usually a list of A2UI "
    "messages) and MUST validate against the provided A2UI JSON SCHEMA."
)
_JWC_JSON_LIST_WORKFLOW_LINE = (
    "- The JSON part MUST be a JSON list of A2UI messages and MUST validate "
    "against the provided A2UI JSON SCHEMA."
)


def _resources_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "resources"


def _examples_dir(version: str) -> Path:
    # Keep the on-disk directory version literal aligned with the SDK version.
    return _resources_dir() / "a2ui" / "examples" / version


def _load_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_prompt_contract(prompt: str) -> str:
    """Remove SDK default wording that conflicts with jiuwenswarm's v0.8 contract."""
    return prompt.replace(
        _SDK_JSON_OBJECT_WORKFLOW_LINE,
        _JWC_JSON_LIST_WORKFLOW_LINE,
    )


class A2UIProtocolSpec:
    """Versioned A2UI protocol adapter.

    The registry starts with v0.8 only. Future versions should be added as new
    A2UIProtocolSpec instances rather than modifying call sites.
    """

    def __init__(self, version: str) -> None:
        if version != VERSION_0_8:
            raise ValueError(f"Unsupported A2UI protocol version: {version}")
        self.version = version
        self.examples_dir = _examples_dir(version)
        self.schema_manager = A2uiSchemaManager(
            version=version,
            catalogs=[BasicCatalog.get_config(version=version)],
            schema_modifiers=[remove_strict_validation],
        )

    @property
    def catalog(self):
        return self.schema_manager.get_selected_catalog()

    def build_prompt(
        self,
        language: str = "en",
        *,
        include_browser_workflows: bool = False,
    ) -> str:
        if language in {"zh", "cn"}:
            role = (
                "JiuwenSwarm 支持可选的 A2UI 输出格式。当富交互界面适合当前回答时，"
                "可以生成严格符合 A2UI 0.8 schema 的消息。"
            )
            ui = (
                "当用户需要列表、卡片、表单、确认结果、可点击操作或结构化信息比较时使用 A2UI。"
                "普通解释、简短回答、敏感内容或不需要交互的回答保持纯文本。"
            )
            workflow = (
                "如果使用 A2UI，回答可以包含说明文本和一个或多个 A2UI JSON block。"
                f"每个 block 必须用 {A2UI_OPEN_TAG} 和 {A2UI_CLOSE_TAG} 包装。"
                "block 内必须是 JSON list，list 中每个元素都是 A2UI server-to-client message。"
                "必须先输出 beginRendering，再输出 surfaceUpdate；如果使用数据绑定，再按需输出 dataModelUpdate。"
                "父组件必须先于子组件出现。不要输出其他协议版本的 schema、字段或组件。"
            )
        else:
            role = (
                "JiuwenSwarm supports an optional A2UI output format. When a rich "
                "interactive answer is appropriate, generate messages that "
                "strictly validate against the A2UI 0.8 schema."
            )
            ui = (
                "Use A2UI for lists, cards, forms, confirmations, clickable "
                "actions, and structured comparisons. Keep ordinary explanations, "
                "short answers, sensitive answers, and non-interactive replies as "
                "plain text."
            )
            workflow = (
                "When using A2UI, the response may contain conversational text and "
                f"one or more A2UI JSON blocks wrapped in {A2UI_OPEN_TAG} and "
                f"{A2UI_CLOSE_TAG}. The JSON inside each block MUST be a JSON list "
                "of A2UI server-to-client messages. Emit beginRendering before "
                "surfaceUpdate; include dataModelUpdate only when data binding is "
                "needed. List parent components before child components. Do not "
                "emit schema, fields, or components from any protocol version "
                "other than 0.8."
            )

        workflow = f"{workflow}\n\n{build_a2ui_autonomy_instruction(language)}"
        if include_browser_workflows:
            workflow = f"{workflow}\n\n{build_a2ui_browser_workflow_instruction()}"
        prompt = self.schema_manager.generate_system_prompt(
            role_description=role,
            workflow_description=workflow,
            ui_description=ui,
            include_schema=True,
            include_examples=False,
            validate_examples=True,
        )
        prompt = _normalize_prompt_contract(prompt)
        examples = self.render_examples(validate=True)
        if examples:
            prompt = f"{prompt}\n\n### Examples:\n{examples}"
        return prompt

    def load_examples(self) -> list[A2UIExample]:
        examples: list[A2UIExample] = []
        if not self.examples_dir.exists():
            return examples
        for path in sorted(self.examples_dir.glob("*.json")):
            messages = coerce_message_list(_load_json_file(path))
            if messages is not None:
                examples.append(A2UIExample(path.stem, path, messages))
        return examples

    def render_examples(self, *, validate: bool = False) -> str:
        rendered: list[str] = []
        for example in self.load_examples():
            if validate:
                self.validate_messages(example.messages)
            rendered.append(
                f"---BEGIN {example.name}---\n"
                f"{json.dumps(example.messages, ensure_ascii=False, indent=2)}\n"
                f"---END {example.name}---"
            )
        return "\n\n".join(rendered)

    @staticmethod
    def parse_response(content: str) -> list[A2UIResponsePart]:
        return parse_a2ui_response(content)

    def validate_messages(self, messages: list[dict[str, Any]]) -> None:
        validate_a2ui_messages(self.catalog, messages)

    def validate_response(self, content: str) -> A2UIValidationResult:
        return validate_a2ui_response(
            content,
            parse_response=self.parse_response,
            validate_messages=self.validate_messages,
        )

    @staticmethod
    def may_contain_a2ui_content(content: str) -> bool:
        """Return whether content looks like tagged, raw, or JSONL A2UI output."""
        return may_contain_a2ui_content(content)

    def build_repair_prompt(
        self,
        invalid_content: str,
        validation_error: str,
        user_query: str | None = None,
    ) -> str:
        query_section = (
            f"Original user request:\n{user_query}\n\n"
            if user_query
            else ""
        )
        schema_prompt = self.build_prompt(language="en")
        return (
            "Your previous A2UI response was invalid. "
            f"Validation error: {validation_error}\n\n"
            f"{query_section}"
            "Return only a valid A2UI 0.8 response. Every A2UI JSON block must be "
            f"wrapped in {A2UI_OPEN_TAG} and {A2UI_CLOSE_TAG}; each block must "
            "contain a JSON list of server-to-client messages that validates "
            "against the provided schema. Do not call tools. Do not explain the "
            "repair.\n\n"
            f"Schema and examples:\n{schema_prompt}\n\n"
            f"Invalid response:\n{invalid_content}"
        )

    def format_for_text_channel(self, content: str) -> str:
        return format_for_text_channel(
            content,
            parse_response=self.parse_response,
            validate_response=self.validate_response,
        )


@lru_cache(maxsize=4)
def get_protocol_spec(version: str = A2UI_ACTIVE_PROTOCOL_VERSION) -> A2UIProtocolSpec:
    """Return a cached protocol spec.

    The A2UI schema and bundled examples are loaded once per protocol version;
    runtime hot-reload is intentionally not supported.
    """
    if version != VERSION_0_8:
        raise ValueError(f"Unsupported A2UI protocol version: {version}")
    return A2UIProtocolSpec(version)


def build_a2ui_prompt_section(
    language: str = "en",
    *,
    include_browser_workflows: bool = False,
) -> str:
    return get_protocol_spec().build_prompt(
        language,
        include_browser_workflows=include_browser_workflows,
    )


def format_a2ui_for_text_channel(content: str, version: str = VERSION_0_8) -> str:
    return get_protocol_spec(version).format_for_text_channel(content)


def format_content_for_channel(content: str, channel_id: str | None) -> str:
    if str(channel_id or "").lower() == "web":
        return content
    spec = get_protocol_spec()
    if not spec.may_contain_a2ui_content(content):
        return content
    formatted = format_a2ui_for_text_channel(content)
    return formatted or ""


def is_a2ui_client_event(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == A2UI_CLIENT_EVENT_TYPE


def _log_a2ui_client_event(event: dict[str, Any]) -> None:
    payload = event.get("event")
    user_action = payload.get("userAction") if isinstance(payload, dict) else None
    if not isinstance(user_action, dict):
        logger.info("A2UI client event received without userAction")
        return

    context = user_action.get("context")
    context_keys = sorted(context.keys()) if isinstance(context, dict) else []
    logger.info(
        "A2UI client event received: action=%s surfaceId=%s "
        "sourceComponentId=%s context_keys=%s",
        user_action.get("name"),
        user_action.get("surfaceId"),
        user_action.get("sourceComponentId"),
        context_keys,
    )


def _get_a2ui_user_action(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("event")
    user_action = payload.get("userAction") if isinstance(payload, dict) else None
    return user_action if isinstance(user_action, dict) else {}


def _get_a2ui_action_context(event: dict[str, Any]) -> dict[str, Any]:
    user_action = _get_a2ui_user_action(event)
    context = user_action.get("context")
    return context if isinstance(context, dict) else {}


def _get_a2ui_action_name(event: dict[str, Any]) -> str:
    user_action = _get_a2ui_user_action(event)
    return str(user_action.get("name") or "").strip()


def _get_a2ui_next_action(event: dict[str, Any]) -> str:
    context = _get_a2ui_action_context(event)
    return str(context.get("next_action") or "").strip()


def _is_browser_preflight_submit(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "browser_preflight_submit" or next_action == "run_browser_agent"


def _is_hotel_option_select(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name in {"hotel_option_select", "hotel_candidate_select"} or next_action in {
        "continue_hotel_booking",
        "select_hotel_candidate",
    }


def _is_hotel_payment_confirm(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "hotel_payment_confirm" or next_action == "confirm_hotel_payment"


def _is_hotel_payment_cancel(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "hotel_payment_cancel" or next_action == "cancel_hotel_payment"


def _is_gmail_email_select(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name in {"gmail_email_select", "gmail_thread_select"} or next_action in {
        "continue_gmail_email_review",
        "open_gmail_email",
    }


def _is_gmail_reply_draft_select(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name in {"gmail_reply_draft_select", "gmail_draft_reply"} or next_action in {
        "continue_gmail_reply_draft",
        "fill_gmail_reply_draft",
    }


def _is_gmail_send_confirm(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "gmail_send_confirm" or next_action == "confirm_gmail_send"


def _is_gmail_send_cancel(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "gmail_send_cancel" or next_action == "cancel_gmail_send"


def _is_gmail_cleanup_select(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "gmail_cleanup_select" or next_action == "review_gmail_cleanup"


def _is_gmail_cleanup_confirm(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "gmail_cleanup_confirm" or next_action == "confirm_gmail_cleanup"


def _is_gmail_cleanup_cancel(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "gmail_cleanup_cancel" or next_action == "cancel_gmail_cleanup"


def _is_social_post_draft_select(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name in {"social_post_draft_select", "social_draft_select"} or next_action in {
        "continue_social_post_draft",
        "fill_social_post_draft",
    }


def _is_social_post_confirm(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "social_post_confirm" or next_action == "confirm_social_post"


def _is_social_post_cancel(event: dict[str, Any]) -> bool:
    action_name = _get_a2ui_action_name(event)
    next_action = _get_a2ui_next_action(event)
    return action_name == "social_post_cancel" or next_action == "cancel_social_post"


def _build_a2ui_event_payload(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> dict[str, Any]:
    return {
        "source": channel,
        "preferred_response_language": language,
        "type": A2UI_CLIENT_EVENT_TYPE,
        "protocolVersion": event.get("protocolVersion", VERSION_0_8),
        "event": event.get("event", {}),
    }


def _build_browser_preflight_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI browser task preflight submission. The user has "
        "confirmed the values in event.userAction.context. Combine those values "
        "with original_query and start the browser task now by calling "
        "spawn_sub_agent with subagent_type='browser_agent' and a complete "
        "task_description. Do not ask again for values already present in the "
        "context. If any required browser-task detail is still missing, render "
        "one more A2UI form instead of starting the browser. Never buy, book, "
        "pay, submit an order, or perform an irreversible action without a final "
        "explicit user confirmation after the browser has shown the exact option "
        "or order summary.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_hotel_option_select_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI hotel candidate selection. The user selected one "
        "candidate from a hotel list previously returned by browser automation. "
        "Treat event.userAction.context as the authoritative selected candidate "
        "and booking context. Continue the existing hotel booking flow by calling "
        "spawn_sub_agent with subagent_type='browser_agent'. The task_description "
        "must instruct the browser_agent to continue from the current browser "
        "state/session and select or open the already chosen hotel candidate. Do "
        "not repeat the broad hotel search, do not change city/date/guest filters, "
        "and do not search again by city/date/hotel name unless recovery is needed "
        "because the current browser state no longer contains the selected "
        "candidate. If recovery is needed, prefer opening a candidate/detail URL "
        "from context; otherwise search narrowly for the selected hotel name using "
        "the already confirmed city, dates, guests, and room criteria. Continue "
        "only until the exact order summary or payment page is visible. Then stop "
        "and render a final A2UI confirmation with action name "
        "'hotel_payment_confirm' and a cancel action named 'hotel_payment_cancel'. "
        "Never buy, book, pay, submit an order, or perform an irreversible action "
        "until the user clicks that final confirmation.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_hotel_payment_confirm_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI final hotel payment confirmation. The user is "
        "confirming the exact order summary shown in event.userAction.context. "
        "Before taking any irreversible action, verify that the selected hotel, "
        "dates, guest/room details, total price, cancellation policy, and payment "
        "step in the current browser state match the context. If they do not "
        "match, stop and render a corrected A2UI confirmation instead of paying. "
        "If they match, continue with browser_agent only as far as allowed by the "
        "current product safety policy and available credentials; otherwise tell "
        "the user what must be completed manually.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_hotel_payment_cancel_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI hotel payment cancellation. Do not continue browser "
        "automation and do not submit payment or booking. Acknowledge the "
        "cancellation briefly in the preferred response language.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_gmail_email_select_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI Gmail email/thread selection. The user selected "
        "one item from Gmail search results previously returned by browser "
        "automation. Treat event.userAction.context as the authoritative "
        "selected email/thread and search context. Continue by calling "
        "spawn_sub_agent with subagent_type='browser_agent'. The task_description "
        "must instruct browser_agent to continue from the current Gmail browser "
        "state/session and open the selected email/thread. Do not repeat the "
        "broad Gmail search or change the query/date/sender filters unless "
        "browser-state recovery is needed. If recovery is needed, prefer a Gmail "
        "thread URL or stable message identifier from context; otherwise search "
        "narrowly for the selected subject/sender using the already confirmed "
        "filters. Read only the selected email/thread, summarize it, extract "
        "action items, and render an A2UI summary with reply-draft options. If "
        "the user asked to draft a reply, include action 'gmail_reply_draft_select'. "
        "Never send email without a final 'gmail_send_confirm' action.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_gmail_reply_draft_select_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI Gmail reply draft selection. The user selected a "
        "reply draft or drafting style from an email summary. Treat "
        "event.userAction.context as the authoritative selected email/thread, "
        "recipient, subject, and draft body. Continue by calling spawn_sub_agent "
        "with subagent_type='browser_agent' to open the existing selected Gmail "
        "thread and fill the reply compose box. Do not send the email. Do not "
        "repeat the broad Gmail search unless state recovery is needed; prefer "
        "thread URL or message identifiers from context for recovery. After the "
        "draft is filled or ready, render a final A2UI confirmation with action "
        "'gmail_send_confirm' and cancel action 'gmail_send_cancel'.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_gmail_send_confirm_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI final Gmail send confirmation. Before sending, "
        "verify that the visible Gmail compose/reply state matches "
        "event.userAction.context for recipient, subject, body, attachment "
        "state, and selected thread. If anything differs, stop and render a "
        "corrected A2UI confirmation. If it matches, call browser_agent to send "
        "the email now using the visible Gmail send action. Never send to a "
        "different recipient or with different content. If Gmail blocks sending "
        "or requires account/security steps outside the current browser session, "
        "stop and report the required manual step.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_gmail_send_cancel_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI Gmail send cancellation. Do not send email and do "
        "not continue browser automation. Acknowledge the cancellation briefly "
        "in the preferred response language.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_gmail_cleanup_select_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI Gmail cleanup selection. The user selected messages, "
        "categories, or cleanup rules from a Gmail cleanup candidate list. Treat "
        "event.userAction.context as the authoritative cleanup selection. Do not "
        "modify Gmail yet. Render a final A2UI cleanup confirmation that lists "
        "the exact operation, selected message count, representative subjects or "
        "senders, excluded messages, and risk level. The confirm action must be "
        "'gmail_cleanup_confirm' and the cancel action must be "
        "'gmail_cleanup_cancel'.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_gmail_cleanup_confirm_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI final Gmail cleanup confirmation. Continue by "
        "calling spawn_sub_agent with subagent_type='browser_agent'. The "
        "task_description must instruct browser_agent to continue from the "
        "current Gmail browser state/session and apply only the confirmed cleanup "
        "operation to the selected messages/categories in event.userAction.context. "
        "Do not repeat or broaden the Gmail search unless state recovery is "
        "needed. Before modifying Gmail, verify the selected messages/count and "
        "operation match the context. If they do not match, stop and render a "
        "corrected A2UI confirmation. Never delete, archive, unsubscribe, mark "
        "read, or label any unconfirmed message.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_gmail_cleanup_cancel_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI Gmail cleanup cancellation. Do not modify Gmail and "
        "do not continue browser automation. Acknowledge the cancellation briefly "
        "in the preferred response language.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_social_post_draft_select_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI social media post draft selection. The user selected "
        "one draft variant for a public social-media post. Treat "
        "event.userAction.context as the authoritative platform, account, draft "
        "body, media/link state, visibility, and posting context. Continue by "
        "calling spawn_sub_agent with subagent_type='browser_agent' to continue "
        "from the current browser state/session, open or use the selected social "
        "platform compose surface, and fill the selected draft. Do not publish, "
        "post, comment, like, follow, delete, or perform any externally visible "
        "action. After the draft is filled or ready, render a final A2UI "
        "confirmation with action 'social_post_confirm' and cancel action "
        "'social_post_cancel'.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_social_post_confirm_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI final social media post confirmation. Before "
        "publishing, verify that the visible compose state matches "
        "event.userAction.context for platform, account, body, media/link state, "
        "visibility, and target audience. If anything differs, stop and render a "
        "corrected A2UI confirmation. If it matches, call browser_agent to "
        "publish the post now using the visible platform publish/post action. "
        "Never publish different content or to a different account. If the "
        "platform blocks publishing or requires account/security steps outside "
        "the current browser session, stop and report the required manual step.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def _build_social_post_cancel_client_event_prompt(
    event: dict[str, Any],
    channel: str,
    language: str,
) -> str:
    payload = _build_a2ui_event_payload(event, channel, language)
    prefix = (
        "You receive an A2UI social media post cancellation. Do not publish the "
        "post and do not continue browser automation. Acknowledge the cancellation "
        "briefly in the preferred response language.\n"
    )
    return prefix + json.dumps(payload, ensure_ascii=False)


def build_a2ui_client_event_prompt(event: dict[str, Any], channel: str, language: str) -> str:
    _log_a2ui_client_event(event)
    if _is_hotel_option_select(event):
        return _build_hotel_option_select_client_event_prompt(event, channel, language)
    if _is_hotel_payment_confirm(event):
        return _build_hotel_payment_confirm_client_event_prompt(event, channel, language)
    if _is_hotel_payment_cancel(event):
        return _build_hotel_payment_cancel_client_event_prompt(event, channel, language)
    if _is_gmail_email_select(event):
        return _build_gmail_email_select_client_event_prompt(event, channel, language)
    if _is_gmail_reply_draft_select(event):
        return _build_gmail_reply_draft_select_client_event_prompt(event, channel, language)
    if _is_gmail_send_confirm(event):
        return _build_gmail_send_confirm_client_event_prompt(event, channel, language)
    if _is_gmail_send_cancel(event):
        return _build_gmail_send_cancel_client_event_prompt(event, channel, language)
    if _is_gmail_cleanup_select(event):
        return _build_gmail_cleanup_select_client_event_prompt(event, channel, language)
    if _is_gmail_cleanup_confirm(event):
        return _build_gmail_cleanup_confirm_client_event_prompt(event, channel, language)
    if _is_gmail_cleanup_cancel(event):
        return _build_gmail_cleanup_cancel_client_event_prompt(event, channel, language)
    if _is_social_post_draft_select(event):
        return _build_social_post_draft_select_client_event_prompt(event, channel, language)
    if _is_social_post_confirm(event):
        return _build_social_post_confirm_client_event_prompt(event, channel, language)
    if _is_social_post_cancel(event):
        return _build_social_post_cancel_client_event_prompt(event, channel, language)
    if _is_browser_preflight_submit(event):
        return _build_browser_preflight_client_event_prompt(event, channel, language)

    prefix = (
        "你收到了一次 A2UI 组件交互。请把 event.userAction.context "
        "视为用户提交的值。对于普通表单或按钮提交，请直接、简洁地使用首选回复语言回答。"
        "不要调用工具、读写记忆或创建文件，除非该 action 明确要求外部工作。\n"
        if language in {"zh", "cn"}
        else (
            "You receive an A2UI component interaction. Treat event.userAction.context "
            "as values submitted by the user. For normal form/button submissions, "
            "answer directly and concisely in the preferred response language. Do not "
            "call tools, read/write memory, or create files unless the action explicitly "
            "requests external work.\n"
        )
    )
    payload = _build_a2ui_event_payload(event, channel, language)
    return prefix + json.dumps(payload, ensure_ascii=False)


__all__ = [
    "A2UI_ACTIVE_PROTOCOL_VERSION",
    "A2UI_CLIENT_EVENT_TYPE",
    "A2UI_CLOSE_TAG",
    "A2UI_OPEN_TAG",
    "A2UIProtocolSpec",
    "A2UIStreamGuard",
    "build_a2ui_client_event_prompt",
    "build_a2ui_prompt_section",
    "format_a2ui_for_text_channel",
    "format_content_for_channel",
    "get_protocol_spec",
    "is_a2ui_client_event",
]
