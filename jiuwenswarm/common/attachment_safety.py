# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.attachment_safety``。"""

from swarm_base.attachment_safety import (  # noqa: F401
    FORBIDDEN_DOCUMENT_EXTENSIONS,
    _BIDI_CONTROL_CHARS,
    _IMAGE_FILENAME_SUFFIX_ALIASES,
    _MAX_FILENAME_BYTES,
    _MAX_SESSION_ID_CHARS,
    _SUPPORTED_IMAGE_MIME_TYPES,
    _UNSAFE_FILENAME_CHARS,
    _UNSAFE_SESSION_CHARS,
    _WINDOWS_RESERVED_NAMES,
    _document_suffix,
    _sanitize_name,
    _truncate_filename_bytes,
    _truncate_text_bytes,
    ensure_image_upload_filename,
    forbidden_formats,
    image_suffix_for_mime,
    is_forbidden_document,
    safe_session_dirname,
    safe_upload_filename,
    strip_data_uri_prefix,
    supported_image_suffixes,
)
