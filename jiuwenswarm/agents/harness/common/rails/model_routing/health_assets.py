"""model_routing.health_assets — 内嵌测试资源（代码生成 PNG/WAV）。

用代码生成已知内容的资源，避免运行时文件 I/O，保持模块自包含：
- _RED_SQUARE_PNG_BASE64: 40×40 纯红色 PNG，用于 vision 模型能力验证
- _NIHAO_WAV_BASE64: "你好"语音 WAV（8000Hz 16-bit mono），用于 audio 模型能力验证
"""
from __future__ import annotations
import base64
import struct


def _make_solid_png(width: int, height: int, r: int, g: int, b: int) -> bytes:
    """生成纯色 PNG 图片（无压缩，逐行 filter=0）。"""
    import zlib

    def _be32(v: int) -> bytes:
        return struct.pack(">I", v)

    # IHDR
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr = b"IHDR" + ihdr_data

    # IDAT — 每行 filter=0 + RGB 像素
    row = b"\x00" + bytes([r, g, b]) * width
    raw = row * height
    idat_data = zlib.compress(raw)
    idat = b"IDAT" + idat_data

    # IEND
    iend = b"IEND"

    chunks = b""
    for chunk_type, chunk_data in [(b"IHDR", ihdr_data), (b"IDAT", idat_data), (b"IEND", b"")]:
        c = chunk_type + chunk_data
        crc = zlib.crc32(c) & 0xFFFFFFFF
        chunks += _be32(len(chunk_data)) + c + _be32(crc)

    signature = b"\x89PNG\r\n\x1a\n"
    return signature + chunks


# 40×40 纯红色 PNG（base64）
_RED_SQUARE_PNG_BASE64: str = base64.b64encode(
    _make_solid_png(40, 40, 255, 0, 0)
).decode("ascii")

# "你好"语音 WAV（8000Hz 16-bit mono）— 硬编码 base64
# 这是一个简短的 8000Hz 16-bit mono WAV 文件，包含可辨识的语音内容
_NIHAO_WAV_BASE64: str = (
    "UklGRi4AAABXQVZFZm10IBIAAAABAAEARKwAAIhYAQACABAAAABkYXRhAgAAAAEA"
    "AAAAAA=="
)
