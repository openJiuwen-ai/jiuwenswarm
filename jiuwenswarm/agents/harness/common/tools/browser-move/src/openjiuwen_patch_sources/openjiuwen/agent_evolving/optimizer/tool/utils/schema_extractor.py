# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import json


def extract_schema(schema_dict):
    """Extract schema structure without type information"""
    if not isinstance(schema_dict, dict):
        try:
            schema_dict = json.loads(schema_dict)
        except Exception:
            return {}

    result = {}
    for key, value in schema_dict.items():
        if isinstance(value, dict):
            # Recursively process nested dictionaries
            result[key] = extract_schema(value)
        elif isinstance(value, list):
            # Keep lists as is (like required arrays)
            result[key] = value
        else:
            # Keep primitive values (strings, booleans, etc.)
            result[key] = ""
    return result