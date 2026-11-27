# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import re
import json
from typing import Callable, Optional, List

from openjiuwen.agent_evolving.optimizer.tool.utils.rits import get_rits_response


class ToolDescriptionReviewer:
    
    def __init__(self, eval_model_id: str, llm_api_key: str):
        self.eval_model_id = eval_model_id
        self.llm_api_key = llm_api_key
        self.processors: List[Callable] = []
    
    def format(self, json_schema: dict, description: str, example: Optional[str] = None) -> dict:
        
        prompt_original = f"""You will receive an input that contains a textual description.
The input may be free-form text, bullet points, or JSON in any structure.
Your task is to convert that content into MY target JSON format, while keeping the information and meaning exactly the same.

Do not add new information, remove information, or reinterpret ambiguous content.
Only reorganize and reformat the content according to the schema provided below.
Target JSON format: \n{json.dumps(json_schema, ensure_ascii=False, indent=2)}.

Output only valid JSON, following the exact structure of the target schema. Do not include explanations, comments, or additional text outside the JSON.

Now convert my following input to desired JSON format:
Input to be converted: \n{description}
"""
        prompt_1 = f"""You will receive an input that contains a textual description.
Your task is to convert it into the target JSON format below.

Rules:
- Preserve all information and meaning exactly.
- Do not add, remove, or reinterpret any information.
- You may rewrite and compress wording to eliminate redundancy.
- Do not restate information already implied by the schema (e.g. type=number/string, required fields).
- Enum/value lists must appear only once at the most relevant location; do not repeat them at field level.
- Use short, content-focused phrases for field descriptions.

Target JSON format:
{json.dumps(json_schema, ensure_ascii=False, indent=2)}

Output only valid JSON following the exact structure.
No explanations or extra text.

Input:
{description}
"""

        prompt_2 = f"""You will receive an input that contains a textual description.
Your task is to convert it into the target JSON format below.

Rules:
- Preserve all information and meaning exactly.
- Do not add, remove, or reinterpret any information.
- You may rewrite and compress wording to eliminate redundancy.
- Do not restate information already implied by the schema (e.g. field types, required fields).
- Do not describe required fields in natural language (e.g. phrases like “each item includes/contains …”).
- Enum/value lists must appear only once at the most relevant location.

Target JSON format:
{json.dumps(json_schema, ensure_ascii=False, indent=2)}

Output only valid JSON following the exact structure.
No explanations or extra text.

Input:
{description}
"""
        prompt = f"""将下面输入转换为目标 JSON 结构。必须满足：

- 输出只允许是有效 JSON，且严格匹配目标结构的键路径与层级（不多不少）。
- 语义必须完全保留：不新增、不删减、不改写含义；可改写措辞以压缩。
- description 去冗余是强制要求：
    - 任何 “每项包含/含有/由…组成/字段包括…” 这类字段清单式描述都必须删除或改写为非清单表述。
    - 不得在 description 中重复 schema 已表达的信息：字段名、字段类型、required 已涵盖的“必填”。
    - 仅保留 schema 无法表达或未显式表达的约束到 description，例如：
        - 覆盖区间/不得留隙/分段规则
        - 默认值语义（如 inflationRate 默认 0）
        - 业务规则（按年累加、考虑通胀等）
    - 枚举值列表只出现一次，放在最贴近字段的位置（通常是该字段的 description）；不得在父级/子级重复。
    如输入中 description 同时包含“字段清单 + 业务约束”，只保留业务约束部分。
    - 若某个 description 完全是冗余字段清单，允许变为简短描述，但不得留空（除非输入本身为空）。
- 请直接输出转换后的 JSON，不要附加解释。

这是目标的json 模板:
{json.dumps(json_schema, ensure_ascii=False, indent=2)}

下面是你需要修改的json，生成后请自检：所有 description 中不得出现“含/包含/包括/each item/contains/fields”等字段列举句式；否则重写直到满足。

Input:
{description}
"""
        
        def verify_output(output):
            return json.loads(output)
        
        response = get_rits_response(
            'gpt-5.2', 
            prompt, 
            self.llm_api_key, 
            verify_output=verify_output, 
            max_attempts=5, 
            include_stop_sequence=False,  
            verbose=False
        )
        return response
    
    @staticmethod
    def _is_mostly_english(text: str) -> bool:

        text_no_space = re.sub(r'\s+', '', text)
        
        if len(text_no_space) == 0:
            return False

        english_chars = len(re.findall(r'[a-zA-Z]', text_no_space))
        

        english_ratio = english_chars / len(text_no_space)
        

        return english_ratio > 0.7
    
    def clean_and_deduplicate(self, data: dict) -> dict:

        prompt = f"""
Given a tool description JSON, go through the content sentence 
by sentence and perform the following cleaning tasks:

1. Remove usage example in the main tool description
2. Remove redundant "必填"/"可选"/"required"/"optional" markers in parameter 
descriptions if they appear in 'required' session
3. Remove verbose, redundant descriptions including:
   - Disclaimers like "若输入无效会返回空结果", 
    "若输入代码无效或未收录会返回未找到或空结果"
   - Obvious statements like "结果可能有延迟"
   - Suggestions like "调用者应自行进行进一步分析或合成总结", 
    "调用者应在本接口返回后自行进行进一步分析"
   - Irrelevant exclusions that are clearly not in the tool's 
    functional scope. e.g. the tool name is maps_directions, 
    since it's a direction tool, statements like "不提供预订或支付功能" 
    or "不支持语音导航" is clearly irrelevant and need to be removed.
   - Any other unnecessary verbose content
4. Clean up descriptions: for parameter descriptions incorrectly 
mixed into the tool descriptions, relocate them to ensure that 
each parameter description is correctly placed in its corresponding 
parameter description instead of the main tool description session.

**Pay attention to KEEP statements on ACTUAL functionality boundaries**
Keep only unique, essential, and actionable information. Output only the 
cleaned JSON without explanations. DO NOT change the overall structure of JSON.

Input JSON:
{json.dumps(data, ensure_ascii=False, indent=2)}
"""
        
        def verify_output(output):
            return json.loads(output)
        
        response = get_rits_response(
            self.eval_model_id, 
            prompt, 
            self.llm_api_key, 
            verify_output=verify_output, 
            max_attempts=5, 
            include_stop_sequence=False,  
            verbose=False
        )
        return response
    
    def cross_check(self, data: dict, ori_tool: str):
        prompt = f"""比较原始描述和修改后的描述，按照以下要求整理修改后的描述：
1. 补充修改后的描述丢失的信息：例如，参数可选值列表丢失，需把原始描述中的列表补充道修改后的对应位置。
2. 确保参数描述信息和工具描述信息位置正确：参考原始描述，确保工具描述中只包含对工具能力、边界等信息，确保参数具体细节要求应在对应的参数描述中，例如：“仅支持经纬度作为输入”应当放在对应的参数描述中，不应当放在主工具能力边界中。

确保不要改变json格式，仅修改文字内容。不要删除内容，仅做整理和补充丢失信息。

原始描述：
{ori_tool}

修改后描述（待优化）：
{json.dumps(data, ensure_ascii=False, indent=2)}
"""
        
        def verify_output(output):
            return json.loads(output)
        
        response = get_rits_response(
            self.eval_model_id, 
            prompt, 
            self.llm_api_key, 
            verify_output=verify_output, 
            max_attempts=5, 
            include_stop_sequence=False,  
            verbose=False
        )
        return response

    def translate_to_chinese(self, data: dict) -> dict:

        json_str = json.dumps(data, ensure_ascii=False)
        
        if not self._is_mostly_english(json_str):

            return data
        
        prompt = f"""Translate all English text in the following JSON to Chinese.
Keep JSON structure unchanged. Keep technical terms and code examples as-is.
Output only the translated JSON without explanations.

Input JSON:
{json.dumps(data, ensure_ascii=False, indent=2)}
"""
        
        def verify_output(output):
            return json.loads(output)
        
        response = get_rits_response(
            self.eval_model_id, 
            prompt, 
            self.llm_api_key, 
            verify_output=verify_output, 
            max_attempts=5, 
            include_stop_sequence=False,  
            verbose=False
        )
        return response

    def process(self, data: dict, ori_tool: str, steps: List[str]) -> dict:

        result = data
        
        for step in steps:
            if step == "cross_check":
                result = self.cross_check(data=data, ori_tool=ori_tool)
            elif step == "clean":
                result = self.clean_and_deduplicate(result)
            elif step == "translate":
                result = self.translate_to_chinese(result)
            else:
                raise ValueError(f"Unknown processing step: {step}")
        return result





    
