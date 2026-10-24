# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import copy
import json
import os
from typing import Any, Dict, List, Optional, Union

from openjiuwen.agent_evolving.optimizer.tool.utils.base_method import BaseMethod
from openjiuwen.agent_evolving.optimizer.tool.utils.format import format_prompt_llama, parse_json
from openjiuwen.agent_evolving.optimizer.tool.utils.rits import get_rits_response
from openjiuwen.agent_evolving.optimizer.tool.utils.sensitive_data import (
    argument_dict, credential_values, is_credential_name, redact,
)
from openjiuwen.core.common.logging import logger


class APICallToExampleMethod(BaseMethod):
    def __init__(
        self, 
        config: Dict[str, Union[str, int, bool]], 
        api_call_fn=None,
        eval_fn=None,
        api_keys=None,
        non_opt_params=None,
    ):
        super().__init__(config)
        if api_keys and not isinstance(api_keys, dict):
            raise TypeError("api_keys must map credential argument names to values")
        if api_keys and any(not is_credential_name(name) for name in api_keys):
            raise ValueError("api_keys contains a non-credential argument name")
        self.run_tool_with_api_call = api_call_fn
        self.eval_fn = eval_fn
        self.api_keys = api_keys
        self.non_opt_params = [] if non_opt_params is None else non_opt_params

    def step(
        self, 
        tool: Dict[str, Any], 
        prev_outputs: Optional[List[Dict]] = None,
        it: int = 0,
        **kwargs,
    ):
        logger.info("Inside method, trying to step")
        prev_outputs = copy.copy(prev_outputs) if prev_outputs is not None else []
        tool_for_opt = copy.deepcopy(tool)
        
        # 1. Rejection sampling:
        # for initial loop, generate candidate api call, run tool, and determine
        # if there are any errors
        for _ in range(self.config['num_init_loop']):
            fn_call = self.generate_api_call_from_description(tool_for_opt, num_gen=1, prev_output=prev_outputs)
            logger.info("API call generation completed")
            if not isinstance(fn_call, dict):
                raise ValueError("API call generation did not return a function call")
            arguments = argument_dict(fn_call)
            if arguments is None:
                raise ValueError("API call arguments must be a JSON object")
            fn_call["arguments"] = arguments
            secrets = credential_values(fn_call, self.api_keys, tool_for_opt)
            safe_fn_call = redact(fn_call, secrets)
            safe_tool = redact(tool_for_opt, secrets)
            safe_description = self.get_original_description(safe_tool)

            tool_res, status_code = self.run_tool_with_api_call(tool_for_opt, fn_call)
            safe_tool_res = redact(tool_res, secrets)
            outputs = {
                'fn_call': safe_fn_call,
                'tool_results': safe_tool_res,
                'status_code': status_code,
                'score': status_code,
            }
            logger.info("Run tool with api call completed")

            api_analysis = self.critique_api_call(safe_tool, safe_fn_call, safe_tool_res)
            api_analysis = redact(api_analysis, secrets)
            logger.info("critique_api_call finished")
            if api_analysis['err_code'] == -1:
                outputs['status_code'] = -1
                outputs['score'] = -1
                outputs['api_reflection'] = api_analysis['analysis']
                prev_outputs.append(outputs)
                if self.verbose:
                    logger.info("API call rejected by critique")
                continue
            break
        
        # to do Stop search if have error at first trial
        # if outputs['status_code'] == -1:
        #     return outputs, "", -1

        # 2. Q/A generation and refinement:
        # For n_refine steps, generate question/answer from valid api call
        # Evaluate and update by batch self-reflection
        insts, scores, analyses, answers, refls = [], [], [], [], []
        inst_output = None
        for n_refine in range(self.config['num_refine_steps']):
            inst = redact(self.generate_instruction_from_api_call(
                safe_tool, safe_fn_call, safe_tool_res, inst_output), secrets)
            ans = redact(self.produce_answer_from_api_call(
                inst, json.dumps(safe_tool), safe_tool_res), secrets)
            inst_eval = redact(self.critique_instruction(
                safe_tool, inst, safe_fn_call, safe_tool_res, ans), secrets)

            insts.append(inst)
            answers.append(ans)
            scores.append(inst_eval['score'])
            analyses.append(inst_eval['analysis'])

            batch_refl = self.batch_reflection_with_scores(
                safe_tool,
                safe_fn_call,
                insts[-self.config['num_feedback_steps']:],
                scores[-self.config['num_feedback_steps']:],
                analyses[-self.config['num_feedback_steps']:],
            ).strip()
            batch_refl = redact(batch_refl, secrets)
            refls.append(batch_refl)

            inst_output = {
                'instructions': insts[-self.config['num_feedback_steps']:],
                'scores': scores[-self.config['num_feedback_steps']:],
                'batch_reflection': batch_refl,
            }
            if inst_eval['score'] == 3:
                break

        # 3. Evaluate newly generated example with downstream LLM -- for
        # selecting difficult examples
        if self.config['score_eval_weight'] > 0.:
            logger.info("Eval step: Using eval fn")
            if isinstance(insts[-1], str) and isinstance(answers[-1], str):
                examples = [(insts[-1].strip(), safe_fn_call, safe_tool_res, answers[-1].strip())]
                credential_args = {
                    name: value for name, value in arguments.items()
                    if is_credential_name(name)
                }
                if secrets and not (
                    credential_args and getattr(self.eval_fn, 'supports_execution_credentials', False)
                ):
                    # Unknown evaluators may persist or log the call; fail closed.
                    eval_score = 1.
                    outputs['eval_skipped_reason'] = 'Evaluator does not support safe credential injection'
                    logger.info('Optional scoring skipped: evaluator lacks safe credential injection')
                else:
                    eval_kwargs = {'runs': 1}
                    if secrets and credential_args:
                        eval_kwargs['execution_credentials'] = credential_args
                    eval_res = self.eval_fn(
                        safe_tool, safe_description, examples, **eval_kwargs
                    )
                    eval_score = eval_res['score_avg'] / 100.
            else:
                eval_score = 1.
        else:
            logger.info("Eval step: hard coded eval_score as score_eval_weight=0 ")
            eval_score = 1.
        final_score = scores[-1] + self.config['score_eval_weight'] * (1. - eval_score)

        outputs['answers'] = answers
        outputs['instructions'] = insts
        outputs['scores'] = scores
        outputs['analyses'] = analyses
        outputs['batch_reflections'] = refls
        outputs['score'] = final_score
        return outputs, insts, outputs['score']

    def generate_api_call_from_description(
        self, tool: Dict[str, Any], 
        example_calls: Optional[List[str]] = None, 
        num_gen: int = 1,
        prev_output: Optional[Dict[str, List[Any]]] = None,
    ) -> List[Dict[str, Any]]:
        function_name = tool['name']
        tool = redact(tool, credential_values(tool, self.api_keys))
        doc_str = json.dumps(tool, ensure_ascii=False)
        user_prompt = f'''A tool is an API. 
You are given an API tool with the following 
documentation, which includes the functionality 
description, required parameters, code snippets for API calls, etc.

Documentation:
{doc_str}
'''
        if example_calls is not None and len(example_calls) > 0:
            example_calls = redact(example_calls, credential_values(example_calls, self.api_keys))
            user_prompt += f'''
Example use cases for this API tool are: 
{os.linesep.join(f'"{api_call}"' for api_call in example_calls)}

'''
        if self.api_keys:
            user_prompt += (
                "For credential parameters, use a placeholder. Credentials are "
                "supplied by the execution layer; do not invent them.\n\n"
            )
        user_prompt += f'''Your task is to write {num_gen} example API call 
for the given API tool given its purpose and parameters list. 
The API call you produced will be executed as function call later and 
return result if correct, or error if you provide incorrect syntax, 
format, or parameters. Given the documentation and description, think 
of possible example API calls and produce those that are likely to be 
correctly executed. Think of parameter values that are likely API calls 
that people use in the real world and be the intension to find out 
the api's capabilities. The goal is to generate realistic, slightly 
edge-case API calls that are valid, executable, and reveal subtle 
limits in the system (e.g., language-restricted fields, domestic-only 
locations, silent defaults, etc.). The generated API call MUST be 
executable and real. Parameter values must be filled in and not 
placeholding text, except credential parameters which must use a placeholder.
You must include the required parameters,
and optionally give parameters that are labeled as "optional 
parameters". Do not hallucinate and produce parameters that 
are not under "required" or "optional". Produce diverse 
parameter values if you are asked to generate multiple API calls, 
but be factual and do not use fake parameters. 
        
You can only use the given function {function_name} and not anything else. Create an API call that include the function name, and the parameters to be input to the API. Include all the required and optional parameters in a single dictionary without separating them. Do not include the URL or other irrelevant information. The output should be in the following JSON format that represents a function call:
{{
    "name": "{function_name}",    
    "arguments": {{
        "parameter_1": <param_value_1>,
        "parameter_2": <param_value_2>
    }}
}}

You must strictly follow the output format, including "name", "arguments", and parameters.\n'''

        if prev_output is not None and len(prev_output) > 0:
            prev_output = redact(prev_output, credential_values(prev_output, self.api_keys))
            user_prompt += 'Previously you generated the following API calls for this '
            user_prompt += f'function {function_name}, which where then executed and critiqued:\n'
            for i, output in enumerate(prev_output, 1):
                if 'api_reflection' in output:
                    user_prompt += (f'{i}. fn_call="{output["fn_call"]}" '
                                    f'fn_output="{json.dumps(output["tool_results"])[:512]}" '
                                    f'status={output["status_code"]} reflection="This is an '
                                    f'example of a bad function call. Here is your reflect'
                                    f'ion: {output["api_reflection"]}"\n')
                else:
                    user_prompt += (f'{i}. fn_call="{output["fn_call"]}" '
                    f'fn_output="{json.dumps(output["tool_results"])[:512]}" '
                    f'status={output["status_code"]} reflection="This is an '
                    f'example of a good and reasonable function call. You '
                    f'should generate a function call that differs from thi'
                    f's if possible; do not generate the same function ca'
                    f'll unless there are no parameters for this function."\n')

            user_prompt += 'You should improve your response based on these reflections.\n\n'

        user_prompt += "Do not output anything other than the JSON output. Now you can begin your task."
        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)

        def verify_output(output):
            fn = parse_json(output)

            if not isinstance(fn, dict):
                raise ValueError("Output must be a dict.")

            if "name" not in fn:
                raise ValueError('incorrect output format, "name" required for function')

            if "arguments" not in fn:
                raise ValueError(
                    f'incorrect output format, "arguments" required for function {fn.get("name")}.'
                )

            if fn.get("name") != function_name:
                raise ValueError(
                    f"Output function '{fn.get('name')}' is inconsistent with the given function "
                    f"'{function_name}'. You must only use the given function {function_name}!"
                )

            return fn

        logger.info(f"Sending request to generate tool use examples")
        result = get_rits_response(
            self.config['gen_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            include_stop_sequence=False, 
            stop_sequences=[
                '<|eot_id|>', 
                '<|end_of_text|>', 
                '<|eom_id|>'
            ], 
            verbose=self.verbose
        )
        if self.api_keys:
            arguments = argument_dict(result)
            if arguments is None:
                raise ValueError("API call arguments must be a JSON object")
            result['arguments'] = arguments
            for name, value in self.api_keys.items():
                arguments[name] = value
        return result

    def critique_api_call(self, tool: Dict[str, Any], fn_call: Dict[str, Any], fn_response: str):
        function_name = tool['name']
        doc_str = json.dumps(tool, ensure_ascii=False)
        user_prompt = f'''
You are given an API tool with the following documentation, which includes the functionality description, required parameters, code snippets for API calls, etc.

Documentation:
{doc_str}

Previously you were asked to write an example API 
call for the function {function_name} given its purpose 
and parameters list, and you generated the following 
function call: {json.dumps(fn_call)}. '''
        
        if len(fn_response) > 2048:
            fn_response = fn_response[:2048]
            user_prompt += f'''The function call you produced 
was later executed and returned the following result. 
Example of function result: "{fn_response}", etc.'''
        else:
            user_prompt += f'''The function call 
you produced was later executed and returned the 
following result: "{fn_response}".'''
        
        user_prompt += '''Your task is to analyze the response and check if there are any errors. 
        1. If there are no errors and everything looks reasonable, give an err_code of 0, and don't provide analysis.
        2. If there is an error, give an err_code of -1. Then in your analysis, describe and analyze in detail why the error occurred based on the error message. Then, based on your analysis, give detailed suggestions to improve the function call so that no errors will be produced. You must give detailed analysis and suggestions, do not simply repeat the error message. The analysis and suggestions should be in the "analysis" field in the output.

        Note that even if the "error" field in the result is empty, the "response" field may contain an error when using the function call. If this is the case you must treat this as an error and analyze the failure. The response field may also be in HTML format. Keep your analysis to less than 200 characters. 

        Your output should be in the following JSON format:
        {{
            "analysis": your analysis and suggestions,
            "err_code": error code (-1 for error, 0 for correct)
        }}

You can begin your task now.'''
        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)
        
        def verify_output(output):
            output_json = parse_json(output)

            if "analysis" not in output_json:
                raise ValueError('No "analysis" found in output')

            if "err_code" not in output_json:
                raise ValueError('No "err_code" found in output')

            output_json["analysis"] = str(output_json.get("analysis", "")).strip()

            try:
                output_json["err_code"] = int(output_json.get("err_code"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f'Invalid "err_code": {output_json.get("err_code")}') from exc

            return output_json

        logger.info(f"Sending request to critique api")
        
        response = get_rits_response(
            self.config['eval_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            stop_sequences=['<|eot_id|>', '<|end_of_text|>', '<|eom_id|>'], 
            verbose=self.verbose
        )
        return response

    def generate_instruction_from_api_call(
            self, 
            tool: Dict[str, Any], 
            fn_call: Dict[str, Any], 
            fn_response: str,
            prev_output: Optional[Dict[str, List[Any]]] = None,
    ):
        function_name = tool['name']
        doc_str = json.dumps(tool, ensure_ascii=False)
        user_prompt = f'''
        You are given an API tool with the following documentation, which includes the functionality description, required parameters, code snippets for API calls, etc.

        Documentation:
        {doc_str}

        For the function {function_name}, you are given the following function call: {json.dumps(fn_call)}, and executing the function call returned the following result: {fn_response}. Your task is to generate a user instruction in natural language that requires the given function call to be completed. Here are some guidelines to follow:
        1. The instruction must be a scenario or problem that cannot be solved without calling the given function {function_name}. This is your main objective.
        2. The problem can be complex and require other tools or APIs, but you must include the given API function. 
        3. You should not directly or explicitly ask for the function to be called; the problem itself must inherently be solved by the function.
        4. Based on the function, function call, its parameters, parameter values, and function execution responses, you should produce a real and reasonable instruction. 
        5. Use non-sensitive parameter values to create the response. Never include credentials or their placeholders in the user instruction.
        6. You must NOT include specific function calls in your response. You should not explicitly show the function names. You should also never explicitly name the parameter names in your response. You should not show any variable names.
        7. Your response has to be in natural language. Do not show any variables, function calls, or code. 
        8. The instruction must not be longer than 3 sentences. It should not be longer than 300 letters. Be succinct and do not spend too much on describing irrelevant background. 
        9. You should respond in the user's first-person perspective.
        10. You are a human user. You are asking a question or giving an instruction. Do not answer in the perspective of an AI assistant. Remember, the user does not know about the API function and thus cannot ask to call the function.
        11. Remember, you are asking a question, so do not answer your own question in the response. Your goal is to give a querying instruction or question, not producing answers or function calls.
        12. Be creative and think about what users will ask in real-world scenarios.
        '''

        user_prompt += '''
        Your output should be in the following JSON format:
        {{
            "instruction": generated instruction
        }}
        '''
        if prev_output is not None:
            
            formatted_lines = []

            for i, (inst, score) in enumerate(
                zip(prev_output["instructions"], prev_output["scores"]),
                1,
            ):
                formatted_lines.append(f'{i}. instruction="{inst}" score={score}')

            formatted = os.linesep.join(formatted_lines)
            
            user_prompt += f'''Previously you generated the following 
instructions for this function call, which were rated and analyzed:
            {formatted}
            Based on these ratings, you are given the following analysis: {prev_output['batch_reflection']}
            You should improve your instructions based on these suggestions. '''

        user_prompt += "You must strictly follow the output format. Now you can begin your task."
        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)

        def verify_output(output):
            output_json = parse_json(output, "instruction")

            if "instruction" not in output_json:
                raise ValueError('No "instruction" found in output')

            return str(output_json.get("instruction", "")).strip()
        
        return get_rits_response(
            self.config['eval_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            stop_sequences=['<|eot_id|>', '<|end_of_text|>', '<|eom_id|>'], 
            verbose=self.verbose
        )

    def critique_instruction(
            self, 
            tool: Dict[str, Any], 
            instruction: str, 
            fn_call: Dict[str, Any], 
            fn_response: str, 
            answer: str
    ):
        function_name = tool['name']
        doc_str = json.dumps(tool, ensure_ascii=False)
        user_prompt = f'''
You are given an instruction "{instruction}", 
function call "{json.dumps(fn_call)}" and an answer "{answer}", 
your task is to give a `score` based on the following rules:
1. You must return 1 if any of the following 
conditions is met (for instruction only): 
    (1) instruction is empty, nonsense, or not in natural language; or 
    (2) instruction is explicitly including function 
    calls or asking for function calls or contains function names; or 
    (3) instruction includes exact function parameter names; or 
    (4) instruction includes code or variable assignment; or 
    (5) instruction is longer than 3 sentences or 300 letters; or 
    (6) instruction does not include a question, query, request, or 
    problem to be solved; or 
    (7) instruction is not in first-person perspective (not using "I" as pronoun), 
    or is in the perspective of an AI assistant instead of a user; or 
    (8) any non-sensitive parameter value in the function call is not present in the instruction; or
    (9) the instruction includes a credential or credential placeholder'''

        user_prompt += f'''
. An instruction that satisfies any of these conditions 
is a bad instruction and should be scored a 1.
        
2. If the answer is a sorry message, not a positive/straight 
response for the given instruction, or mentions any 
errors (API error, invalid parameter error, ..., etc.), 
mentions cannot use API or cannot respond, return 1. Any 
errors must be scored a 1, no exceptions.

3. If the answer is a positive/straight response 
for the given instruction, you have to further check.
    3.1 If the answer is not sufficient to 
    determine whether they solve the instruction or not, return 2.
    3.2 If you are confident that the answer 
    is sufficient to determine whether the solve the 
    instruction or not, return 3 if solvable or 1 if unsolvable.

Finally, organize your output in the following JSON format:

{{
    "analysis": your reasoning,
    "score": score
}}

You must strictly follow the output format. Your reasoning 
should not be longer than 200 words. You must also strictly 
follow the scoring rules, and remember that the score must 
be a number between 1 and 3. You can begin your task now.'''
        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)
        
        def verify_output(output):
            output_json = parse_json(output, "analysis")

            if not isinstance(output_json, dict):
                raise ValueError(
                    "incorrect output format (not a dict), " \
                    "you have to output Dict containing your analysis and rating"
                )

            if "analysis" not in output_json:
                raise ValueError('incorrect output format, "analysis" required.')

            if "score" not in output_json:
                raise ValueError('incorrect output format, "score" required.')

            output_json["analysis"] = str(output_json.get("analysis", "")).strip()

            try:
                output_json["score"] = int(output_json.get("score"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f'Invalid "score": {output_json.get("score")}') from exc

            return output_json
        
        return get_rits_response(
            self.config['eval_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            stop_sequences=['<|eot_id|>', '<|end_of_text|>', '<|eom_id|>'], 
            verbose=self.verbose
        )

    def batch_reflection_with_scores(
            self, 
            tool: Dict[str, Any], 
            fn_call: Dict[str, Any], 
            instructions: List[str], 
            scores: List[float], 
            analyses: Optional[List[str]] = None
    ):
        function_name = tool['name']
        doc_str = json.dumps(tool, ensure_ascii=False)
        lines = []

        for i, (inst, score, ana) in enumerate(
            zip(instructions, scores, analyses),
            1,
        ):
            line = f'{i}. instruction="{inst}" score={score} analysis="{ana}"'
            lines.append(line)

        formatted = os.linesep.join(lines)
        user_prompt = f'''You are given an API tool with the 
following documentation, which includes the functionality 
description, required parameters, code snippets for API calls, etc.

Documentation:
{doc_str}

Previously, given the function call {json.dumps(fn_call)}, 
you were asked to generate example instructions that require 
the use of the function {function_name} to complete. 
The example instructions generated by you were then scored 
by an expert on whether the instructions can be fulfilled 
using the given API function. Scores are in a scale 
between 1 (lowest) and 3 (highest). Below are the 
generated instructions, scores, and analyses:

{formatted}

Task:
1. Firstly, identify and contrast the patterns of 
instructions and function calls that have achieved 
good scores with those that have not. If there are 
no bad scores, only summarize the patterns of the good ones.
2. Next, specify the suggestions that can lead to 
improved performance for the generated instructions 
and function calls with bad scores. You should 
focus on capturing the high-level pattern of the 
examples relevant to the API documentation. 
Note that both the function and the function 
call cannot be changed, and focus your 
suggestions on how to improve the example 
instructions, including deciding what information 
to use from parameters of the function call.

Keep your analysis and suggestions to less 
than 500 characters. You can now start your task.'''
        
        prompt = format_prompt_llama(system_prompt="", user_prompt=user_prompt)
        
        def verify_output(output):
            return output.strip()

        return get_rits_response(
            self.config['eval_model_id'], 
            prompt, 
            self.config['llm_api_key'], 
            verify_output, 
            max_attempts=15, 
            stop_sequences=['<|eot_id|>', '<|end_of_text|>', '<|eom_id|>'], 
            verbose=self.verbose
        )

    def get_original_description(self, tool: Dict[str, Any]):
        description = tool['description']
        indicator = 'The description of this function is: "'
        found = description.find(indicator)
        description = description[found + len(indicator): -1] if found != -1 else description
        return description
