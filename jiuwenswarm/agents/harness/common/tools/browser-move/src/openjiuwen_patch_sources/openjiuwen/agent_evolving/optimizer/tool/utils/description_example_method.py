# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import os
import json
from typing import List, Dict, Tuple, Any, Optional, Union

from openjiuwen.agent_evolving.optimizer.tool.utils.base_method import BaseMethod
from openjiuwen.agent_evolving.optimizer.tool.utils.rits import get_rits_response
from openjiuwen.agent_evolving.optimizer.tool.utils.format import format_prompt_llama, parse_json
from openjiuwen.core.common.logging import logger


class ToolDescriptionMethod(BaseMethod):
    def __init__(
        self, 
        config: Dict[str, Union[str, int, bool]], 
        eval_fn,
    ):
        super().__init__(config)
        self.eval_fn = eval_fn

    def step(
        self,
        tool: Dict[str, Any],
        examples: Optional[List[Tuple[str, Any, str, str]]] = None,
        prev_outputs: Optional[List[Dict]] = None,
        it: int = 0,
        **kwargs,
    ):
        if it == 0:
            description = self.get_original_description(tool) 
            # TOOL IS openai tool format, may change to improve it directly, not just desc part
            output = {'description': description, 'iteration': 0}
            logger.info(f"Current description - original description: {output}")
        else:
            # load negative ex
            function_name = tool['name']
            neg_examples = self.get_negative_examples(function_name)
            examples_obtained = {"neg_examples": neg_examples, "examples": examples}
            # improve with neg ex
            output = self.generate(tool, examples_obtained, prev_outputs, it)
            logger.info(f"Current description - generated description: {output}")

        # eval with pos ex
        results = self.eval_loop(tool, output['description'], examples, runs=1)
        output = output | results
        return output, output.get("description"), output.get("score_avg")

    def generate(
        self,
        tool: Dict[str, Any],
        examples: Optional[List[Tuple[str, Any, str, str]]] = None,
        prev_outputs: Optional[List[Dict]] = None,
        it: int = 0,
    ):
        logger.info("Generating desc")
        output = self.generate_description_from_documentation(
            tool, examples, prev_outputs,
        )
        logger.info("Generating desc finished")
        output['iteration'] = it
        return output

    def eval_loop(
        self,
        tool: Dict[str, Any],
        description: str,
        examples: List[Any],
        runs: int = 1,
    ):
        return self.eval_fn(tool, description, examples, runs)

    def critique_descriptions(
        self,
        tool: Dict[str, Any],
        examples: Optional[List[Tuple[str, Any, str, str]]] = None,
        prev_outputs: Optional[List[Dict[str, List[Any]]]] = None,
    ):
        function_name = tool['name']
        doc_str = json.dumps(tool, ensure_ascii=False)
        user_prompt = f'''
        You are given a function {function_name} with the following documentation, which includes the functionality description, required parameters, code snippets for API calls, etc.

        Documentation:
        {doc_str}

        '''

        if len(examples) > 0 and prev_outputs is not None and len(prev_outputs) > 0:
            user_prompt += (
                "\nPreviously, the given tool was used in solving instructions "
                "by a tool assistant with the following function descriptions:\n"
            )
            for output in prev_outputs[::-1][:self.config['num_feedback_steps']][::-1]:
                if output['iteration'] == 0:
                    user_prompt += "Original description: "
                else:
                    user_prompt += f"Iteration #{output['iteration']}, description="
                user_prompt += f"{output['description']}\n"
                user_prompt += (
                    "Here are the instructions the assistant "
                    "tried to solve with this tool description, with "
                    "their corresponding answers and errors produced by the assistant: "
                ) 
                for i, ((inst, fn_call, fn_output, ans), result) in enumerate(zip(examples, output['results']), 1):
                    user_prompt += f"{i}. instruction=\"{inst}\", answer=\"{result['answer']}\", errors: "
                    if len(result['errors']) == 0:
                        user_prompt += 'None'

                    for j, error in enumerate(result['errors']):
                        user_prompt += (f"({j}) function_call={error['function_name']},"
                                        f" arguments={json.dumps(error['arguments'])}, "
                                        f"error={error['error_msg'][:512]} "
                                    )
                    user_prompt += f". The ground truth function_call and arguments should be: {json.dumps(fn_call)}.\n"

                user_prompt += f"Overall the performance of this description is:"
                user_prompt += f" score={output['score_avg']}%, stdev={output['score_std']}.\n"

            user_prompt += '''

            Now your task is to critique the descriptions based on these results. A good description maximizes the score, minimizing the stdev, and helps the assistant correctly use the function without errors. In your analysis:
            (1) Identify how the descriptions affect the function call errors of the assistant. Be specific on which errors the assistant tends to make, and find patterns in the description that causes the assistant to make such errors.
            (2) Identify and contrast the patterns of descriptions that have achieved good scores (> 60%) with those that have not. Analyze how the description can be improved. 

            Your analysis should be less than 500 characters long, do not violate.
            '''

        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)

        def verify_output(output):
            return {'analysis': output.strip()}
        return get_rits_response(
            self.config['eval_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            include_stop_sequence=False, 
            verbose=self.config['verbose']
        )

    def critique_negative_examples(
        self,
        tool: Dict[str, Any],
        examples: Optional[List[Tuple[str, Any, str, str]]] = None
    ):
        function_name = tool['name']
        doc_str = json.dumps(tool, ensure_ascii=False)
        user_prompt = f'''
        You are given a function {function_name} with the following documentation, which includes the functionality description, required parameters, code snippets for API calls, etc.

        Documentation:
        {doc_str}
        '''

        if len(examples) > 0:
            user_prompt += (
                "\nPreviously, the given tool was used in solving instructions "
                "by a tool assistant with the following function descriptions:\n"
            )
            user_prompt += (
                "Here are the instructions the assistant "
                "tried to solve with this tool description, with "
                "their corresponding answers and errors produced by the assistant: "
            ) 
            for i, (inst, fn_call, fn_output, ans) in enumerate(examples, 1):
                if len(fn_output) > 256:
                    fn_output = fn_output[:256]
                    user_prompt += f"Example response of the function: {fn_output}, etc"
                else:
                    user_prompt += f"Response of the function: {fn_output}"

                user_prompt += f"{i}. instruction=\"{inst}\""
                user_prompt += f". The system generated function call as below "
                user_prompt += f"  base on the original documentation: {json.dumps(fn_call)}.\n"
                user_prompt += f"The runction output obtained is {fn_output}: fn_output. "
                user_prompt += f"And thus result to answer=\"{ans}\""

            user_prompt += '''

            Now your task is to critique the descriptions based on these results. In your analysis:
            (1) Identify how the descriptions affect the function call errors of the assistant. Be specific on which errors the assistant tends to make, and find patterns in the description that causes the assistant to make such errors.
            (2) Identify any constrains or limitations the tool have. Analyze how the description can be improved so that it reflect the ability constrains.

            Your analysis should be less than 500 characters long, do not violate.
            '''

        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)

        def verify_output(output):
            return {'analysis': output.strip()}
        return get_rits_response(
            self.config['eval_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            include_stop_sequence=False, 
            verbose=self.config['verbose']
        )
    
    def critique_descriptions(
        self,
        tool: Dict[str, Any],
        examples: Optional[List[Tuple[str, Any, str, str]]] = None,
        prev_outputs: Optional[List[Dict[str, List[Any]]]] = None,
    ):
        function_name = tool['name']
        doc_str = json.dumps(tool, ensure_ascii=False)
        user_prompt = f'''
        You are given a function {function_name} with the following documentation, which includes the functionality description, required parameters, code snippets for API calls, etc.

        Documentation:
        {doc_str}

        '''

        if len(examples) > 0 and prev_outputs is not None and len(prev_outputs) > 0:
            # Separate positive and negative examples based on performance threshold
            positive_examples = []
            negative_examples = []
            performance_threshold = 60.0  # Configurable threshold
            
            for output in prev_outputs[::-1][:self.config['num_feedback_steps']][::-1]:
                if output['score_avg'] >= performance_threshold:
                    positive_examples.append(output)
                else:
                    negative_examples.append(output)
            
            # Add positive examples section
            if positive_examples:
                user_prompt += "\n=== POSITIVE EXAMPLES (Good Performance) ===\n"
                user_prompt += "The following tool descriptions achieved good performance:\n\n"
                
                for output in positive_examples:
                    if output['iteration'] == 0:
                        user_prompt += "Original description: "
                    else:
                        user_prompt += f"Iteration #{output['iteration']}, description="
                    user_prompt += f"{output['description']}\n"

                    user_prompt += "Instructions solved successfully: "
                    for i, ((inst, fn_call, fn_output, ans), result) in enumerate(zip(examples, output['results']), 1):
                        user_prompt += f"{i}. instruction=\"{inst}\", answer=\"{result['answer']}\", errors: "
                        if len(result['errors']) == 0:
                            user_prompt += 'None'
                        else:
                            for j, error in enumerate(result['errors']):
                                user_prompt += (f"({j}) function_call={error['function_name']},"
                                        f" arguments={json.dumps(error['arguments'])}, "
                                        f"error={error['error_msg'][:512]} "
                                    )
                        user_prompt += f". Ground truth: {json.dumps(fn_call)}.\n"

                    user_prompt += f"Performance: score={output['score_avg']}%, stdev={output['score_std']}.\n\n"
            
            # Add negative examples section  
            if negative_examples:
                user_prompt += "\n=== NEGATIVE EXAMPLES (Poor Performance) ===\n"
                user_prompt += "The following tool descriptions had poor performance:\n\n"
                
                for output in negative_examples:
                    if output['iteration'] == 0:
                        user_prompt += "Original description: "
                    else:
                        user_prompt += f"Iteration #{output['iteration']}, description="
                    user_prompt += f"{output['description']}\n"

                    user_prompt += "Instructions with problems: "
                    for i, ((inst, fn_call, fn_output, ans), result) in enumerate(zip(examples, output['results']), 1):
                        user_prompt += f"{i}. instruction=\"{inst}\", answer=\"{result['answer']}\", errors: "
                        if len(result['errors']) == 0:
                            user_prompt += 'None'
                        else:
                            for j, error in enumerate(result['errors']):
                                user_prompt += (f"({j}) function_call={error['function_name']},"
                                        f" arguments={json.dumps(error['arguments'])}, "
                                        f"error={error['error_msg'][:512]} "
                                    )
                        user_prompt += f". Ground truth: {json.dumps(fn_call)}.\n"

                    user_prompt += f"Performance: score={output['score_avg']}%, stdev={output['score_std']}.\n\n"

            user_prompt += f'''
            Now your task is to critique the descriptions by comparing positive and negative examples. A good description maximizes the score, minimizes the stdev, and helps the assistant correctly use the function without errors. In your analysis:
            
            (1) POSITIVE PATTERN ANALYSIS: Identify what makes the high-performing descriptions (>{performance_threshold}%) successful. What specific phrases, structures, or information do they contain that help the assistant use the function correctly?
            
            (2) NEGATIVE PATTERN ANALYSIS: Identify what causes low-performing descriptions to fail. What specific errors does the assistant make, and what aspects of these descriptions lead to confusion or incorrect function calls?
            
            (3) CONTRAST AND RECOMMENDATIONS: Compare positive vs negative patterns. What are the key differences? What specific improvements would transform a negative example into a positive one?
            
            Your analysis should be less than 500 characters long, do not violate.
            '''

        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)

        def verify_output(output):
            return {'analysis': output.strip()}
        return get_rits_response(
            self.config['eval_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            include_stop_sequence=False, 
            # stop_sequences=['<|eot_id|>', '<|end_of_text|>', '<|eom_id|>'], 
            verbose=self.config['verbose']
        )

    def critique_all_descriptions(
        self,
        tool: Dict[str, Any],
        examples: Optional[List[Tuple[str, Any, str, str]]] = None,
        prev_outputs: Optional[List[Dict[str, List[Any]]]] = None,
    ):
        function_name = tool['name']
        doc_str = json.dumps(tool, ensure_ascii=False)
        user_prompt = f'''
        You are given a function {function_name} with the following documentation, which includes the functionality description, required parameters, code snippets for API calls, etc.

        Documentation:
        {doc_str}
        '''

        if len(examples) > 0 and prev_outputs is not None and len(prev_outputs) > 0:
            # Separate positive and negative examples based on performance threshold
            positive_examples = examples["examples"]
            negative_examples = examples["neg_examples"]
            
            # Add positive examples section
            if positive_examples:
                user_prompt += "\n=== POSITIVE EXAMPLES (Good Performance) ===\n"
                user_prompt += "The following examples achieved good performance:\n\n"
                
                for i, (inst, fn_call, fn_output, ans) in enumerate(positive_examples, 1):
                    user_prompt += f"{i}. instruction=\"{inst}\", Ground truth: {json.dumps(fn_call)}.\n"
                    if len(fn_output) > 256:
                        fn_output = fn_output[:256]
                        user_prompt += f"Example response of the function: {fn_output}, etc"
                    else:
                        user_prompt += f"Response of the function: {fn_output}"
                        
            # Add negative examples section  
            if negative_examples:
                user_prompt += "\n=== NEGATIVE EXAMPLES (Poor Performance) ===\n"
                user_prompt += "The following tool descriptions had poor performance:\n\n"
                
                for i, (inst, fn_call, fn_output, ans) in enumerate(negative_examples, 1):
                    user_prompt += f"{i}. instruction=\"{inst}\", The "
                    user_prompt += f"function call system generated: {json.dumps(fn_call)}."
                    if len(fn_output) > 256:
                        fn_output = fn_output[:256]
                        user_prompt += f"Example response of the function: {fn_output}"
                    else:
                        user_prompt += f"Response of the function: {fn_output}"

            user_prompt += f'''
            Now your task is to critique the descriptions by comparing positive and negative examples. In your analysis:
            
            (1) POSITIVE PATTERN ANALYSIS: Identify patterns in successful cases. What specific phrases, structures, or information do they contain that help the assistant use the function correctly?
            
            (2) NEGATIVE PATTERN ANALYSIS: Identify what causes un-successful cases. What specific errors does the assistant make, and what aspects of these descriptions lead to confusion or incorrect function calls?
            
            (3) CONTRAST AND RECOMMENDATIONS: Compare positive vs negative patterns. What are the key differences? Analyze carefully to uncover any unspecified constrains or limitations?
            
            Your analysis should be less than 500 characters long, do not violate.
            '''

        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)

        def verify_output(output):
            return {'analysis': output.strip()}
        return get_rits_response(
            self.config['eval_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            include_stop_sequence=False, 
            # stop_sequences=['<|eot_id|>', '<|end_of_text|>', '<|eom_id|>'], 
            verbose=self.config['verbose']
        )

    def generate_description_from_documentation(
        self,
        tool: Dict[str, Any],
        examples: Optional[List[Tuple[str, Any, str]]] = None,
        prev_outputs: Optional[List[Dict[str, List[Any]]]] = None,
    ):
        # td - MOD PROMPT TO ANALYZE NEGATIVE CASE
        pos = examples["examples"]
        neg = examples["neg_examples"]
        tmp = self.critique_descriptions(tool, pos, prev_outputs)
        tmp_contrast = self.critique_all_descriptions(tool, examples, prev_outputs)
        
        analysis = tmp['analysis']
        analysis_contrast = tmp_contrast['analysis']
        function_name = tool['name']
        doc_str = json.dumps(tool, ensure_ascii=False)
        user_prompt = f'''
        You are given an API tool with the following documentation, which includes the functionality description, required parameters, code snippets for API calls, etc.

        Documentation:
        {doc_str}

        '''
        if len(examples) > 0 and prev_outputs is not None and len(prev_outputs) > 0:
            user_prompt += (
                "\nPreviously, the given tool was used in solving instructions "
                "by a tool assistant with the following function descriptions:\n"
            )            
            for output in prev_outputs[::-1][:self.config['num_feedback_steps']][::-1]:
                if output['iteration'] == 0:
                    user_prompt += "Original description: "
                else:
                    user_prompt += f"Iteration #{output['iteration']}, description="
                user_prompt += f"{output['description']}\n"
                user_prompt += "Performance of this description is: "
                user_prompt += f" score={output['score_avg']}%, stdev={output['score_std']}.\n"
            user_prompt += (f'\nFurthermore, an analysis was performed on the '
            f'descriptions for the previous iterations: "{analysis}". An '
            f'analysis was performed on the negative cases for the cons'
            f'trains and ability limits of the function: "{analysis_contrast}"')

            user_prompt += f'''
Your task is to further enhance the description for the function {function_name} to MODIFY THE TOOL DESCRIPTION and PARAMETER DESCRIPTION part, with the objective of maximizing the score, minimizing the stdev, and help the assistant correctly use the function without errors.  

Incorporate the analysis and generate the enhanced descriptions. The enhanced description should focus on what this tool can or cannot do, and add the capability boundaries of the tool, e.g., "returns summaries, not full text", "covers domestic locations only", "supports English language only", etc.

The enhanced description should not be longer than 1000 characters, do not violate this.
'''

        # use openai tool call schema
        desired_desc_schema = json.dumps(
            {'type': '', 
             'name': '', 
             'description': '', 
             'parameters': {'type': '', 
                            'properties': {
                                '<PARAMETER_NAME_0>': {'type': '', 'description': ''}, 
                                '<PARAMETER_NAME_1>': {'type': '', 'description': ''}
                            }, 
                            'required': ['<PARAMETER_NAME>']
                            }
            })
        user_prompt += f'''
**IMPORTANT**: You must preserve the exact JSON schema structure provided below. Only modify the text content - do not change schema structure.

**IMPORTANT**: Since no extra fields can be added, include capability boundaries within the main tool description text. Be explicit about what the function CANNOT do to prevent misuse.

**Required Output Format:**
Return JSON following this exact schema structure (modify only description texts):
{{
    "description": {desired_desc_schema}
}}

**Critical**: Maintain all field names, types, and schema structure. Only enhance the textual detail contents.
'''
        
        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)

        def verify_output(output):
            output_json = parse_json(output, "description")

            if "description" not in output_json:
                raise AssertionError('No "description" found in output')

            output_json["description"] = str(output_json["description"]).strip()
            return output_json

        return get_rits_response(
            self.config['gen_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            include_stop_sequence=False, 
            stop_sequences=['<|eot_id|>', '<|end_of_text|>', '<|eom_id|>'], 
            verbose=self.config['verbose']
        )

    def load_examples(self, examples_dir, function_name, max_num_examples=3):
        examples_path = os.path.join(examples_dir, f'{function_name}.json')
        logger.info(f"Trying to load examples from {examples_path}")
        with open(examples_path, 'r', encoding='utf-8') as f:
            all_outputs = json.load(f)
        if all_outputs is None:
            raise RuntimeError

        selected_examples = []
        for node_history in all_outputs:
            for step_output in node_history[::-1]:
                fn_call = step_output['fn_call']
                fn_output = step_output['tool_results']
                inst = step_output['instructions'][-1]
                ans = step_output['answers'][-1]
                if 'scores' in step_output.keys():
                    score = step_output['scores'][-1]
                    if score >= 3. and isinstance(inst, str) and isinstance(ans, str): # td: SCORE THRESHOLD
                        selected_examples.append((inst.strip(), fn_call, fn_output, ans.strip()))
                        break
                else:
                    pass

        return selected_examples[:max_num_examples]
    
    def get_negative_examples(self, function_name):
        examples_path = self.config["neg_ex_input_path"]
        max_num_examples = self.config['num_examples_for_desc']

        if os.path.exists(examples_path):
            # load from provided path
            with open(examples_path, 'r', encoding='utf-8') as f:
                all_outputs = json.load(f)
        else:
            # if not found, fallback to load from self play examples
            logger.warning(f"NO NEGATIVE FILE FOUND at {examples_path}, FALLBACK TO LOAD GENERATED EXAMPLES")
            examples_path = os.path.join(self.config['examples_dir'], f'{function_name}.json')
            with open(examples_path, 'r', encoding='utf-8') as f:
                all_outputs = json.load(f)
        if all_outputs is None:
            raise RuntimeError

        selected_examples = []
        for node_history in all_outputs:
            for step_output in node_history[::-1]:
                # check all variables exist
                if not all(k in step_output for k in ('instructions', 'fn_call', 'tool_results', 'answers')):
                    continue
                fn_call = step_output['fn_call']
                fn_output = step_output['tool_results']
                inst = step_output['instructions'][-1]
                ans = step_output['answers'][-1]
                if "scores" in step_output:
                    score = step_output['scores'][-1]
                    if 1. <= score < 3. and isinstance(inst, str) and isinstance(ans, str): 
                        #  SCORE THRESHOLD
                        selected_examples.append((inst.strip(), fn_call, fn_output, ans.strip()))
                else:
                    selected_examples.append((inst.strip(), fn_call, fn_output, ans.strip()))
        return selected_examples[:max_num_examples]
    
    def get_original_description(self, tool: Dict[str, Any]):
        description = tool['description']
        indicator = 'The description of this function is: "'
        found = description.find(indicator)
        description = description[found + len(indicator): -1] if found != -1 else description
        return description

    def get_examples(self, tool: Dict[str, Any]):
        function_name = tool['name']
        examples = None
        if self.config['examples_dir'] is not None:
            examples = self.load_examples(
                self.config['examples_dir'], 
                function_name,
                self.config['num_examples_for_desc'],
            )
        logger.info(f"{len(examples)} Examples loaded for tool: {function_name}: {examples}")
        return examples
