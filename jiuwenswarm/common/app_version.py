"""端侧 app_ver 版本判定（xiaoyi 手机端）。

AskUser 结构化选项卡（status-update input-required 出站 + ToolExecute/
InvokeRsp 应答回传 + ask_user_id 契约字段）仅对支持该契约的手机端开启：
app_ver >= 11.7.10。app_ver 由手机端随消息 data part 的
variables.systemVariables 携带（与 push_id 同层，既有字段，四段如
"11.7.10.900"）；缺失/非法版本一律按不支持处理（fail-closed）——旧
端侧对契约外字段做严格 schema 校验，宁可不发卡也不能破坏旧版本。

gateway（xiaoyi_connect）与 server（agent_adapter）共用同一实现，
避免两份拷贝语义漂移。
"""

# 选项卡契约最低端侧版本：11.7.10 起支持（含等号，元组逐段比较）。
# 裸三段 "11.7.10" 即 (11,7,10) 弹框；带构建号的四段版本（
# "11.7.10.200"/"11.7.10.900"）与 11.7.11 起均弹框；11.7.9.x 及更低不弹。
ASK_USER_TABS_MIN_APP_VER = (11, 7, 10)


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
    """手机端 app_ver 是否支持 AskUser 结构化选项卡（>= 11.7.10）。

    完整元组逐段比较：等于 11.7.10（裸三段）支持；高于它的版本
    （"11.7.10.200"/"11.7.10.900" 等带构建号四段、11.7.11 起）支持；
    11.7.9.x 及更低不支持。

    Args:
        app_ver: 端侧上报的版本字符串；缺失按不支持处理。

    Returns:
        版本大于等于 11.7.10 时为 True，否则 False。
    """
    parsed = parse_version_tuple(app_ver)
    return parsed is not None and parsed >= ASK_USER_TABS_MIN_APP_VER
