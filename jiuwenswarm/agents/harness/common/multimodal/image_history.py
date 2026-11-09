# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Injection, budgeting and eviction of image content in conversation history.

发送侧写入历史的最高层：把引用行物化成 ``image_url`` 块、按三预算「最老优先」降级、
维护 ``attached`` / ``omitted`` 状态行，以及非视觉模型分支的视图过滤 mutator。依赖
``image_content`` / ``image_processing`` / ``image_reference``。
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.agents.harness.common.multimodal.image_content import (
    _content_to_query_text,
    _message_content,
    _part_with_text,
    _text_from_content_part,
    filter_image_content_from_view,
    is_image_content_block,
    is_user_authored_message,
)
from jiuwenswarm.agents.harness.common.multimodal.image_processing import (
    _data_uri_bytes,
    _data_uri_visual_tokens,
    _image_block_bytes,
    _image_block_visual_tokens,
    _image_data_uri_from_path,
    _image_file_size_bytes,
    _image_file_visual_tokens,
)
from jiuwenswarm.agents.harness.common.multimodal.image_reference import (
    _IMAGE_ATTACHED_LINE_RE,
    _IMAGE_REFERENCE_LINE_RE,
    _READ_FILE_IMAGE_TEXT_RE,
    _build_image_state_line,
    _message_image_states,
    _normalize_image_file,
    build_image_reference_line,
    current_multimodal_image_files,
    normalize_image_path,
    parse_image_references,
)
from jiuwenswarm.common.utils import logger

_MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR = "_jiuwenswarm_multimodal_image_window_mutator"


def inject_image_content_from_references(
    messages: list[Any],
    *,
    image_files: list[dict[str, Any]] | None = None,
    max_total_bytes: int = 0,
    max_visual_tokens: int = 0,
    max_count: int = 0,
) -> tuple[list[Any], int]:
    """Materialize referenced images into the conversation history as ``image_url`` blocks.

    在 ``before_model_call`` 里、窗口构建（含压缩处理器）之前调用，因此：

    - 本轮新上传的图片从**第一次** LLM 调用起就按视觉 token 计入窗口，
      压缩判定看到的是含图片的上下文，而不是下一轮才看到；
    - 图像内容写入历史本身（就地修改传入的 message 对象），后续轮次直接复用
      这条消息里的 base64，不再重新读文件、不再重新缩小；
    - 每条 ``[image #n] path=...`` 引用行只处理一次，处理结果记录为
      ``[image #n attached]`` / ``[image #n omitted: ...]`` 状态行；状态行判定
      按消息作用域（同一条消息里的引用行与状态行配对），压缩摘要搬走消息后不会
      把老图重新加回；
    - 预算按「最老优先」裁剪**本次请求**的全部图片（含 read_file 注入的图），
      被裁的图片改成文字说明并记入历史，之后不会再被自动加回；
    - 计量口径是「实际发出去的图」：视觉 token 按缩小后的发送尺寸（最长边
      ``_MAX_VISION_INLINE_IMAGE_DIMENSION``）估算，字节数默认取文件元数据，
      配置了总字节预算时改为预先编码后的精确发送字节；
    - 引用序号撞车（adapter 重建后从 1 重新计数、或用户重新上传同一文件）时，
      就地改写引用行为新序号再注入，不再静默丢图；
    - 历史中的原引用行与磁盘原文件都保留，需要更高清晰度时可让模型再次
      ``read_file`` 原文件重新出图（读图护栏对 omitted 图片留出例外）；
    - 安全边界：只有**本次请求上传清单**（``current_multimodal_image_files()``，
      adapter 在 invoke 内绑定）里的路径才会被读盘物化。引用行是模型可见的纯文本，
      用户可以手写任意服务器路径；若只校验 MIME 与文件存在性，就能把 file_guard /
      沙箱之外的本地图片内联进模型请求。清单为空时一律不物化任何新像素。
    """

    messages_list = list(messages)
    # 上传清单与引用路径两侧都用 normalize_image_path 归一后比较，口径与
    # is_provided_image_path 一致（大小写 / 相对段 / ~ 形态不同不影响判定）。
    # 显式传入 image_files 时优先用它（rail 从 agent 实例读，跨请求→agent 任务边界），
    # 否则回退 ContextVar（同调用栈场景）。
    manifest = (
        list(image_files)
        if image_files is not None
        else current_multimodal_image_files()
    )
    uploaded_paths = {
        normalize_image_path(str(item.get("path") or ""))
        for item in manifest
        if str(item.get("path") or "").strip()
    }
    # 清单为空时本轮不物化任何新像素；但历史图片的预算重算仍要执行
    # （N5：预算每轮重算、最老优先降级），因此不在入口整体提前返回。
    refs_by_message: list[tuple[Any, list[dict[str, Any]]]] = []
    states_by_message: dict[int, dict[int, str]] = {}
    # 序号 → 引用/状态行所在消息，用于判定"这个序号是否已被别的消息占用"：
    # 本消息自己的引用不算撞号，别的消息里的同号引用/状态才算。
    index_owners: dict[int, set[int]] = {}
    for message in messages_list:
        if not is_user_authored_message(message):
            # 引用行只认用户消息：tool result / assistant 文本里的同格式行不物化像素，
            # 避免被读到的文件内容把本地图片内联进模型请求（绕过 file_guard）。
            continue
        message_key = id(message)
        text = _content_to_query_text(_message_content(message))
        refs = parse_image_references(text) if text else []
        refs_by_message.append((message, refs))
        states_by_message[message_key] = _message_image_states(message)
        for ref in refs:
            index = int(ref["index"])
            index_owners.setdefault(index, set()).add(message_key)
        for index in states_by_message[message_key]:
            index_owners.setdefault(index, set()).add(message_key)

    used_indices: set[int] = set(index_owners)
    next_index = (max(used_indices) + 1) if used_indices else 1

    pending: list[tuple[Any, dict[str, Any]]] = []
    seen_paths: set[str] = set()
    renumbered = 0
    for message, refs in refs_by_message:
        message_key = id(message)
        local_states = states_by_message[message_key]
        for ref in refs:
            index = int(ref["index"])
            if index in local_states:
                # 本消息已处理过这张图（附过像素或被预算卸载过），不再重复处理。
                continue
            image = _normalize_image_file(ref)
            if not image:
                logger.info(
                    "[inject_image_content] skip unusable image ref: index=%d path=%s mime=%s",
                    index,
                    ref.get("path"),
                    ref.get("mime_type"),
                )
                continue
            path = image["path"]
            path_key = normalize_image_path(path)
            if path_key not in uploaded_paths:
                # 安全门（N12 的延伸）：引用行只是模型可见的纯文本，任何能写 user
                # 消息的人都能伪造一行指向服务器任意可读图片的引用。只有本次上传
                # 清单内的路径才允许读盘物化；清单为空时整轮都不物化新像素。
                logger.warning(
                    "[inject_image_content] skip non-uploaded image ref: index=%d path=%s",
                    index,
                    path,
                )
                continue
            if index_owners.get(index, set()) - {message_key}:
                # 序号在别的消息里已被占用：adapter 重建后重新从 1 计数，或用户重新
                # 上传同一张图。换新序号并改写引用行，否则会与历史状态行撞号丢图。
                new_index = next_index
                next_index += 1
                if not _rewrite_reference_line_index(
                    message, index, str(ref.get("path") or ""), new_index
                ):
                    logger.warning(
                        "[inject_image_content] renumber failed: index=%d path=%s",
                        index,
                        path,
                    )
                    continue
                used_indices.add(new_index)
                index_owners.setdefault(new_index, set()).add(message_key)
                image["index"] = new_index
                index = new_index
                renumbered += 1
            if path_key in seen_paths:
                # 同一请求内同一路径被两条 user 消息重复引用（或同一消息重复写）：
                # 只为第一条物化像素。跳过项必须补一条状态行，否则下一轮它仍是
                # 「未处理」状态，会再被注入一份 base64（像素块重复、预算白占）。
                _append_state_marker(
                    message,
                    _build_image_state_line(index, "omitted", "duplicate"),
                )
                continue
            seen_paths.add(path_key)
            pending.append((message, image))

    logger.info(
        "[inject_image_content] refs_pending=%d renumbered=%d max_total_bytes=%d max_visual_tokens=%d max_count=%d",
        len(pending),
        renumbered,
        max_total_bytes,
        max_visual_tokens,
        max_count,
    )
    has_budget = max_total_bytes > 0 or max_visual_tokens > 0 or max_count > 0
    if not pending and not has_budget:
        return messages_list, 0

    # 先完成窗口计量，再读取像素组：默认预算（三项全 0 = 不限）只用文件元数据
    # （字节数 + 头部尺寸折算发送尺寸）计量，不读像素组（需求「先完成窗口计量，
    # 再读取像素组请求」）。配置了总字节预算时，缩小后的发送态字节数无法由元数据
    # 推出，只能预先编码拿到精确值——这是该预算下的必要代价，编码结果随后直接复用，
    # 不会重复编码。
    # 预算每轮都跑：历史里的图片（含 read_file 注入的）同样受本次请求预算约束，
    # 不是只有「本轮有新上传」才裁剪。
    existing = _collect_image_slots(messages_list)
    precomputed = _precompute_pending_data_uris(pending) if max_total_bytes > 0 else {}
    pending_sizes: list[tuple[int, int, int]] = []
    for _, image in pending:
        index = image["index"]
        data_uri = precomputed.get(index)
        size_bytes = _data_uri_bytes(data_uri) if data_uri else _image_file_size_bytes(image)
        if max_visual_tokens <= 0:
            visual_tokens = 0
        elif data_uri:
            visual_tokens = _data_uri_visual_tokens(data_uri)
        else:
            visual_tokens = _image_file_visual_tokens(image)
        pending_sizes.append((index, size_bytes, visual_tokens))
    dropped = _apply_image_budget(
        existing,
        pending_sizes,
        max_total_bytes=max_total_bytes,
        max_visual_tokens=max_visual_tokens,
        max_count=max_count,
    )

    attached = 0
    for message, image in pending:
        if image["index"] in dropped:
            _append_state_marker(
                message,
                _build_image_state_line(image["index"], "omitted", "request budget"),
            )
            continue
        data_uri = precomputed.get(image["index"]) or _image_data_uri_from_path(
            image["path"], image["mime_type"]
        )
        if data_uri:
            _append_image_part(message, image["index"], data_uri)
            attached += 1
        else:
            _append_state_marker(
                message,
                _build_image_state_line(image["index"], "omitted", "unreadable"),
            )

    logger.info(
        "[inject_image_content] attached=%d dropped=%d evicted=%d existing=%d",
        attached,
        len(dropped),
        sum(1 for slot in existing if slot.get("evicted")),
        len(existing),
    )
    return messages_list, attached


def _precompute_pending_data_uris(
    pending: list[tuple[Any, dict[str, Any]]],
) -> dict[int, str]:
    """字节预算下预先编码待注入图片，拿到缩小后的精确发送态字节数。

    只在配置了总字节预算时调用：默认（0 = 不限）不读像素，保持"先计量后读图"。
    编码结果按图片序号返回，注入阶段直接复用，一张图只编码一次。
    """

    data_uris: dict[int, str] = {}
    for _, image in pending:
        data_uri = _image_data_uri_from_path(image["path"], image["mime_type"])
        if data_uri:
            data_uris[image["index"]] = data_uri
    return data_uris


def remove_multimodal_image_filter_window_mutator(context: Any) -> bool:
    """摘掉非视觉模型分支安装的视图过滤 mutator，返回是否真的摘掉。

    同一 agent 的 ContextEngine / ModelContext 跨请求复用：上一轮按非视觉模型
    安装的 mutator 若不撤销，会把本轮 enabled 分支刚注入历史的像素在视图层再
    剥掉，模型实际收不到图，只能退回视觉工具或重复 read_file。模型能力每次
    请求都可能随模型切换变化，mutator 生命周期必须跟随当前解析值，而不是首次
    安装时的值。
    """

    mutators = getattr(context, "_window_mutators", None)
    if not isinstance(mutators, list):
        return False
    before = len(mutators)
    mutators[:] = [
        mutator
        for mutator in mutators
        if not bool(getattr(mutator, _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR, False))
    ]
    return len(mutators) != before


def ensure_multimodal_image_filter_window_mutator(context: Any) -> bool:
    mutators = getattr(context, "_window_mutators", None)
    if not isinstance(mutators, list):
        logger.info(
            "[ensure_multimodal_image_filter_window_mutator] _window_mutators not a list: %r",
            mutators,
        )
        return False

    # 幂等：重装前先摘旧实例，避免重复追加；同时把「之前是 enabled、现在是
    # disabled」的切换也收敛成恰好一个过滤 mutator。
    remove_multimodal_image_filter_window_mutator(context)

    async def multimodal_image_filter_window_mutator(_context: Any, window: Any) -> Any:
        context_messages = list(getattr(window, "context_messages", []) or [])
        view_messages, removed = filter_image_content_from_view(context_messages)
        if not removed:
            return window
        model_copy = getattr(window, "model_copy", None)
        if callable(model_copy):
            return model_copy(update={"context_messages": view_messages})
        return window

    setattr(
        multimodal_image_filter_window_mutator,
        _MULTIMODAL_IMAGE_WINDOW_MUTATOR_ATTR,
        True,
    )
    mutators.append(multimodal_image_filter_window_mutator)
    return True


def _rewrite_reference_line_index(message: Any, index: int, path: str, new_index: int) -> bool:
    """把消息里的 ``[image #index] path=...`` 引用行改写成 ``#new_index``。

    序号撞车时使用：让历史里的引用行、状态行、实际注入序号三者保持一致，
    后续轮次才能按状态行判定"已处理"，不会每轮重复注入。
    """

    parts = _message_parts(message)
    changed = False
    for part_index, part in enumerate(parts):
        text = _text_from_content_part(part)
        if not text:
            continue
        lines = text.splitlines()
        rebuilt: list[str] = []
        for line in lines:
            match = _IMAGE_REFERENCE_LINE_RE.match(line.strip())
            if match and int(match.group(1)) == index and match.group(2) == path:
                record: dict[str, Any] = {
                    "path": path,
                    "filename": match.group(3),
                    "mime_type": match.group(4),
                }
                if match.group(5) is not None:
                    record["size_bytes"] = int(match.group(5))
                rebuilt.append(build_image_reference_line(record, new_index))
                changed = True
            else:
                rebuilt.append(line)
        if rebuilt != lines:
            parts[part_index] = _part_with_text(part, "\n".join(rebuilt))
    if changed:
        _set_message_content(message, parts)
    return changed


def _message_parts(message: Any) -> list[Any]:
    content = _message_content(message)
    if isinstance(content, list):
        return list(content)
    if content is None:
        return []
    return [{"type": "text", "text": str(content)}]


def _set_message_content(message: Any, content: Any) -> None:
    if isinstance(message, dict):
        message["content"] = content
    else:
        message.content = content


def _append_parts(message: Any, parts: list[Any]) -> None:
    _set_message_content(message, _message_parts(message) + parts)


def _append_state_marker(message: Any, line: str) -> None:
    _append_parts(message, [{"type": "text", "text": line}])


def _append_image_part(message: Any, index: int, data_uri: str) -> None:
    _append_parts(
        message,
        [
            {"type": "text", "text": _build_image_state_line(index, "attached")},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ],
    )


def _collect_image_slots(messages: list[Any]) -> list[dict[str, Any]]:
    """收集历史里活着的图片块及其配对状态行，供预算按「最老优先」卸载。

    每条 slot 记录：消息对象、图片块下标、配对的 ``[image #n attached]`` 状态行下标
    （read_file 注入的图没有状态行 → None，靠前面的 ``Image loaded from read_file:``
    文本行识别来源）、字节数。视觉 token 延迟到需要时才算（解码 base64 有成本）。
    """

    slots: list[dict[str, Any]] = []
    for message in messages:
        content = _message_content(message)
        if not isinstance(content, list):
            continue
        pending_state: tuple[int, int] | None = None
        last_source_path: str | None = None
        for part_index, part in enumerate(content):
            text = _text_from_content_part(part)
            if text is not None:
                # 逐行匹配：压缩等链路会把多个文本 part 合并成一个多行 part，
                # 状态行不一定独占整段文本。与 ``_message_image_states`` 的
                # splitlines 口径保持一致，否则已 attached 的图会被当成
                # read_file 来源图（没有状态行）而误配对 / 误改名。
                for line in text.splitlines():
                    stripped = line.strip()
                    match = _IMAGE_ATTACHED_LINE_RE.match(stripped)
                    if match:
                        # 最近一条状态行配对下一个 image block（保持既有语义）。
                        pending_state = (int(match.group(1)), part_index)
                    source_match = _READ_FILE_IMAGE_TEXT_RE.match(stripped)
                    if source_match:
                        last_source_path = source_match.group(1).strip()
                continue
            if not is_image_content_block(part):
                continue
            index = pending_state[0] if pending_state else None
            state_part_index = pending_state[1] if pending_state else None
            pending_state = None
            slots.append(
                {
                    "message": message,
                    "part_index": part_index,
                    "index": index,
                    "state_part_index": state_part_index,
                    "source_path": None if index is not None else last_source_path,
                    "bytes": _image_block_bytes(part),
                    "tokens": 0,
                }
            )
            last_source_path = None
    return slots


def _slot_image_part(slot: dict[str, Any]) -> Any:
    content = _message_content(slot["message"])
    part_index = slot["part_index"]
    if isinstance(content, list) and 0 <= part_index < len(content):
        return content[part_index]
    return None


def rewrite_orphan_attached_markers(messages: list[Any]) -> int:
    """把「attached 状态行还在、像素块已被搬走」的孤儿标记改写为 ``omitted``。

    通用压缩器(DialogueCompressor 等纯文本摘要)搬走图像块时不认识
    attached/omitted 契约,会把 ``[image #n attached]`` 原样留在摘要文本里——
    消费端(附件计数 / read_file 重读护栏)按标记判定"像素已提供",模型既没有
    像素又被禁止取回,图片彻底不可得(2026-11-08 换模型回程事故)。这里按
    **消息作用域**核对:消息 content 里一个 image 块都没有、却残留 attached
    状态行 → 逐行改写为 ``omitted: compacted``;块还在的消息不动。改写就地
    生效(与 ``_apply_image_evictions`` 的就地语义一致),幂等——已是 omitted
    的行不重复处理。

    Returns:
        改写的标记行数(0 = 没有孤儿)。
    """
    rewritten = 0
    for message in messages:
        content = _message_content(message)
        if not isinstance(content, list):
            continue
        if any(is_image_content_block(part) for part in content):
            # 像素块还在(整条消息作用域):标记与块仍配对,不是孤儿。
            continue
        new_parts: list[Any] = []
        message_hit = False
        for part in content:
            text = _text_from_content_part(part)
            if text is None:
                new_parts.append(part)
                continue
            rebuilt: list[str] = []
            hit = 0
            for line in text.splitlines():
                match = _IMAGE_ATTACHED_LINE_RE.match(line.strip())
                if match:
                    rebuilt.append(
                        _build_image_state_line(int(match.group(1)), "omitted", "compacted")
                    )
                    hit += 1
                else:
                    rebuilt.append(line)
            if hit:
                message_hit = True
                rewritten += hit
                new_parts.append(_part_with_text(part, "\n".join(rebuilt)))
            else:
                new_parts.append(part)
        if message_hit:
            _set_message_content(message, new_parts)
    return rewritten


def _replace_attached_state_line(part: Any, index: int) -> Any:
    """把文本 part 里的 ``[image #n attached]`` 行改写成 ``omitted``，保留其它行。

    状态行不一定独占整个文本 part：压缩链路会把多条文本合并成一个多行 part。
    整段替换会连用户正文一起吞掉，所以按行定位、只改命中的那一行。
    """

    replacement = _build_image_state_line(index, "omitted", "request budget")
    text = _text_from_content_part(part)
    if text is None:
        return {"type": "text", "text": replacement}
    rebuilt: list[str] = []
    replaced = False
    for line in text.splitlines():
        match = _IMAGE_ATTACHED_LINE_RE.match(line.strip())
        if match and int(match.group(1)) == index:
            rebuilt.append(replacement)
            replaced = True
        else:
            rebuilt.append(line)
    if not replaced:
        # 状态行与图片块配对了却定位不到行（极端历史脏数据）：至少把事实追加到末尾。
        rebuilt.append(replacement)
    return _part_with_text(part, "\n".join(rebuilt))


def _apply_image_evictions(slots: list[dict[str, Any]]) -> None:
    """把预算裁掉的图片块改写成文字说明并记入历史（同一条消息只重建一次）。"""

    buckets: dict[int, dict[str, Any]] = {}
    for slot in slots:
        bucket = buckets.setdefault(
            id(slot["message"]),
            {"message": slot["message"], "image_parts": {}, "state_parts": {}},
        )
        bucket["image_parts"][slot["part_index"]] = slot["index"]
        if slot["index"] is not None and slot["state_part_index"] is not None:
            bucket["state_parts"][slot["state_part_index"]] = slot["index"]

    for bucket in buckets.values():
        message = bucket["message"]
        parts = _message_parts(message)
        rebuilt: list[Any] = []
        for part_index, part in enumerate(parts):
            if part_index in bucket["image_parts"]:
                index = bucket["image_parts"][part_index]
                if index is None or index not in bucket["state_parts"].values():
                    # read_file 注入的图没有状态行，就地换成省略说明；前面的路径说明行
                    # 保留，模型可以再用 read_file 按原文件重新出图。
                    rebuilt.append({"type": "text", "text": "[image omitted: request budget]"})
                # 有配对状态行时由状态行承载事实（改写为 omitted），像素块直接摘除，
                # 避免同一张图同时留下 attached / omitted 两条互相矛盾的状态。
                continue
            if part_index in bucket["state_parts"]:
                index = bucket["state_parts"][part_index]
                rebuilt.append(_replace_attached_state_line(part, index))
                continue
            rebuilt.append(part)
        _set_message_content(message, rebuilt)


def _apply_image_budget(
    slots: list[dict[str, Any]],
    pending_sizes: list[tuple[int, int, int]],
    *,
    max_total_bytes: int,
    max_visual_tokens: int,
    max_count: int,
) -> set[int]:
    """按「最老的图片先卸载」裁剪单次请求的图片预算，返回不注入的新图序号。

    ``slots`` 是历史里活着的图片块（含 read_file 注入的图）；``pending_sizes``
    元素为 ``(index, size_bytes, visual_tokens)``：默认取文件元数据（大小 + 头部尺寸
    折算发送尺寸），配置了总字节预算时由调用方预编码后传入精确发送字节与视觉 token。

    预算对「本次请求会看到的全部图片」生效，不区分本轮是否有新上传：否则没有
    新图的轮次会把历史图片无限期留在窗口里。
    """

    dropped: set[int] = set()
    if max_total_bytes <= 0 and max_visual_tokens <= 0 and max_count <= 0:
        return dropped

    if max_visual_tokens > 0:
        for slot in slots:
            slot["tokens"] = _image_block_visual_tokens(_slot_image_part(slot))

    total_bytes = sum(slot["bytes"] for slot in slots)
    total_tokens = sum(slot["tokens"] for slot in slots)
    total_count = len(slots) + len(pending_sizes)
    pending_bytes = sum(size for _, size, _tokens in pending_sizes)
    pending_tokens = sum(tokens for _, _size, tokens in pending_sizes)

    def over_budget() -> bool:
        if max_count > 0 and total_count > max_count:
            return True
        if max_total_bytes > 0 and (total_bytes + pending_bytes) > max_total_bytes:
            return True
        return max_visual_tokens > 0 and (total_tokens + pending_tokens) > max_visual_tokens

    evicted: list[dict[str, Any]] = []
    slot_index = 0
    while over_budget() and slot_index < len(slots):
        slot = slots[slot_index]
        slot_index += 1
        evicted.append(slot)
        slot["evicted"] = True
        total_bytes -= slot["bytes"]
        total_tokens -= slot["tokens"]
        total_count -= 1

    if evicted:
        _apply_image_evictions(evicted)

    for index, size, tokens in pending_sizes:
        if not over_budget():
            break
        dropped.add(index)
        pending_bytes -= size
        pending_tokens -= tokens
        total_count -= 1

    return dropped
