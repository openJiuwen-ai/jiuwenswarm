# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import json
from typing import Any, Dict, List, Tuple
import anyio

import numpy as np
# from openai import OpenAI

from openjiuwen.agent_evolving.optimizer.tool.utils.rits import rits_response
from openjiuwen.core.common.logging import logger
from openjiuwen.core.foundation.llm import (
    ModelClientConfig,
    ModelRequestConfig
)
from openjiuwen.core.foundation.llm import OpenAIModelClient


class SimpleEval:
    """
    Improved evaluation wrapper that generates function calls based on instructions
    and evaluates both function call accuracy and output effectiveness.
    """
    
    def __init__(
            self, 
            api_wrapper=None, 
            config: Dict = None, 
            fn_call_weight=0.4, 
            output_effectiveness_weight=0.6
        ):
        """
        Initialize the evaluator.
        
        Args:
            api_wrapper: The API wrapper to use for function execution
            llm_agent: LLM agent with tool calling capability (e.g., OpenAI client with function calling)
            evaluation_llm: LLM client for evaluating output effectiveness (can be the same as llm_agent)
            fn_call_weight: Weight for function call accuracy score (0-1)
            output_effectiveness_weight: Weight for output effectiveness score (0-1)
        """
        self.api_wrapper = api_wrapper
        self.fn_call_weight = fn_call_weight
        self.output_effectiveness_weight = output_effectiveness_weight
        self.config = config
        
        if abs(fn_call_weight + output_effectiveness_weight - 1.0) > 1e-6:
            raise ValueError("fn_call_weight and output_effectiveness_weight must sum to 1.0")
    
    def __call__(
        self,
        tool: Dict[str, Any],
        description: str,
        examples: List[Tuple[str, Any, str, str]],
        runs: int = 1,
    ):
        """
        Evaluate a tool with given examples.
        
        Args:
            tool: Tool definition dict with 'function' key
            description: Description of the function
            examples: List of (instruction, fn_call, fn_output, answer) tuples
            runs: Number of evaluation runs (for averaging)
            
        Returns:
            Dict with 'score_avg', 'score_std', 'fn_call_accuracy', 'output_effectiveness', and 'results'
        """
        all_scores = []
        all_fn_call_scores = []
        all_output_scores = []
        all_results = []
        
        for run in range(runs):
            run_results = []
            total_fn_call_score = 0.0
            total_output_score = 0.0
            total_count = len(examples)
            
            for i, (instruction, expected_fn_call, fn_output, answer) in enumerate(examples):
                result = self._evaluate_single_example(
                    {
                        "tool": tool,
                        "description": description,
                        "instruction": instruction,
                        "expected_fn_call": expected_fn_call,
                        "expected_output": fn_output,
                        "answer": answer,
                        "example_id": i,
                    }
                )
                run_results.append(result)
                total_fn_call_score += result['fn_call_score']
                total_output_score += result['output_effectiveness_score']
            
            # Calculate average scores for this run
            avg_fn_call_score = total_fn_call_score / total_count if total_count > 0 else 0.0
            avg_output_score = total_output_score / total_count if total_count > 0 else 0.0
            
            # Calculate weighted total score
            total_score = (self.fn_call_weight * avg_fn_call_score + 
                          self.output_effectiveness_weight * avg_output_score)
            
            all_scores.append(total_score)
            all_fn_call_scores.append(avg_fn_call_score)
            all_output_scores.append(avg_output_score)
            all_results.append(run_results)
        
        return {
            'score_avg': np.mean(all_scores) * 100.0,
            'score_std': np.std(all_scores) * 100.0,
            'fn_call_accuracy': np.mean(all_fn_call_scores) * 100.0,
            'output_effectiveness': np.mean(all_output_scores) * 100.0,
            'results': all_results[0] if runs == 1 else all_results,
        }
    
    def _evaluate_single_example(self, example: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate a single example.
        
        Returns:
            Dict with evaluation results in the format expected by the original system
        """
        tool = example["tool"]
        description = example["description"]
        instruction = example["instruction"]
        expected_fn_call = example["expected_fn_call"]
        expected_output = example["expected_output"]
        answer = example["answer"]
        example_id = example["example_id"]
        try:
            # Step 1: Generate function call based on instruction and tool description
            generated_fn_call = self._generate_function_call(tool, description, instruction)
            
            # Step 2: Evaluate function call accuracy
            fn_call_score = self._evaluate_function_call_accuracy(generated_fn_call, expected_fn_call)
            
            # Step 3: Execute the generated function call
            execution_result = None
            execution_error = None
            errors = []
            
            if self.api_wrapper:
                try:
                    actual_output, status_code = self.api_wrapper(tool, generated_fn_call)
                    if status_code == 0:
                        execution_result = json.loads(actual_output)
                    else:
                        execution_error = json.loads(actual_output)
                        # Format error according to expected structure
                        errors.append({
                            'function_name': generated_fn_call.get(
                                'name', 
                                tool.get('name', 'unknown')
                            ),
                            'arguments': generated_fn_call.get('arguments', {}),
                            'error_msg': str(execution_error)
                        })
                except Exception as e:
                    execution_error = {"error": str(e)}
                    if generated_fn_call:
                        function_name = generated_fn_call.get(
                            "name",
                            tool.get("name", "unknown"),
                        )
                        arguments = generated_fn_call.get("arguments", {})
                    else:
                        function_name = tool.get("name", "unknown")
                        arguments = {}

                    errors.append(
                        {
                            "function_name": function_name,
                            "arguments": arguments,
                            "error_msg": str(e),
                        }
                    )
            else:
                logger.error("Missing required input: api_wrapper")
                error_msg = "Missing required input: api_wrapper"
                errors.append({
                    'function_name': tool.get('name', 'unknown'),
                    'arguments': {},
                    'error_msg': error_msg
                })
                raise ValueError(error_msg)
            
            # Step 4: Evaluate output effectiveness
            output_effectiveness_score = self._evaluate_output_effectiveness(
                instruction, execution_result, execution_error, answer
            )
            
            # Calculate weighted score
            weighted_score = (self.fn_call_weight * fn_call_score + 
                            self.output_effectiveness_weight * output_effectiveness_score)
            
            # Format according to expected structure for output['results']
            return {
                'instruction': instruction,
                'expected_fn_call': expected_fn_call,
                'generated_fn_call': generated_fn_call,
                'fn_call_score': fn_call_score,
                'execution_result': execution_result,
                'execution_error': execution_error,
                'output_effectiveness_score': output_effectiveness_score,
                'weighted_score': weighted_score,
                'answer': answer,
                'errors': errors
            }
                
        except Exception as e:
            logger.error(f"Error evaluating example {example_id}: {str(e)}")
            # Format error according to expected structure
            return {
                'instruction': instruction,
                'expected_fn_call': expected_fn_call,
                'generated_fn_call': None,
                'fn_call_score': 0.0,
                'execution_result': None,
                'execution_error': {"error": str(e)},
                'output_effectiveness_score': 0.0,
                'weighted_score': 0.0,
                'answer': answer,  # This is what the original format expects in result['answer']
                'errors': [{
                    'function_name': tool.get('name', 'unknown'),
                    'arguments': {},
                    'error_msg': str(e)
                }]
            }

    def _generate_function_call(self, tool: Dict[str, Any], description: str, instruction: str) -> Dict[str, Any]:
        """
        Generate a function call using LLM agent's built-in tool calling capability.
        """
        try:
            # client = OpenAI()
            model_config = ModelRequestConfig(
                model=self.config["eval_model_id"],
            )
            model_client = ModelClientConfig(
                client_provider="OpenAI",
                api_base="https://api.openai.com/v1",
                api_key=self.config['llm_api_key'],
                verify_ssl=False
            )
            client = OpenAIModelClient(
                model_config=model_config,
                model_client_config=model_client
            )
            if "type" not in tool.keys():
                tool["type"] = "function"

            if isinstance(tool.get("description"), str):
                try:
                    desc_json = json.loads(tool["description"])
                    if "function" in desc_json:
                        func = desc_json["function"]
                        tool = {
                            "name": func.get("name", tool["name"]),
                            "type": tool.get("type", "tool"),
                            "description": func.get("description", ""),
                            "parameters": func.get("parameters", {})
                        }
                except json.JSONDecodeError:
                    pass  # description not JSON, keep same


            api_response = anyio.run(
                lambda: client.invoke(
                    messages=[{"role": "user", "content": instruction}],
                    tools=[{"type": "function", "function": tool}],
                )
            )

            fn_args = api_response.tool_calls[0].arguments
            function_name = api_response.tool_calls[0].name
            function_call = {"name": function_name, "arguments": fn_args}

            # try to load str arguments to dict
            if isinstance(function_call, str):
                try:
                    function_call = json.loads(function_call)
                except json.JSONDecodeError:
                    pass
            # Return the first generated call, or handle multiple calls as needed
            if function_call and len(function_call) > 0:
                return function_call
            else:
                # Fallback if no calls generated
                return {
                    "name": function_name,
                    "arguments": {}
                }
                
        except Exception as e:
            logger.error(f"Error generating function call: {str(e)}")
            # Fallback: return basic function call structure
            return {
                "name": function_name,
                "arguments": {}
            }
    
    def _evaluate_function_call_accuracy(
            self, 
            generated_fn_call: Dict[str, Any], 
            expected_fn_call: Dict[str, Any]
        ) -> float:
        """
        Evaluate the accuracy of the generated function call compared to the expected one.
        Returns a score between 0 and 1.
        """
        try:
            score = 0.0
            max_score = 0.0
            
            # Check function name (30% weight)
            max_score += 0.3
            if generated_fn_call.get('name') == expected_fn_call.get('name'):
                score += 0.3
            
            # Check parameters (70% weight)
            gen_params = generated_fn_call.get('arguments', {})
            exp_params = expected_fn_call.get('arguments', {})
            
            # standardize both params to dict
            if isinstance(gen_params, str):
                try: 
                    gen_params = json.loads(gen_params)
                except json.JSONDecodeError:
                    pass
            if isinstance(exp_params, str):
                try: 
                    exp_params = json.loads(exp_params)
                except json.JSONDecodeError:
                    pass

            if not exp_params and not gen_params:
                # Both empty parameters
                score += 0.7
                max_score += 0.7
            elif exp_params:
                param_score = 0.0
                for key, expected_value in exp_params.items():
                    max_score += 0.7 / len(exp_params)
                    if key in gen_params:
                        if self._compare_parameter_values(gen_params[key], expected_value):
                            param_score += 0.7 / len(exp_params)
                
                score += param_score
            else:
                max_score += 0.7
            
            return score / max_score if max_score > 0 else 0.0
            
        except Exception as e:
            logger.error(f"Error evaluating function call accuracy: {str(e)}")
            return 0.0
    
    @staticmethod
    def _compare_parameter_values(actual, expected):
        """
        Compare parameter values with type tolerance.
        """
        # Direct equality
        if actual == expected:
            return True
        
        # Type conversion tolerance
        try:
            # Try numeric comparison
            if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
                return abs(actual - expected) < 1e-6

            # Try string comparison
            if str(actual).strip().lower() == str(expected).strip().lower():
                return True

        except (TypeError, ValueError):
            return False

        return False
    
    def _evaluate_output_effectiveness(
            self, 
            instruction: str, 
            execution_result: Any, 
            execution_error: Any, 
            expected_answer: str
        ) -> float:
        """
        Evaluate how effectively the function call output solves the user's problem using LLM.
        Returns a score between 0 and 1.
        """
        if execution_error:
            return 0.0

        prompt = f"""
Evaluate whether the function execution result effectively solves the user's problem.

User Instruction: {instruction}

Function Execution Result: {json.dumps(execution_result, indent=2)}

Expected Answer/Goal: {expected_answer}

Please evaluate on a scale of 0-100 how well the function execution result addresses the user's instruction and matches the expected answer. Consider:
1. Does the result provide the information requested in the instruction?
2. Is the result accurate and complete?
3. Does it align with the expected answer?

Respond with only a number between 0 and 100. Do not include explanations.
"""
        
        try:
            # Use the evaluation LLM to generate a score 
            response = rits_response(
                model_id=self.config["eval_model_id"], 
                prompt=prompt, 
            )
            score = float(response.strip())
            return min(max(score, 0.0), 100.0) / 100.0
        except Exception as e:
            logger.error(f"Error evaluating output effectiveness: {str(e)}")
            return self._simple_output_comparison(execution_result, expected_answer)
    
    @staticmethod
    def _simple_output_comparison(
        execution_result: Any, 
        expected_answer: str
    ) -> float:
        """
        Simple fallback comparison when LLM evaluation is not available.
        """
        try:
            if execution_result is None:
                return 0.0
            
            result_str = json.dumps(execution_result) if not isinstance(execution_result, str) else execution_result
            
            # Simple string similarity
            if expected_answer.lower().strip() in result_str.lower().strip():
                return 1.0
            elif result_str.lower().strip() in expected_answer.lower().strip():
                return 0.8
            else:
                return 0.3  # Partial credit for having some result
                
        except Exception as e:
            logger.error(f"Error in simple output comparison: {str(e)}")
            return 0.0
