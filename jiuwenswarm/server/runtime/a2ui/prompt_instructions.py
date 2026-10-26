# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped A2UI prompt rail instructions."""

from __future__ import annotations

import json
import re
from typing import Any


_BROWSER_WORKFLOW_ACTION_MARKERS = (
    "browser_preflight_submit",
    "run_browser_agent",
    "hotel_option_select",
    "continue_hotel_booking",
    "hotel_payment_confirm",
    "hotel_payment_cancel",
    "gmail_email_select",
    "continue_gmail_email_review",
    "gmail_reply_draft_select",
    "continue_gmail_reply_draft",
    "gmail_send_confirm",
    "gmail_send_cancel",
    "gmail_cleanup_select",
    "review_gmail_cleanup",
    "gmail_cleanup_confirm",
    "gmail_cleanup_cancel",
    "social_post_draft_select",
    "continue_social_post_draft",
    "social_post_confirm",
    "social_post_cancel",
)
_BROWSER_WORKFLOW_INTENT_RE = re.compile(
    r"(?:"
    r"(?:book|reserve|buy|purchase|checkout|fill|submit|search|summari[sz]e|"
    r"reply|clean|archive|delete|post|publish|comment|like|follow|open|click)"
    r".{0,80}"
    r"(?:browser|web(?:site)?|gmail|e-?mail|mailbox|hotel|ticket|form|"
    r"social(?:\s+media)?|facebook|instagram|linkedin|x\.com|twitter)"
    r"|"
    r"(?:browser|web(?:site)?|gmail|e-?mail|mailbox|hotel|ticket|form|"
    r"social(?:\s+media)?|facebook|instagram|linkedin|x\.com|twitter)"
    r".{0,80}"
    r"(?:book|reserve|buy|purchase|checkout|fill|submit|search|summari[sz]e|"
    r"reply|clean|archive|delete|post|publish|comment|like|follow|open|click)"
    r"|"
    r"(?:预订|预约|购买|下单|填写|提交|搜索|查找|总结|回复|清理|归档|删除|"
    r"发布|发帖|评论|点赞|关注|打开|点击)"
    r".{0,40}"
    r"(?:浏览器|网页|网站|邮箱|邮件|收件箱|酒店|宾馆|机票|车票|表单|"
    r"社交媒体|微博|小红书)"
    r"|"
    r"(?:浏览器|网页|网站|邮箱|邮件|收件箱|酒店|宾馆|机票|车票|表单|"
    r"社交媒体|微博|小红书)"
    r".{0,40}"
    r"(?:预订|预约|购买|下单|填写|提交|搜索|查找|总结|回复|清理|归档|删除|"
    r"发布|发帖|评论|点赞|关注|打开|点击)"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def is_a2ui_browser_workflow_request(value: Any) -> bool:
    """Return whether the current request needs browser-specific A2UI rules."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value or "")

    normalized = text.lower()
    if any(marker in normalized for marker in _BROWSER_WORKFLOW_ACTION_MARKERS):
        return True
    return bool(_BROWSER_WORKFLOW_INTENT_RE.search(text))


def build_a2ui_browser_workflow_instruction() -> str:
    """Return browser-workflow rules for requests that actually need them."""
    browser_preflight_rule_en = (
        " Browser task preflight: when the user asks you to perform a browser "
        "automation task such as booking tickets, reserving hotels, buying "
        "products, filling forms, handling webmail, posting to social media, or "
        "comparing purchasable options, first check "
        "whether the task has enough user-confirmed details before calling any "
        "browser tool or browser subagent. If required details are missing and "
        "the Web A2UI channel is available, do not start browser automation yet. "
        "Instead, render an A2UI information-collection or confirmation form. "
        "Do not ask for those missing browser-task details through plain natural "
        "language, Markdown, or the ask_user tool when A2UI is available. "
        "The submit Button action name MUST be 'browser_preflight_submit'. Its "
        "action.context MUST include original_query, task_type, next_action with "
        "the value 'run_browser_agent', must_confirm_before_payment with true, "
        "and all form values using path references. After this action is "
        "submitted, combine the original request and submitted values, then use "
        "spawn_sub_agent with subagent_type 'browser_agent'. Never buy, book, "
        "pay, or place an order without a final explicit user confirmation."
    )
    mandatory_account_action_rule_en = (
        " Mandatory A2UI account-action gate: on the Web channel, Gmail, email, "
        "mailbox cleanup, social-media posting, comments, or any externally "
        "visible account action MUST use A2UI when A2UI is available. This is "
        "not optional. Do not use plain text, Markdown, ask_user, todo tools, "
        "memory tools, task planning, or task_tool as a substitute for the A2UI "
        "preflight, candidate selection, draft review, or final confirmation. "
        "For a request like finding emails and replying to those that need a "
        "reply, first render an A2UI preflight if filters or reply preferences "
        "are incomplete; after Gmail search, render an A2UI email/thread "
        "candidate list before opening or replying to selected messages; after "
        "drafting, render a final A2UI send confirmation before sending. Never "
        "search multiple emails and send replies in the same uninterrupted run "
        "without an A2UI user selection and final send confirmation."
    )
    hotel_booking_flow_rule_en = (
        " Hotel booking A2UI flow: after browser_agent returns candidate hotels, "
        "present the candidates as A2UI cards or a comparable list. Each hotel's "
        "selection Button action name MUST be 'hotel_option_select'. Its "
        "action.context MUST include task_type='hotel', next_action with the "
        "value 'continue_hotel_booking', original_query, selected hotel name, "
        "candidate index or id, and the confirmed city/check-in/check-out/guest "
        "and room criteria. Include a candidate/detail URL, provider, room type, "
        "price, currency, and cancellation policy when available. After the user "
        "selects a hotel, continue the existing browser state for that candidate; "
        "do not restart the broad city/date search. At the payment or order "
        "summary page, render a final A2UI confirmation whose confirm Button "
        "action name is 'hotel_payment_confirm' and whose cancel Button action "
        "name is 'hotel_payment_cancel'."
    )
    gmail_flow_rule_en = (
        " Gmail A2UI flow: for Gmail search, summarization, reply drafting, or "
        "mailbox cleanup requests, collect missing filters with preflight A2UI "
        "before browser automation. If the user already provided enough filters, "
        "browser_agent may search Gmail, but the returned emails/threads MUST "
        "still be shown as A2UI candidates before summarizing multiple messages, "
        "drafting replies, modifying labels, archiving, deleting, unsubscribing, "
        "or sending anything. After browser_agent returns Gmail search "
        "results, render emails or threads as A2UI cards/lists. Each email "
        "selection Button action name MUST be 'gmail_email_select'. Its "
        "action.context MUST include task_type='gmail', next_action with the "
        "value 'continue_gmail_email_review', original_query, search query or "
        "filters, sender, subject, date/time, message/thread id or index, and "
        "thread URL when available. For reply drafts, render draft options whose "
        "selection Button action name is 'gmail_reply_draft_select' with "
        "next_action='continue_gmail_reply_draft', recipient, subject, selected "
        "draft body, tone, and selected thread context. At the final send step, "
        "render a confirmation with confirm action 'gmail_send_confirm' and "
        "cancel action 'gmail_send_cancel'; after the user clicks confirm, send "
        "the email only if the visible Gmail compose state still matches the "
        "A2UI confirmation context. For mailbox cleanup, candidate "
        "selection actions MUST use 'gmail_cleanup_select' with "
        "next_action='review_gmail_cleanup'. The final cleanup confirmation MUST "
        "use confirm action 'gmail_cleanup_confirm' and cancel action "
        "'gmail_cleanup_cancel'. Never send, delete, archive, unsubscribe, mark "
        "read, label, or otherwise modify Gmail without the relevant final "
        "confirmation action."
    )
    social_post_flow_rule_en = (
        " Social media A2UI flow: for posting to social media websites, first "
        "collect missing platform, account, audience, visibility, tone, media, "
        "link, and post intent details with A2UI. Render draft variants as A2UI "
        "cards. Each draft selection Button action name MUST be "
        "'social_post_draft_select'. Its action.context MUST include "
        "task_type='social_post', next_action with the value "
        "'continue_social_post_draft', original_query, platform, account or "
        "account_hint, selected draft body, visibility, media/link state, and "
        "target audience. After a draft is selected, fill the compose UI but do "
        "not publish. The final publish confirmation MUST use confirm action "
        "'social_post_confirm' and cancel action 'social_post_cancel'; after "
        "the user clicks confirm, publish the post only if the visible compose "
        "state still matches the A2UI confirmation context. Never publish, post, "
        "comment, like, follow, delete, or perform externally visible social "
        "actions without final explicit confirmation."
    )
    return (
        mandatory_account_action_rule_en
        + browser_preflight_rule_en
        + hotel_booking_flow_rule_en
        + gmail_flow_rule_en
        + social_post_flow_rule_en
    )


def build_a2ui_autonomy_instruction(language: str = "en") -> str:
    # --- en rules ---
    image_url_rule_en = (
        " Do not invent image URLs. Use user-provided HTTPS URLs or verified "
        "stable sources; do not use guessed upload.wikimedia.org thumbnail paths."
    )
    unsupported_component_rule_en = (
        " A2UI does not support modal, dialog, popup, alert, toast, "
        "or floating overlay. Do not simulate these with absolute-positioned "
        "cards or fake close buttons. Use inline status, inline card, or "
        "confirmation area inside the normal surface instead. If the request "
        "cannot be approximated faithfully, answer in plain text that the "
        "requested component is not currently supported."
    )
    autonomy_rule_en = (
        " When the user's request requires interactive information collection "
        "(e.g., selections, input, or confirmations), or requires displaying "
        "structured comparison of 2+ same-type items (e.g., multiple "
        "hotels/options/products/routes), prefer A2UI. "
        "Output format decision (execute before every reply) — judge in this "
        "order, first match wins, stop: "
        "1. Reply needs to collect user input (select/fill/confirm) → MUST A2UI; "
        "2. Reply contains 2+ same-type items in parallel comparison → MUST A2UI; "
        "3. Reply is a single narrative explanation, brief fact, or greeting → plain text. "
        "Fallback: when a reply matches multiple rules, prefer the more structured format "
        "(A2UI > plain text). "
        "A2UI typical scenarios: information collection forms, actionable "
        "confirmations, multi-result comparison, and tool-result presentations. "
        "For real-world recommendation, comparison, "
        "shopping, ranking, price, travel, restaurant, or product requests, "
        "use tools first when available, then decide whether A2UI is the best "
        "final presentation. If the user already provided complete structured "
        "data or asks for a demo, you may render directly without tools. "
        "Capability boundary FIRST: even when you cannot execute the final "
        "action (no browser agent, no Gmail/social/tax login, no payment), if "
        "the user needs you to collect structured intent (options/fields/"
        "preferences/quantities), you MUST render an A2UI form to "
        "gather it — never fall back to Markdown text questions or ask_user "
        "rounds just because you cannot execute. The collected data can "
        "produce a checklist/draft/plan for the user to act on externally. "
        "When collecting multiple structured fields, use one A2UI form "
        "(parallel collection) instead of ask_user rounds. "
        "CRITICAL — must emit a2ui-json, not markdown mock forms: "
        "when the above decision tree conditions are met, you MUST output a "
        "valid <a2ui-json> block. Do NOT simulate a form with markdown "
        "lists, plain-text option lists, or pseudo-button text. Markdown mock "
        "forms are NOT interactive and are FORBIDDEN when A2UI is available. "
        "If you catch yourself writing option lists or button text as plain "
        "text, STOP and emit a2ui-json instead. "
        "Never write tool_call, invoke, or function-call tags as plain text."
    )
    requested_component_rule_en = (
        " You must match the requested component type: generate a card list "
        "only when the user asks for cards or a card list; Card/list is not "
        "a universal fallback. For a single object detail request, build a "
        "single object detail layout, not a multi-card demo. Do not substitute "
        "a fixed demo for the requested component. For every user-editable "
        "interactive component — TextField, MultipleChoice, CheckBox, "
        "DateTimeInput, and Slider — bind its value/selections to a data "
        "model path, initialize that path with dataModelUpdate, and include "
        "the submitted value in Button.action.context using a path reference. "
        "Use DateTimeInput (not TextField) for any date or time selection "
        "field. Do not emit an empty Button.action.context for form submissions. "
        "Button MUST use \"child\" to reference a component id (e.g. a Text "
        "component) for its label content; do NOT use a \"label\" field. "
        "For MultipleChoice, selections MUST be an object with a "
        "\"path\" key (e.g. selections={\"path\":\"/form/cabinClass\"}) and "
        "the default selection MUST be provided via dataModelUpdate on that "
        "path. "
        "MultipleChoice option values MUST NOT share a name with ANY component id "
        "(e.g. if a component id is \"anonymous\", use option value \"anon\" not \"anonymous\" — "
        "matching a component id causes circular dependency). "
        "No Button.action.context \"key\" may equal ANY component id in the same surface. "
        "For repeated list/card data, encode arrays as one collection key "
        "with indexed valueMap entries (\"0\", \"1\"); each item contains "
        "nested valueMap fields. Use item-relative paths inside templates "
        "(name, price, or /item/name). Do not nest templates inside "
        "template-rendered components in A2UI; flatten repeated item "
        "details into fields on the outer item, or use explicit child "
        "components bound to those fields."
        + image_url_rule_en
        + unsupported_component_rule_en
    )
    # --- zh rules ---
    image_url_rule_zh = (
        " 不要编造图片 URL。使用用户提供的 HTTPS URL 或已验证的稳定来源；"
        "不要使用猜测的 upload.wikimedia.org thumbnail 路径。"
    )
    unsupported_component_rule_zh = (
        " A2UI 不支持弹窗、模态框、dialog、popup、alert、toast、浮层。"
        "不要用绝对定位卡片或假的关闭按钮模拟这些组件。"
        "请在普通 surface 内使用行内状态、行内卡片或确认区域替代。"
        "如果无法忠实近似，请用纯文本说明当前暂不支持该组件。"
    )
    autonomy_rule_zh = (
        " 当用户的请求需要交互式收集信息（如选择、输入、确认等），"
        "或需要展示 2 个及以上同类项的结构化对比（如多个酒店/方案/产品/路线），优先采用 A2UI。"
        "输出格式决策（每次回复前必须执行）——按以下顺序判断，命中即定格式，不再继续："
        "1. 回复需要收集用户输入（选择/填写/确认等交互操作）→ 必须 A2UI；"
        "2. 回复包含 2 个及以上同类项的并列对比 → 必须 A2UI；"
        "3. 回复仅为单条叙述性解释、简短事实、寒暄 → 纯文本。"
        "兜底：当某回复同时满足多条格式规则时，优先选择更结构化的格式（A2UI > 纯文本）。"
        "A2UI 典型场景：信息收集表单、可操作确认、多结果比较、"
        "以及工具结果的交互式展示。"
        "对于真实世界推荐、对比、选购、排行、价格、旅行、餐厅或产品请求，"
        "如果有可用工具，应先使用工具获取依据，再判断 A2UI 是否是最佳最终展示方式。"
        "如果用户已经提供完整结构化数据，或明确要求演示 UI，可以直接渲染而不调用工具。"
        "能力边界优先：即使你无法执行最终动作（无浏览器代理、无 Gmail/社媒/个税登录、"
        "无支付），只要用户需要你收集结构化意图（选项/字段/偏好/数量），"
        "你必须用 A2UI 表单收集——绝不能因做不了执行就退回 Markdown 文字问或 "
        "ask_user 逐个问。收集到的数据可生成对照清单/草稿/方案供用户在外部操作。"
        "当需收集多个结构化字段时，用一个 A2UI 表单并行收集，不要用 ask_user 逐个问。"
        "关键——必须输出 a2ui-json，禁止 markdown 模拟表单："
        "满足上述决策树条件时，你必须输出合法的 <a2ui-json> 块。"
        "不要用 markdown 列表、纯文字选项列表、"
        "或伪按钮文本模拟表单。markdown 模拟表单不可交互，a2ui 可用时禁止使用。"
        "如果你发现自己在用纯文字写选项或按钮，立即停止，改输出 a2ui-json。"
        "绝不能把 tool_call、invoke 或函数调用标签当作普通文本输出。"
    )
    requested_component_rule_zh = (
        " 必须匹配用户请求的组件类型：只有用户要求卡片或卡片列表时才生成 "
        "card list；Card/list 不是万能 fallback。单个对象详情请求应生成单对象"
        "详情布局，不要生成多卡片 demo。不要用固定 demo 替代用户请求的组件。"
        "对每个可交互组件——TextField、"
        "MultipleChoice、CheckBox、DateTimeInput、Slider——必须把其 value/selections "
        "绑定到 data model 路径，用 dataModelUpdate 初始化该路径，并在 "
        "Button.action.context 中用 path reference 包含提交值。"
        "日期或时间选择字段必须用 DateTimeInput（不用 TextField）。"
        "表单提交不能输出空的 Button.action.context。"
        "Button 必须用 \"child\" 引用一个组件 id（如 Text 组件）作为按钮文字，不要用 \"label\" 字段。"
        "MultipleChoice 尤其要注意：selections 必须是带 \"path\" 键的对象"
        "（如 selections={\"path\":\"/form/cabinClass\"}），默认选中值必须通过 "
        "dataModelUpdate 在该路径上设置。"
        "MultipleChoice 的 option value 不要跟任何组件 id 同名"
        "（如组件 id=\"anonymous\" 时 option value 用 \"anon\" 而非 \"anonymous\"，否则会致循环依赖）。"
        "Button.action.context 的每个 \"key\" 不得与同一 surface 内任何组件 id 同名。"
        "List 或卡片列表展示重复数据时，dataModelUpdate 的 key 不能重复，"
        "请把数组编码为集合 key 并用索引项（\"0\"、\"1\"），每个 item 含嵌套 "
        "valueMap 字段，模板内用 item-relative path。"
        "不要在模板内使用集合绝对路径（如 /phones/name）。"
        "不要在模板渲染出的组件内部嵌套 template；"
        "请把重复 item 的明细拍平成外层 item 字段，或使用显式子组件绑定这些字段。"
        + image_url_rule_zh
        + unsupported_component_rule_zh
    )

    if language in {"zh", "cn"}:
        return (
            "如果决策树判定使用 A2UI，输出一段很短的说明后跟合法的 a2ui-json block。"
            + autonomy_rule_zh
            + requested_component_rule_zh
        )
    return (
        "If the decision tree determines A2UI is appropriate, output a very "
        "short intro followed by a valid a2ui-json block. "
        + autonomy_rule_en
        + requested_component_rule_en
    )


__all__ = [
    "build_a2ui_autonomy_instruction",
    "build_a2ui_browser_workflow_instruction",
    "is_a2ui_browser_workflow_request",
]
