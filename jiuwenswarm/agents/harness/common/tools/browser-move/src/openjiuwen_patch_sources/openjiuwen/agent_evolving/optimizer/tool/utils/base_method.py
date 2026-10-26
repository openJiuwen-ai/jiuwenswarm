# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import os
from typing import List, Dict, Any, Optional, Union
import ast
import json

from openjiuwen.agent_evolving.optimizer.tool.utils.rits import get_rits_response


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
    reset = "\033[0m"



class BaseMethod:
    def __init__(
        self, 
        config: Dict[str, Union[str, int, bool]],
    ):
        self.config = config
        self.verbose = config['verbose'] if 'verbose' in config else False

    def produce_answer_from_api_call(self, instruction: str, doc_str: str, api_response: str):
        user_prompt = f'''
Please respond in natural language text. Do not include code in your responses. You are given an API tool with the following documentation, which includes the functionality description, required parameters, code snippets for API calls, etc.

Documentation:
{doc_str}

You are given the following instruction: "{instruction}"
To produce a response to the instruction, you made an API call to the given tool, which returned the following results:
{api_response}

Given the instruction and the results of API call, produce an effective and short answer (less than 300 letters) to the user in natural language. Your answer must be based on the results of the API call, do not hallucinate or answer anything not in the API results. You must not include code, comments, JSON data structures, notes, or other irrelevant information in your answer. If there is an error or failure using the tool, you must report the error in your answer and do not make things up, especially when you receive an input about invalid parameters. Also, absolutely do NOT tell a user about a simulated response. Treat every successful API output as real. Every successful API call contains real data. This is very important.

Finally, organize your output in the following JSON format:
{{
    "answer": answer
}}
You must strictly follow the output format. You can begin your task now.'''
        
        def verify_output(output):
            output_json = parse_json(output)

            if not isinstance(output_json, dict):
                raise ValueError("Output must be a dict.")

            if "answer" not in output_json:
                raise ValueError('"answer" field is required.')

            if "error" in output_json:
                raise ValueError(output_json.get("error"))

            return output_json["answer"].strip()

        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)
        output = get_rits_response(
            self.config['gen_model_id'],
            prompt,
            self.config['llm_api_key'],
            verify_output,
            max_attempts=15,
            include_stop_sequence=False,
            stop_sequences=['<|eot_id|>', '<|end_of_text|>', '<|eom_id|>'],
            verbose=self.config['verbose']
        )
        if self.config['verbose']:
            print_bold('Final LLM output: ')
            print_bold(output)
        return output
