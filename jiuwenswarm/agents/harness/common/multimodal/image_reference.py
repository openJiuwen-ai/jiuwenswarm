# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Image reference lines, upload-file normalization and history state lines.

引用行是模型可见的历史契约（``[image #n] path=... filename=... mime=...``），状态行是
「给过/没给」的事实来源（``[image #n attached|omitted]``）。本模块还负责上传清单
ContextVar 与路径归一。依赖 ``image_content`` / ``image_processing``；不 import ``history``。
"""

from __future__ import annotations

import mimetypes
import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.multimodal.image_content import (
    _content_to_query_text,
    _message_content,
    is_user_authored_message,
)
from jiuwenswarm.agents.harness.common.multimodal.image_processing import (
    _VISION_INLINE_IMAGE_MIME_TYPES,
)

_CURRENT_MULTIMODAL_IMAGE_FILES: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "jiuwenswarm_current_multimodal_image_files",
    default=(),
)


# 引用行是模型可见的历史契约。path / filename 用「到下一个 key 为止」的写法解析，
# 允许文件名含空格（Windows 截图名、含空格的下载目录）；旧格式（无空格）同样命中。
# 值本身含字面量 `` filename=`` / `` mime=`` 的极端路径无法区分，属可接受边界。
_IMAGE_REFERENCE_LINE_RE = re.compile(
    r"^\[image #(\d+)\]\s+"
    r"path=(.+?)\s+"
    r"filename=(.+?)\s+"
    r"mime=(\S+)"
    r"(?:\s+size=(\d+))?\s*$"
)


def build_image_reference_line(image_file: dict[str, Any], index: int) -> str:
    """Format an image-file record as a plain-text reference line."""

    segments = [
        "[image #{}] path={} filename={} mime={}".format(
            index,
            image_file.get("path", ""),
            image_file.get("filename", ""),
            image_file.get("mime_type", ""),
        )
    ]
    size_bytes = image_file.get("size_bytes")
    if size_bytes is not None:
        segments.append("size={}".format(size_bytes))
    return " ".join(segments)


def parse_image_references(text: str) -> list[dict[str, Any]]:
    """Parse image reference lines out of free-form text."""

    refs: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _IMAGE_REFERENCE_LINE_RE.match(line.strip())
        if not match:
            continue
        ref: dict[str, Any] = {
            "index": int(match.group(1)),
            "path": match.group(2),
            "filename": match.group(3),
            "mime_type": match.group(4),
        }
        size_bytes = match.group(5)
        if size_bytes is not None:
            ref["size_bytes"] = int(size_bytes)
        refs.append(ref)
    return refs


def extract_multimodal_image_files(params: Any) -> list[dict[str, Any]]:
    """Return normalized image-file records from a request payload."""

    if not isinstance(params, dict):
        return []

    candidates: list[Any] = []
    raw_media_items = params.get("media_items")
    if isinstance(raw_media_items, list):
        candidates.extend(raw_media_items)

    files = params.get("files")
    if isinstance(files, list):
        # relay 通道下发的 files 固定是 list（web 才走 dict 信封）；list 形态必须
        # 直接收进候选，否则原生图片块注入在 relay 主链路（单 agent 与 team）永不激活。
        candidates.extend(files)
    elif isinstance(files, dict):
        uploaded_images = files.get("uploaded_images")
        if isinstance(uploaded_images, list):
            candidates.extend(uploaded_images)

    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in candidates:
        image = _normalize_image_file(item)
        if not image:
            continue
        path = image["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        normalized.append(image)
    return normalized


def set_current_multimodal_image_files(image_files: list[dict[str, Any]]) -> Any:
    """Bind request image files to the current Core model-call task."""

    return _CURRENT_MULTIMODAL_IMAGE_FILES.set(tuple(_normalize_image_files(image_files)))


def reset_current_multimodal_image_files(token: Any) -> None:
    _CURRENT_MULTIMODAL_IMAGE_FILES.reset(token)


def current_multimodal_image_files() -> list[dict[str, Any]]:
    return list(_CURRENT_MULTIMODAL_IMAGE_FILES.get())


# 上传清单的 agent 实例镜像。ContextVar 不跨「请求协程 → agent 任务」边界：适配器在
# 请求入口 set，而 send_input 只是把消息入队，before_model_call 在 agent 自己的任务里
# 跑，读不到请求协程 set 的值（N12 安全门因此把合法上传判成 non-uploaded）。所以把
# 清单同时镜像到 DeepAgent 实例上——适配器（self._instance）与 rail（self._deep_agent）
# 拿到的是同一对象，注入时从实例读即可跨边界。同调用栈仍优先走 ContextVar，跨任务走实例。
_CURRENT_MULTIMODAL_IMAGE_FILES_AGENT_ATTR = "_jiuwenswarm_current_image_files"


def store_current_multimodal_image_files_on_agent(
    agent: Any, image_files: list[dict[str, Any]]
) -> None:
    """把本次请求上传清单镜像到 agent 实例（覆盖写，空列表即清除上一请求残留）。"""

    if agent is None:
        return
    try:
        setattr(
            agent,
            _CURRENT_MULTIMODAL_IMAGE_FILES_AGENT_ATTR,
            tuple(_normalize_image_files(image_files)),
        )
    except Exception:  # pragma: no cover - 冻结对象 / 属性拦截时降级为只靠 ContextVar
        pass


def current_multimodal_image_files_from_agent(agent: Any) -> list[dict[str, Any]] | None:
    """读 agent 实例上的上传清单镜像；未存储 → None（调用方回退 ContextVar）。"""

    value = getattr(agent, _CURRENT_MULTIMODAL_IMAGE_FILES_AGENT_ATTR, None)
    if value is None:
        return None
    return list(value)


# 「本轮上传清单」随 build context 传给 team 成员的键。team 成员 agent 不是 adapter
# 的 ``self._instance``（agent 属性镜像拿不到），ContextVar 也跨不到成员自己的任务，
# 唯一可达通道是 ``SwarmBuildContext.request_metadata``——与 ``enable_routing`` 同一做法。
# 写侧必须在 adapter/team_helpers 里用真实上传清单覆盖写（同一键由请求 metadata 透传时
# 可能是外部输入，不能被当成清单信任）。
_TEAM_REQUEST_IMAGE_FILES_KEY = "multimodal_image_files"


def store_team_request_image_files(
    request_metadata: Any, image_files: list[dict[str, Any]] | None
) -> None:
    """把本轮上传清单写进 ``request_metadata``（覆盖写，空列表即清除上一轮残留）。"""

    if not isinstance(request_metadata, dict):
        return
    request_metadata[_TEAM_REQUEST_IMAGE_FILES_KEY] = _normalize_image_files(
        image_files or []
    )


def team_request_image_files(request_metadata: Any) -> list[dict[str, Any]] | None:
    """读 ``request_metadata`` 里的上传清单；从未写入 → None（调用方自行回退）。"""

    if not isinstance(request_metadata, dict):
        return None
    raw = request_metadata.get(_TEAM_REQUEST_IMAGE_FILES_KEY)
    if not isinstance(raw, list):
        return None
    return [dict(item) for item in raw if isinstance(item, dict)]


def normalize_image_path(path: str) -> str:
    """把图片路径归一为可比较的 key（大小写 / 相对段 / ``~`` 展开）。"""

    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path or "").strip())))


# 图片提及的「文件名类字符」：字母 / 数字 / 下划线 / 连字符。命中片段前后若是这些
# 字符，说明它只是另一个更长文件名的一部分（``1.png`` vs ``11.png``、``a.png`` vs
# ``data.png``），不是同一张图。
_IMAGE_MENTION_BOUNDARY = r"[0-9A-Za-z_\-]"


def normalize_image_mention_text(text: str) -> str:
    """把任意文本归一成图片提及的比较形态：``\\`` → ``/`` 且小写。

    为什么不用 :func:`normalize_image_path`：这里比较的是**自由文本**（消息正文 /
    工具结果）里的片段，realpath 会把正文里的相对段 / ``~`` 一并改写；而 POSIX 上
    ``os.path.normcase`` 是恒等，无法把 Windows 反斜杠路径与正斜杠文本对齐。这里
    只做「分隔符 + 大小写」两项归一，保证两种形态互认。
    """

    return str(text or "").replace("\\", "/").lower()


def _image_mention_pattern(needle: str) -> re.Pattern[str] | None:
    """把路径 / 文件名编译成带边界的提及正则；空串返回 None。"""

    normalized = normalize_image_mention_text(needle).strip()
    if not normalized:
        return None
    return re.compile(
        rf"(?<!{_IMAGE_MENTION_BOUNDARY}){re.escape(normalized)}(?!{_IMAGE_MENTION_BOUNDARY})"
    )


def normalized_text_mentions_image(image: dict[str, Any], normalized_text: str) -> bool:
    """在**已归一**的文本里判断是否提到该图（完整路径或 basename 文件名）。

    归一（``\\`` → ``/`` + 小写）由调用方用 :func:`normalize_image_mention_text` 做一次。
    为什么单独暴露：``ensure_manifest_images_referenced`` 要在每次模型调用、最多 N 张图
    × 全量消息（含 60k 级 tool result）上循环，逐 (image, text) 组合归一等于每张图都
    重扫一遍大文本；先对每条消息归一一次再对多张图复用，才能保住 Nice-2 的
    「不做全量重复小写扫描」。
    """

    path = str(image.get("path") or "").strip()
    path_pattern = _image_mention_pattern(path)
    if path_pattern is not None and path_pattern.search(normalized_text):
        return True
    # filename 按 basename 形态比较：上传方可能把带目录的名字原样透传，也可能给图改名
    # （filename 与路径 basename 不同），两种形态都只按末段文件名判定。
    filename = str(image.get("filename") or "").strip()
    if not filename:
        filename = path
    filename = os.path.basename(normalize_image_mention_text(filename))
    filename_pattern = _image_mention_pattern(filename)
    return bool(filename_pattern is not None and filename_pattern.search(normalized_text))


def text_mentions_image(image: dict[str, Any], text: str) -> bool:
    """判断文本是否提到清单里的这张图（完整路径或文件名，边界对齐）。

    为什么不用裸 ``in``：清单 ``1.png`` 会在 ``11.png`` / ``data.png`` 里命中（子串），
    清单 ``C:/uploads/3.jpg`` 在 Windows 反斜杠形态的正文里又命中不了（只能靠文件名
    兜底，路径命中整体失效）。这里先统一分隔符 / 大小写，再要求命中片段前后不是
    文件名类字符：``请识别3.jpg`` 仍命中，``1.png`` 不再误命中 ``11.png``。
    单条文本的便捷封装；批量场景（同一文本要对多张图判定）请先归一一次，再走
    :func:`normalized_text_mentions_image`，避免每张图重复归一同一段大文本。
    """

    normalized_text = normalize_image_mention_text(text)
    if not normalized_text:
        return False
    return normalized_text_mentions_image(image, normalized_text)


_DISPATCH_REFERENCE_RE = re.compile(r"\[image #\d+\]\s+path=(.+?)\s+filename=")


def augment_message_with_reference_lines(
    content: str,
    image_files: list[dict[str, Any]] | None,
    *,
    max_images: int = 4,
) -> str:
    """把成员消息里"只提了文件名 / 路径"的上传图补成完整引用行（leader 分发兜底）。

    为什么需要：team leader 常把图片路径写进任务看板（tool result，不物化）或只写
    "识别 3.jpg"，成员侧的注入只认 user 消息里的 ``[image #n] path=...`` 引用行。
    这里按**文件名 / 完整路径**把本轮上传清单中对应的图补成引用行，追加到消息正文；
    只有真正被提到的图才补，不会把整批上传清单塞给任意成员。提及判定走
    :func:`text_mentions_image`（分隔符 / 大小写归一 + 边界对齐），避免 ``1.png``
    被 ``11.png`` 之类的子串误命中。

    幂等：消息里已有同一路径的引用行时不重复追加；``max_images`` 限单条消息补几张。
    """

    if not isinstance(content, str) or not content.strip():
        return content
    images = image_files or []
    if not images:
        return content
    already: set[str] = set()
    for match in _DISPATCH_REFERENCE_RE.finditer(content):
        already.add(normalize_image_path(match.group(1)))
    next_index = 1
    used = [int(i) for i in re.findall(r"\[image #(\d+)\]", content)]
    if used:
        next_index = max(used) + 1
    appended: list[str] = []
    for image in _normalize_image_files(images):
        if len(appended) >= max(0, int(max_images or 0)):
            break
        path = str(image.get("path") or "").strip()
        if not path:
            continue
        key = normalize_image_path(path)
        if key in already:
            continue
        if not text_mentions_image(image, content):
            continue
        appended.append(build_image_reference_line(image, next_index))
        already.add(key)
        next_index += 1
    if not appended:
        return content
    return content.rstrip() + "\n" + "\n".join(appended)



def is_provided_image_path(path: str, provided_paths: Any) -> bool:
    """判断 path 是否落在「本次请求已随消息提供像素」的路径集合内。

    ``provided_paths`` 是路径字符串列表。调用方（rail）从 ctx.extra 传入，
    不依赖 ContextVar——工具执行可能跨任务 / 线程，请求级 ContextVar 会丢。
    """

    candidate = str(path or "").strip()
    if not candidate or not provided_paths:
        return False
    target = normalize_image_path(candidate)
    return any(
        normalize_image_path(str(item)) == target
        for item in provided_paths
        if str(item or "").strip()
    )


def collect_image_references_from_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Collect image reference lines from user-authored message text.

    只扫 user 角色：引用行是上传入口写入用户消息的历史契约，工具输出 / 抓取正文
    里的同名文本不得被当成本次请求的图片引用。
    """

    refs: list[dict[str, Any]] = []
    for message in messages:
        if not is_user_authored_message(message):
            continue
        text = _content_to_query_text(_message_content(message))
        if text:
            refs.extend(parse_image_references(text))
    return refs


def _normalize_image_files(image_files: list[dict[str, Any]] | tuple[Any, ...]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in image_files:
        image = _normalize_image_file(item)
        if not image:
            continue
        path = image["path"]
        if path in seen_paths:
            continue
        seen_paths.add(path)
        normalized.append(image)
    return normalized


def _normalize_image_file(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    item_type = item.get("type")
    if item_type is not None and item_type != "image":
        return None

    path = str(item.get("path") or "").strip()
    if not path:
        return None

    mime_type = str(item.get("mime_type") or item.get("mimeType") or "").strip().lower()
    if not mime_type:
        mime_type = mimetypes.guess_type(path)[0] or ""
    if mime_type not in _VISION_INLINE_IMAGE_MIME_TYPES:
        return None

    filename = str(item.get("filename") or Path(path).name).strip() or Path(path).name
    image: dict[str, Any] = {
        "type": "image",
        "filename": filename,
        "path": path,
        "mime_type": mime_type,
    }
    index = item.get("index")
    if isinstance(index, int):
        # 引用序号：注入/预算卸载按 [image #index] 写状态行，必须透传。
        image["index"] = index
    size_bytes = item.get("size_bytes")
    if isinstance(size_bytes, int):
        image["size_bytes"] = size_bytes
    return image


# 状态行：``[image #n attached]`` / ``[image #n omitted]`` / ``[image #n omitted: reason]``。
# 捕获 reason 供 read_file 重出图护栏区分「预算卸载 / 不可读」与「重复引用」：
# 后者该 path 已在别的消息随像素提供，不能再放行一次重读。
_IMAGE_STATE_LINE_RE = re.compile(
    r"^\[image #(\d+) (attached|omitted)(?:: ([^\]]*))?\]$"
)
_IMAGE_ATTACHED_LINE_RE = re.compile(r"^\[image #(\d+) attached\]$")
# read_file 注入的像素块前面有一行路径说明（agent-core 生成），预算卸载时保留它，
# 模型据此知道"这张图可以用 read_file 重新出图"。
_READ_FILE_IMAGE_TEXT_RE = re.compile(r"^Image loaded from read_file:\s*(.+)$")


def _build_image_state_line(index: int, state: str, reason: str = "") -> str:
    if state == "attached":
        return f"[image #{index} attached]"
    if reason:
        return f"[image #{index} omitted: {reason}]"
    return f"[image #{index} omitted]"


def _iter_text_values(content: Any):
    if isinstance(content, str):
        yield content
        return
    if not isinstance(content, list):
        return
    for part in content:
        if isinstance(part, str):
            yield part
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            yield part["text"]


def _message_image_states(message: Any) -> dict[int, str]:
    """单条消息内的图片状态行。

    引用行与它的状态行写在同一批内容里（注入时就地追加到引用行所在消息），
    因此状态判定按消息作用域：压缩把老消息搬进摘要后，新消息里的同号引用
    不会被老状态行误判成"已处理"，也不会把老图重新加回。
    """

    states: dict[int, str] = {}
    for text in _iter_text_values(_message_content(message)):
        for line in text.splitlines():
            match = _IMAGE_STATE_LINE_RE.match(line.strip())
            if match:
                states[int(match.group(1))] = match.group(2)
    return states


def _message_omitted_image_reasons(message: Any) -> dict[int, str]:
    """单条消息内 ``omitted`` 状态行的序号 → 原因（归一小写，无原因为空串）。"""

    reasons: dict[int, str] = {}
    for text in _iter_text_values(_message_content(message)):
        for line in text.splitlines():
            match = _IMAGE_STATE_LINE_RE.match(line.strip())
            if not match or match.group(2) != "omitted":
                continue
            reasons[int(match.group(1))] = str(match.group(3) or "").strip().lower()
    return reasons


def collect_omitted_image_paths(messages: list[Any]) -> list[str]:
    """收集历史里被卸载（``omitted``）的图片路径。

    被预算卸载 / 读取失败的图片像素已经不在上下文里，允许模型按原文件重新出图
    （``read_file`` 重读原图 → 重新缩小编码 → 新的像素块），而不是靠放大已压糊的
    像素。同一路径只返回一次。

    ``omitted: duplicate`` 例外：它只是「同一请求里同 path 已在另一条消息随像素
    提供过」的去重标记，像素并没有缺失；若也算进来，read_file 会对同一路径再放行
    一次重出图，白占预算。
    """

    paths: list[str] = []
    for message in messages:
        if not is_user_authored_message(message):
            continue
        text = _content_to_query_text(_message_content(message))
        if not text:
            continue
        refs = {
            int(ref["index"]): str(ref.get("path") or "")
            for ref in parse_image_references(text)
        }
        if not refs:
            continue
        for index, reason in _message_omitted_image_reasons(message).items():
            if reason == "duplicate":
                continue
            path = refs.get(index)
            if path and path not in paths:
                paths.append(path)
    return paths


def collect_provided_image_paths(messages: list[Any]) -> list[str]:
    """收集历史里**像素已经随消息提供**的图片路径（``attached`` 状态）。

    与 :func:`collect_omitted_image_paths` 相反：只有注入成功、像素块确实写进
    历史的图片才算「已提供」。read_file 护栏必须按这个集合拒绝重复读——用上传
    清单会把"本轮上传了但注入失败 / 预算全卸载"的图片也算成已提供，模型既看不到
    像素又被禁止 read_file，图片彻底不可得。
    """

    paths: list[str] = []
    for message in messages:
        if not is_user_authored_message(message):
            continue
        text = _content_to_query_text(_message_content(message))
        if not text:
            continue
        refs = {
            int(ref["index"]): str(ref.get("path") or "")
            for ref in parse_image_references(text)
        }
        if not refs:
            continue
        for index, state in _message_image_states(message).items():
            if state != "attached":
                continue
            path = refs.get(index)
            if path and path not in paths:
                paths.append(path)
    return paths
