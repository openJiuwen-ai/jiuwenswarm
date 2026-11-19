"""端侧 app_ver 版本判定（xiaoyi 手机端）。

AskUser 结构化选项卡（status-update input-required 出站 + ToolExecute/
InvokeRsp 应答回传 + ask_user_id 契约字段）仅对 app_ver 达到门限
ASK_USER_TABS_MIN_APP_VER（完整元组逐段比较 >=）的手机端开启。
app_ver 由手机端随消息 data part 的
variables.systemVariables 携带（与 push_id 同层，既有字段，四段如
"11.7.10.900"）；缺失/非法版本一律按不支持处理（fail-closed）——旧
端侧对契约外字段做严格 schema 校验，宁可不发卡也不能破坏旧版本。

gateway（xiaoyi_connect）与 server（agent_adapter）共用同一实现，
避免两份拷贝语义漂移。
"""

# 选项卡契约最低端侧版本门限（元组逐段比较，>=）。11.7.9 线第 301 内部
# 构建起端侧支持选项卡契约：11.7.9.301（含等号）与 11.7.10+ 全系开启；
# 11.7.9.300 及更早（含裸三段 "11.7.9"，元组前缀比较小于四段门限）与
# 未上报/非法版本 fail-closed 走文本回退。
ASK_USER_TABS_MIN_APP_VER = (11, 7, 9, 301)


def parse_version_tuple(app_ver: str) -> tuple[int, ...] | None:
    """点分版本号 → 整数元组（段数不定，端侧实为四段如 "11.7.10.200"）。

    每段取前导数字，遇到非数字段即截断（"11.7.10.200.beta1" →
    (11, 7, 10, 200)，预发版本按其数字前缀判定）。整串无数字返回 None。

    Args:
        app_ver: 端侧上报的版本字符串，如 "11.7.10.200"。

    Returns:
        版本各段整数元组；不可解析时为 None。
    """
    text = str(app_ver or "").strip()
    if not text:
        return None
    segments: list[int] = []
    for part in text.split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        segments.append(int(digits))
    if not segments:
        return None
    return tuple(segments)


def xiaoyi_ask_user_tabs_supported(app_ver: str) -> bool:
    """手机端 app_ver 是否支持 AskUser 结构化选项卡（达到门限）。

    完整元组逐段比较：>= ASK_USER_TABS_MIN_APP_VER（含等号）支持；
    低于门限（11.7.9.300 及更早，含裸三段 "11.7.9"）不支持；缺失/非法
    解析为 None，按不支持处理（fail-closed）。

    Args:
        app_ver: 端侧上报的版本字符串；缺失按不支持处理。

    Returns:
        版本达到门限 ASK_USER_TABS_MIN_APP_VER 时为 True，否则 False。
    """
    parsed = parse_version_tuple(app_ver)
    return parsed is not None and parsed >= ASK_USER_TABS_MIN_APP_VER
