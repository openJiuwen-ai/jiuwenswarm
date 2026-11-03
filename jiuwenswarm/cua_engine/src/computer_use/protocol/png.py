"""PNG framing, checksum and bounded scanline validation (native wire v3)."""
from __future__ import annotations

import base64
import math
import re
import struct
import zlib


class PNG(bytearray):
    """Image bytes with identity; copying bytes does not copy capture metadata."""

    def __repr__(self):
        return getattr(self, "display_summary", None) or super().__repr__()

    def __str__(self):
        return repr(self)


_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def decode_png(value) -> PNG:

    def fail():
        raise ValueError("protocol: invalid PNG Base64 or corrupt PNG data")

    if not isinstance(value, str) or not value or len(value) > 64 * 1024 * 1024:
        fail()
    try:
        data = base64.b64decode(value, validate=True)
    except ValueError:
        fail()
    if base64.b64encode(data).decode("ascii") != value or data[:8] != _SIGNATURE:
        fail()
    offset = 8
    width = height = depth = color = interlace = 0
    ended = palette = data_ended = False
    chunks = []
    while offset < len(data):
        if offset + 12 > len(data):
            fail()
        length = struct.unpack_from(">I", data, offset)[0]
        end = offset + length + 12
        if end > len(data) or ended:
            fail()
        # Buffer.toString('ascii') masks the high bit in the existing decoder.
        kind = bytes(v & 127 for v in data[offset + 4:offset + 8]).decode("ascii")
        payload = data[offset + 8:end - 4]
        if re.fullmatch(r"[A-Za-z]{4}", kind) is None or zlib.crc32(data[offset + 4:end - 4]) != struct.unpack_from(
                ">I", data, end - 4)[0]:
            fail()
        if offset == 8 and kind != "IHDR":
            fail()
        if kind == "IHDR":
            if offset != 8 or length != 13:
                fail()
            width, height = struct.unpack_from(">II", payload)
            depth, color, interlace = payload[8], payload[9], payload[12]
            invalid_header = not width or not height or width > 32768 or height > 32768 or depth not in _DEPTHS.get(
                color, ()) or payload[10] or payload[11] or interlace > 1
            if invalid_header:
                fail()
        elif kind == "PLTE":
            invalid_palette = palette or chunks or not length or length % 3 or length > 768 or color in (0, 4)
            if invalid_palette:
                fail()
            palette = True
        elif kind == "IDAT":
            if data_ended or (color == 3 and not palette):
                fail()
            chunks.append(payload)
        elif kind == "IEND":
            if length or not chunks:
                fail()
            ended = True
        elif kind[0] == kind[0].upper():
            fail()
        if chunks and kind != "IDAT":
            data_ended = True
        offset = end
    if not ended:
        fail()
    passes = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2),
              (0, 1, 1, 2)] if interlace else [(0, 0, 1, 1)]
    rows, expected = [], 0
    for x, y, dx, dy in passes:
        w, h = max(0, math.ceil((width - x) / dx)), max(0, math.ceil((height - y) / dy))
        if not w or not h:
            continue
        size = 1 + math.ceil(w * _CHANNELS[color] * depth / 8)
        expected += size * h
        rows.append((h, size))
    if expected > 256 * 1024 * 1024:
        fail()
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(b"".join(chunks), expected + 1)
        if len(raw) != expected or not inflater.eof:
            fail()
    except zlib.error:
        fail()
    offset = 0
    for count, size in rows:
        for _ in range(count):
            if raw[offset] > 4:
                fail()
            offset += size
    return PNG(data)


def png_size(png) -> dict[str, int]:
    if len(png) < 24 or bytes(png[:8]) != _SIGNATURE:
        raise ValueError("protocol: invalid PNG header")
    width, height = struct.unpack_from(">II", png, 16)
    if not width or not height:
        raise ValueError("protocol: invalid PNG dimensions")
    return dict(width=width, height=height)
