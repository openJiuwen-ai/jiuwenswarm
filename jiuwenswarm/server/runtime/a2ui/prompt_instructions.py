# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request-scoped A2UI prompt rail instructions."""

from __future__ import annotations


def build_a2ui_autonomy_instruction(language: str = "en") -> str:
    # --- en rules ---
    image_url_rule_en = (
        " Do not invent image URLs. Use user-provided HTTPS URLs or verified "
        "stable sources; do not use guessed upload.wikimedia.org thumbnail paths."
    )
    unsupported_component_rule_en = (
        " A2UI does not support modal, dialog, popup, alert, toast, "
        "or floating overlay. Use inline status, inline card, or confirmation "
        "area inside the normal surface instead."
    )
    autonomy_rule_en = (
        " When the user's request requires interactive operations to collect "
        "information (e.g., selections, input, or confirmations), prefer A2UI. "
        "Capability boundary FIRST: even when you cannot execute the final "
        "action (no browser agent, no Gmail/social/tax login, no payment), if "
        "the user needs you to collect structured intent (options/fields/"
        "preferences/quantities), you MUST render an A2UI form to "
        "gather it — never fall back to Markdown text questions or ask_user "
        "rounds just because you cannot execute. The collected data can "
        "produce a checklist/draft/plan for the user to act on externally. "
        "When collecting multiple structured fields, use one A2UI form "
        "(parallel collection) instead of ask_user rounds. "
        "CRITICAL — must emit a2ui-json, not markdown mock forms: when the user "
        "needs to select, input, or confirm operations, you MUST output a valid "
        "<a2ui-json> block. Do NOT simulate a form with markdown "
        "lists, plain-text option lists, or pseudo-button text. Markdown mock "
        "forms are NOT interactive and are FORBIDDEN when A2UI is available. "
        "If you catch yourself writing option lists or button text as plain "
        "text, STOP and emit a2ui-json instead. "
        "JSON self-check: before emitting an <a2ui-json> block, mentally "
        "verify it is valid JSON — every string value with double quotes "
        "inside must escape them (\\\"), every key needs a colon (:), no "
        "stray symbols like + between key and value. A single typo breaks "
        "the whole block and the UI will not render. "
        "Never write tool_call, invoke, or function-call tags as plain text."
    )
    requested_component_rule_en = (
        " You must match the requested component type. For every user-editable "
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
        "For repeated list/card data, encode arrays as one collection key "
        "with indexed valueMap entries (\"0\", \"1\"); each item contains "
        "nested valueMap fields. Use item-relative paths inside templates "
        "(name, price, or /item/name). Do not nest templates inside "
        "template-rendered components in A2UI."
        + image_url_rule_en
        + unsupported_component_rule_en
    )
    a2ui_json_example = (
        "\n"
        "Example a2ui-json block structure:\n"
        "<a2ui-json>\n"
        "[\n"
        '  {"beginRendering": {"surfaceId": "form", "root": "root"}},\n'
        '  {"surfaceUpdate": {"surfaceId": "form", "components": [\n'
        '    {"id": "root", "component": {"Column": {"children": {"explicitList": ["name", "submit_text", "submit"]}}}},\n'
        '    {"id": "name", "component": {"TextField": {"label": {"literalString": "Name"}, "text": {"path": "/form/name"}, "textFieldType": "shortText"}}},\n'
        '    {"id": "submit_text", "component": {"Text": {"text": {"literalString": "Submit"}, "usageHint": "body"}}},\n'
        '    {"id": "submit", "component": {"Button": {"child": "submit_text", "primary": true, "action": {"name": "submit_form", "context": {"name": {"path": "/form/name"}}}}}}\n'
        "  ]}},\n"
        '  {"dataModelUpdate": {"surfaceId": "form", "path": "/form", "contents": [{"key": "name", "valueString": ""}]}}\n'
        "]\n"
        "</a2ui-json>\n"
        "Follow this structure: beginRendering first, then surfaceUpdate with "
        "components, then dataModelUpdate to initialize default values."
    )

    # --- zh rules ---
    image_url_rule_zh = (
        " 不要编造图片 URL。使用用户提供的 HTTPS URL 或已验证的稳定来源；"
        "不要使用猜测的 upload.wikimedia.org thumbnail 路径。"
    )
    unsupported_component_rule_zh = (
        " A2UI 不支持弹窗、模态框、dialog、popup、alert、toast、浮层。"
        "请在普通 surface 内使用行内状态、行内卡片或确认区域替代。"
    )
    autonomy_rule_zh = (
        " 当用户的请求需要通过交互式操作收集信息（如选择、输入、确认等），优先采用 A2UI。"
        "能力边界优先：即使你无法执行最终动作（无浏览器代理、无 Gmail/社媒/个税登录、"
        "无支付），只要用户需要你收集结构化意图（选项/字段/偏好/数量），"
        "你必须用 A2UI 表单收集——绝不能因做不了执行就退回 Markdown 文字问或 "
        "ask_user 逐个问。收集到的数据可生成对照清单/草稿/方案供用户在外部操作。"
        "当需收集多个结构化字段时，用一个 A2UI 表单并行收集，不要用 ask_user 逐个问。"
        "关键——必须输出 a2ui-json，禁止 markdown 模拟表单："
        "当用户的请求需要交互式收集信息，比如选择、输入、确认等操作，而非只回复一句话时，"
        "你必须输出合法的 <a2ui-json> 块。不要用 markdown 列表、纯文字选项列表、"
        "或伪按钮文本模拟表单。markdown 模拟表单不可交互，a2ui 可用时禁止使用。"
        "如果你发现自己在用纯文字写选项或按钮，立即停止，改输出 a2ui-json。"
        "JSON 自检：输出 <a2ui-json> 块前，心算校验它是合法 JSON——"
        "字符串值内的双引号必须转义（\\\"），每个 key 要有冒号（:），"
        "key 与 value 之间不能有多余符号（如 +）。一个 typo 会让整块 JSON 失效，"
        "界面将无法渲染。"
        "绝不能把 tool_call、invoke 或函数调用标签当作普通文本输出。"
    )
    requested_component_rule_zh = (
        " 必须匹配用户请求的组件类型。对每个可交互组件——TextField、"
        "MultipleChoice、CheckBox、DateTimeInput、Slider——必须把其 value/selections "
        "绑定到 data model 路径，用 dataModelUpdate 初始化该路径，并在 "
        "Button.action.context 中用 path reference 包含提交值。"
        "Use DateTimeInput (not TextField) for any date or time selection "
        "field. 表单提交不能输出空的 Button.action.context。"
        "Button 必须用 \"child\" 引用一个组件 id（如 Text 组件）作为按钮文字，不要用 \"label\" 字段。"
        "MultipleChoice 尤其要注意：selections 必须是带 \"path\" 键的对象"
        "（如 selections={\"path\":\"/form/cabinClass\"}），默认选中值必须通过 "
        "dataModelUpdate 在该路径上设置。"
        "List 或卡片列表展示重复数据时，dataModelUpdate 的 key 不能重复，"
        "请把数组编码为集合 key 并用索引项（\"0\"、\"1\"），每个 item 含嵌套 "
        "valueMap 字段，模板内用 item-relative path。"
        + image_url_rule_zh
        + unsupported_component_rule_zh
    )

    if language in {"zh", "cn"}:
        return (
            "当用户的请求需要交互式收集信息，比如选择、输入、确认等操作，"
            "而非只回复一句话时，必须输出 "
            "<a2ui-json>...</a2ui-json> block。输出一段很短的说明，"
            "然后输出合法的 a2ui-json block。"
            "不要用 markdown 列表或纯文字模拟表单。"
            "寒暄、简单事实回答、只读信息展示可用纯文本，无需 a2ui。"
            "如果确实需要外部事实或图片，可以短暂使用可用工具，"
            "随后用 a2ui 呈现。"
            "block 内必须是 A2UI server-to-client message list，"
            "并且必须先 beginRendering，再 surfaceUpdate，再按需 dataModelUpdate。"
            + autonomy_rule_zh
            + requested_component_rule_zh
            + a2ui_json_example
        )
    return (
        "When the request requires interactive information collection (e.g., "
        "selecting, inputting, or confirming operations, rather than just "
        "replying with a single sentence), you MUST output an "
        "<a2ui-json>...</a2ui-json> block. Output a very "
        "short intro followed by a valid a2ui-json block. Do not simulate a "
        "form with markdown lists or plain-text options. Greetings, simple "
        "factual answers, and read-only information display can use plain "
        "text. If external facts or images are needed, use the available "
        "tools briefly, then present with a2ui. The block must contain an "
        "A2UI server-to-client message list, with beginRendering before "
        "surfaceUpdate and dataModelUpdate only when needed."
        + autonomy_rule_en
        + requested_component_rule_en
        + a2ui_json_example
    )


__all__ = [
    "build_a2ui_autonomy_instruction",
]
