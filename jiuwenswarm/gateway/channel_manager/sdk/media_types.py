# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared media-type helpers (E1 connector SDK).

Harvested verbatim from the byte-identical copies that lived in
``dingtalk_file_service.py`` and ``wecom_file_service.py``: magic-number
based extension detection and extension-to-MIME mapping.
"""

from __future__ import annotations

# 文件魔数映射（用于格式检测）
FILE_SIGNATURES = {
    # 图片
    b'\x89PNG': '.png',
    b'\xff\xd8\xff': '.jpg',
    b'GIF8': '.gif',
    b'RIFF': '.webp',  # 需要进一步检查 WEBP 标识
    # 音频
    b'ID3': '.mp3',
    b'\xff\xfb': '.mp3',
    b'\xff\xfa': '.mp3',
    b'fLaC': '.flac',
    b'OggS': '.ogg',
    # 视频
    b'ftyp': '.mp4',
    b'moof': '.mp4',
    b'moov': '.mp4',
    b'\x1a\x45\xdf\xa3': '.mkv',
    b'FLV': '.flv',
}

# MIME 类型映射
MIME_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.ogg': 'audio/ogg',
    '.flac': 'audio/flac',
    '.mp4': 'video/mp4',
    '.mkv': 'video/x-matroska',
    '.flv': 'video/x-flv',
    '.pdf': 'application/pdf',
    '.doc': 'application/msword',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xls': 'application/vnd.ms-excel',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.ppt': 'application/vnd.ms-powerpoint',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.txt': 'text/plain',
    '.zip': 'application/zip',
    '.json': 'application/json',
}


def detect_file_extension(content: bytes) -> str:
    """通过文件头魔数检测文件扩展名。"""
    if len(content) < 12:
        return ''

    # 检查 WEBP（RIFF....WEBP）
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return '.webp'

    # 检查 WAV（RIFF....WAVE）
    if content[:4] == b'RIFF' and content[8:12] == b'WAVE':
        return '.wav'

    # 检查 MP4（ftyp/moof/moov）
    if content[4:8] in (b'ftyp', b'moof', b'moov'):
        return '.mp4'

    # 检查其他格式
    for signature, ext in FILE_SIGNATURES.items():
        if content.startswith(signature):
            return ext

    return ''


def get_mime_type(extension: str) -> str:
    """获取文件扩展名对应的 MIME 类型。"""
    return MIME_TYPES.get(extension.lower(), 'application/octet-stream')