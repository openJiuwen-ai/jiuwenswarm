# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Client preference binding rules."""

PREFERENCE = {
    "kind": "object",
    "fields": {"key": {"kind": "string", "maxBytes": 128}, "default": {"kind": "boolean"}},
    "required": ("key", "default"),
}
