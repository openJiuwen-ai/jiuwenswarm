# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import json
import ast


def parse_json(output, header=None):
    try:
        json_idx = -1
        if header is not None:
            json_idx = output.find(f'{{"{header}":')
            if json_idx == -1:
                json_idx = output.find(f'{{\n"{header}":')
        if json_idx == -1:
            json_idx = output.find('{\n')
        if json_idx == -1:
            json_idx = output.find('{')
        json_end_idx = output.rfind('}')
        json_end_idx = json_end_idx + 1 if json_end_idx != -1 else -1
        output = output[json_idx:json_end_idx].strip()
        output_json = json.loads(output)     
    except json.JSONDecodeError:
        output_json = ast.literal_eval(output)
    return output_json


def format_prompt_llama(system_prompt: str, user_prompt: str):
    return system_prompt + user_prompt


def print_bold(text):
    bold = "\033[1m"
    rest = "\033[0m"