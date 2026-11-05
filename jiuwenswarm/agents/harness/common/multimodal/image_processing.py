# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Image shrinking and visual-token measurement.

本包唯一持有 PIL 的模块：图片缩小/编码、data URI 构造、视觉 token 估算与字节计量。
不 import 包内其它模块（``base64`` / ``hashlib`` / ``PIL`` 之外只依赖日志）。
"""

from __future__ import annotations

import base64
import hashlib
import io
from collections import OrderedDict
from pathlib import Path
from typing import Any

from PIL import Image

from jiuwenswarm.common.utils import logger

_VISION_INLINE_IMAGE_MIME_TYPES = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})
_MAX_VISION_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_VISION_INLINE_IMAGE_DIMENSION = 2048

_IMAGE_FORMAT_TO_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

_IMAGE_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_JPEG_QUALITY_STEPS = (95, 85, 70, 50, 30, 10)
_WEBP_QUALITY_STEPS = (95, 85, 70, 50, 30, 10)
_SHRINK_SCALE_STEP = 0.85
_MIN_SHRINK_DIMENSION = 32


# 源文件大小硬上限：先 stat 再读，超过直接拒绝。否则超大文件会在
# ``read_bytes()`` 里整体进内存，一次请求就可能把进程打爆。
_MAX_IMAGE_SOURCE_BYTES = 64 * 1024 * 1024


# 历史图片块的视觉 token 缓存。预算每轮都会遍历历史图，而 data URI 可达数 MB，
# 不能拿大字符串当 ``lru_cache`` 的 key（常驻内存 + 哈希成本都在大字符串上）；
# 这里按 payload 的 sha256 摘要缓存，命中直接返回、不再 base64 解码 + PIL 开图。
# 上限 64 条并做 LRU 淘汰：单请求窗口外的旧图自然淘汰，不会无界增长。
_IMAGE_VISUAL_TOKEN_CACHE_LIMIT = 64
_IMAGE_VISUAL_TOKEN_CACHE: OrderedDict[bytes, int] = OrderedDict()


# 视觉 token 计量（OpenAI GPT-4o 风格 tile 算法）：图片在上下文窗口中的占用
# 按分辨率估算，而不是按 base64 字符长度或文件字节数。
# 这是**预算裁剪用的近似值**，不参与正确性判定：不同厂商的视觉 token 定价不同
# （Gemini / Claude / Qwen-VL 与 GPT-4o 有差异），统一按同一系数估算只会让
# `multimodal_image_token_budget` 的裁剪点偏松或偏紧，不会改变发送内容与状态行语义。
_VISION_TILE_SIZE = 512
_VISION_BASE_TOKENS = 85
_VISION_TOKENS_PER_TILE = 170


def _image_block_bytes(part: Any) -> int:
    if not isinstance(part, dict):
        return 0
    image_url = part.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url:
        return 0
    if not url.startswith("data:"):
        # 远程 URL 的图片字节数无法从字符串推断；不能把 URL 文本当 base64 估算
        # （会得到 len(url)*3//4 这种无意义的小数字，破坏字节预算）。
        return 0
    payload = url.partition("base64,")[2]
    return len(payload) * 3 // 4


def _data_uri_bytes(data_uri: str) -> int:
    payload = data_uri.partition("base64,")[2] if data_uri.startswith("data:") else data_uri
    return len(payload) * 3 // 4


def _image_file_size_bytes(image: dict[str, Any]) -> int:
    """图片元数据字节数：优先用记录里的 ``size_bytes``，否则 stat 磁盘文件。

    只读元数据、不读像素，供预算计量在读取像素组之前使用。
    """

    size = image.get("size_bytes")
    if isinstance(size, int) and size > 0:
        return size
    try:
        return Path(str(image.get("path") or "")).stat().st_size
    except OSError:
        return 0


def estimate_image_visual_tokens(width: int, height: int) -> int:
    """按分辨率估算图片的视觉 token 占用（OpenAI GPT-4o 风格 tile 算法）。

    图片在上下文窗口里的占用应按视觉 token 计，而不是 base64 字符长度或
    文件字节数：一张高分辨率图即便字节很小，其视觉 token 也很大。

    入参必须是**实际发送尺寸**：发送前的缩小链路会把最长边压到
    ``_MAX_VISION_INLINE_IMAGE_DIMENSION``，按原图尺寸计价会高估数倍。
    """

    if width <= 0 or height <= 0:
        return 0
    tiles_x = (width + _VISION_TILE_SIZE - 1) // _VISION_TILE_SIZE
    tiles_y = (height + _VISION_TILE_SIZE - 1) // _VISION_TILE_SIZE
    return _VISION_BASE_TOKENS + _VISION_TOKENS_PER_TILE * tiles_x * tiles_y


def _clamp_sent_dimensions(width: int, height: int) -> tuple[int, int]:
    """把原图尺寸折算成发送前的缩小尺寸（最长边不超过内联上限，等比缩放）。

    与 :func:`_image_data_uri_from_path` / agent-core ``shrink_image_bytes`` 使用
    同一个上限：预算计量按「实际发出去的像素」算，否则原图 4000×3000 会被
    缩到 1536×1152，而计量仍按原尺寸多算约 5 倍。
    """

    longest = max(width, height)
    if longest <= _MAX_VISION_INLINE_IMAGE_DIMENSION:
        return width, height
    scale = _MAX_VISION_INLINE_IMAGE_DIMENSION / longest
    return max(1, round(width * scale)), max(1, round(height * scale))


def _read_image_dimensions(path: str) -> tuple[int, int]:
    """轻量读取图片像素尺寸：只读头部元数据，不解码像素组。

    返回 ``(width, height)``，读取失败返回 ``(0, 0)``。
    """

    try:
        with Image.open(path) as image:
            return image.size
    except Exception:
        return (0, 0)


def _image_file_visual_tokens(image: dict[str, Any]) -> int:
    """图片的视觉 token 估算（按发送态尺寸）：优先记录宽高，否则读文件头部。"""

    width = image.get("width")
    height = image.get("height")
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        width, height = _read_image_dimensions(str(image.get("path") or ""))
    return estimate_image_visual_tokens(*_clamp_sent_dimensions(width, height))


def _image_block_visual_tokens(part: Any) -> int:
    """已注入历史里的 ``image_url`` 块的视觉 token：从 data URI 头部读尺寸。

    每轮预算都会遍历全部历史图，而单张 data URI 可达数 MB：不缓存的话每轮都要
    base64 解码 + PIL 开图。按 payload 的 sha256 摘要做有界 LRU 缓存，命中后直接
    返回 token 数，不再解码。失败结果不缓存，避免一次坏数据永久污染。
    """

    if not isinstance(part, dict):
        return 0
    image_url = part.get("image_url")
    url = image_url.get("url") if isinstance(image_url, dict) else image_url
    if not isinstance(url, str) or not url.startswith("data:"):
        return 0
    payload = url.partition("base64,")[2]
    if not payload:
        return 0
    cache_key = hashlib.sha256(payload.encode("ascii", "replace")).digest()
    cached = _IMAGE_VISUAL_TOKEN_CACHE.get(cache_key)
    if cached is not None:
        _IMAGE_VISUAL_TOKEN_CACHE.move_to_end(cache_key)
        return cached
    try:
        with Image.open(io.BytesIO(base64.b64decode(payload))) as image:
            width, height = image.size
    except Exception:
        return 0
    tokens = estimate_image_visual_tokens(width, height)
    _IMAGE_VISUAL_TOKEN_CACHE[cache_key] = tokens
    while len(_IMAGE_VISUAL_TOKEN_CACHE) > _IMAGE_VISUAL_TOKEN_CACHE_LIMIT:
        _IMAGE_VISUAL_TOKEN_CACHE.popitem(last=False)
    return tokens


def _data_uri_visual_tokens(data_uri: str) -> int:
    """已缩小 data URI 的视觉 token：头部尺寸即发送尺寸，无需再折算。"""

    return _image_block_visual_tokens({"image_url": {"url": data_uri}})


def shrink_image(path: str, *, max_bytes: int, max_dimension: int) -> str:
    """Return a base64 ``data:`` URI for ``path``, shrinking the image when needed.

    agent-core 提供 ``image_shrink`` 时复用它（该模块由 core !3521 / 本地提交
    b5275fe4b 引入；锁定 ``uv.lock`` 版本尚无该模块，自动回退本地实现），保证
    上传图与 read_file 图走同一套缩小链路：够小原样送出，超限才按质量从高到低
    逐档压缩，第一档放得下就停。只产出内存副本，源文件永不修改；读不到或无法
    解码时返回空串。

    这里的跨仓依赖是「同名同签名的可选模块」：core 尚未发布时导入失败，未来签名
    漂移时调用失败，两种情况都回退本地实现并记日志，不让单张图片的编码细节把整个
    请求打挂（N9：失败降级不阻断）。

    入口先做源文件大小预检（与 ``_shrink_image_local`` 同一阈值）：无论最终走 core
    还是本地实现，超大文件都不得整体读进内存。
    """

    try:
        if Path(path).stat().st_size > _MAX_IMAGE_SOURCE_BYTES:
            logger.warning(
                "image source too large, skip: path=%s limit=%d",
                path,
                _MAX_IMAGE_SOURCE_BYTES,
            )
            return ""
    except OSError:
        # stat 失败由具体实现走各自的失败降级（返回空串），这里只负责大小门禁。
        pass
    try:
        from openjiuwen.harness.tools.multimodal.image_shrink import (
            shrink_image_to_data_uri,
        )
    except (ImportError, AttributeError) as exc:
        # 锁定版本（uv.lock 的 rev 尚无该模块）走这里：回退是常态，不是异常。
        logger.debug(
            "agent-core image_shrink unavailable, fall back to local shrink: %s", exc
        )
        return _shrink_image_local(
            path, max_bytes=max_bytes, max_dimension=max_dimension
        )
    try:
        return shrink_image_to_data_uri(
            path,
            max_bytes=max_bytes,
            max_dimension=max_dimension,
        )
    except TypeError as exc:
        # 未来 core 改了同名函数签名时不得让请求失败：回退本地实现。
        logger.warning(
            "agent-core image_shrink signature mismatch, fall back to local shrink: %s",
            exc,
        )
        return _shrink_image_local(
            path, max_bytes=max_bytes, max_dimension=max_dimension
        )


def _shrink_image_local(path: str, *, max_bytes: int, max_dimension: int) -> str:
    """读取并缩小本地图片；先 stat 做大小预检，超大源文件直接拒绝。"""

    file_path = Path(path)
    if not file_path.is_file():
        return ""
    try:
        if file_path.stat().st_size > _MAX_IMAGE_SOURCE_BYTES:
            logger.warning(
                "image source too large, skip: path=%s limit=%d",
                file_path,
                _MAX_IMAGE_SOURCE_BYTES,
            )
            return ""
    except OSError:
        return ""
    try:
        data = file_path.read_bytes()
    except OSError:
        return ""
    if not data:
        return ""

    try:
        with Image.open(file_path) as image:
            image.load()
            mime_type = _IMAGE_FORMAT_TO_MIME.get(image.format or "") or _IMAGE_EXT_TO_MIME.get(
                file_path.suffix.lower(), ""
            )
            if not mime_type:
                return ""

            width, height = image.size
            if len(data) <= max_bytes and max(width, height) <= max_dimension:
                return _build_data_uri(data, mime_type)

            encoded_bytes = _shrink_image_to_bytes(image, mime_type, max_bytes, max_dimension)
        if encoded_bytes:
            return _build_data_uri(encoded_bytes, mime_type)
    except Exception:
        logger.debug(
            "image pre-encode failed: path=%s", file_path, exc_info=True
        )
    return ""


def _build_data_uri(data: bytes, mime_type: str) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _shrink_image_to_bytes(
    image: Any,
    mime_type: str,
    max_bytes: int,
    max_dimension: int,
) -> bytes | None:
    current = _resize_to_max_dimension(_prepare_for_resize(image), max_dimension)
    encoded = _encode_within_bytes(current, mime_type, max_bytes)
    if encoded is not None:
        return encoded

    while max(current.size) > _MIN_SHRINK_DIMENSION:
        next_dimension = int(max(current.size) * _SHRINK_SCALE_STEP)
        current = _resize_to_max_dimension(current, next_dimension)
        encoded = _encode_within_bytes(current, mime_type, max_bytes)
        if encoded is not None:
            return encoded
    return None


def _prepare_for_resize(image: Any) -> Any:
    if image.mode in ("RGB", "RGBA", "L"):
        return image
    if image.mode in ("LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        return image.convert("RGBA")
    return image.convert("RGB")


def _resize_to_max_dimension(image: Any, max_dimension: int) -> Any:
    width, height = image.size
    longest = max(width, height)
    if longest <= max_dimension:
        return image
    ratio = max_dimension / longest
    size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _encode_within_bytes(image: Any, mime_type: str, max_bytes: int) -> bytes | None:
    if mime_type == "image/png":
        encoded = _save_image(image, image_format="PNG", optimize=True)
        return encoded if len(encoded) <= max_bytes else None
    if mime_type == "image/jpeg":
        rgb = image.convert("RGB")
        for quality in _JPEG_QUALITY_STEPS:
            encoded = _save_image(rgb, image_format="JPEG", quality=quality, optimize=True)
            if len(encoded) <= max_bytes:
                return encoded
        return None
    if mime_type == "image/webp":
        for quality in _WEBP_QUALITY_STEPS:
            encoded = _save_image(image, image_format="WEBP", quality=quality)
            if len(encoded) <= max_bytes:
                return encoded
        return None
    if mime_type == "image/gif":
        encoded = _save_image(image.convert("RGB"), image_format="GIF", optimize=True)
        return encoded if len(encoded) <= max_bytes else None
    return None


def _save_image(image: Any, *, image_format: str, **kwargs: Any) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format=image_format, **kwargs)
    return buf.getvalue()


def _image_data_uri_from_path(path_text: str, mime_type: str) -> str:
    path = Path(path_text.strip())
    if not path.is_file():
        return ""
    if mime_type not in _VISION_INLINE_IMAGE_MIME_TYPES:
        return ""
    return shrink_image(
        str(path),
        max_bytes=_MAX_VISION_INLINE_IMAGE_BYTES,
        max_dimension=_MAX_VISION_INLINE_IMAGE_DIMENSION,
    )
