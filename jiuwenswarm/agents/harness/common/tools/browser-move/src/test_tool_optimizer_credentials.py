"""Credential regression tests for the patched tool optimizer."""

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


UTILS = Path(__file__).parent / "openjiuwen_patch_sources/openjiuwen/agent_evolving/optimizer/tool/utils"
PREFIX = "openjiuwen.agent_evolving.optimizer.tool.utils"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CredentialSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fake_modules = {}
        for name in (
            "openjiuwen", "openjiuwen.agent_evolving",
            "openjiuwen.agent_evolving.optimizer",
            "openjiuwen.agent_evolving.optimizer.tool", PREFIX,
            "openjiuwen.core", "openjiuwen.core.common",
            "openjiuwen.core.foundation",
        ):
            module = types.ModuleType(name)
            module.__path__ = []
            fake_modules[name] = module

        base = types.ModuleType(PREFIX + ".base_method")

        class BaseMethod:
            def __init__(self, config):
                self.config = config
                self.verbose = config.get("verbose", False)

        base.BaseMethod = BaseMethod
        fake_modules[base.__name__] = base

        fmt = types.ModuleType(PREFIX + ".format")
        fmt.format_prompt_llama = lambda system_prompt, user_prompt: user_prompt
        fmt.parse_json = json.loads
        fake_modules[fmt.__name__] = fmt

        rits = types.ModuleType(PREFIX + ".rits")
        rits.get_rits_response = lambda *args, **kwargs: None
        rits.rits_response = lambda *args, **kwargs: None
        fake_modules[rits.__name__] = rits

        llm = types.ModuleType("openjiuwen.core.foundation.llm")
        llm.ModelClientConfig = object
        llm.ModelRequestConfig = object
        llm.OpenAIModelClient = object
        fake_modules[llm.__name__] = llm

        logging = types.ModuleType("openjiuwen.core.common.logging")
        logging.logger = types.SimpleNamespace(info=lambda *args: None)
        fake_modules[logging.__name__] = logging

        with patch.dict(sys.modules, fake_modules):
            cls.sensitive = load_module(PREFIX + ".sensitive_data", UTILS / "sensitive_data.py")
            cls.toolcall = load_module("test_toolcall_example_method", UTILS / "toolcall_example_method.py")
            cls.evaluator = load_module("test_customized_eval", UTILS / "customized_eval.py")
            cls.wrapper = load_module("test_customized_api", UTILS / "customized_api.py")

    def test_redacts_nested_fields_json_results_and_authorization_text(self):
        secret = "test-secret-123"
        payload = {
            "arguments": {"apiKey": secret, "nested": [{"accessToken": secret}]},
            "result": json.dumps({"password": secret, "message": "Authorization: Bearer " + secret}),
        }
        safe = self.sensitive.redact(payload, self.sensitive.credential_values(payload))
        self.assertNotIn(secret, json.dumps(safe))
        self.assertEqual(safe["arguments"]["apiKey"], self.sensitive.REDACTED)
        self.assertEqual(payload["arguments"]["apiKey"], secret)

    def test_step_executes_real_call_but_returns_only_safe_examples(self):
        secret = "test-secret-456"
        raw_call = {"name": "lookup", "arguments": {"api_key": secret, "query": "weather"}}
        executed = []
        seen_by_model = []
        config = {"num_init_loop": 1, "num_refine_steps": 1,
                  "num_feedback_steps": 1, "score_eval_weight": 0, "verbose": True}
        method = self.toolcall.APICallToExampleMethod(
            config, api_call_fn=lambda tool, call: (executed.append(call) or json.dumps(
                {"response": {"password": secret}}), 0))
        method.generate_api_call_from_description = lambda *args, **kwargs: raw_call
        method.critique_api_call = lambda tool, call, result: (
            seen_by_model.append((tool, call, result)) or {"err_code": 0, "analysis": ""})
        method.generate_instruction_from_api_call = lambda *args: "Use " + secret
        method.produce_answer_from_api_call = lambda *args: "Answer " + secret
        method.critique_instruction = lambda *args: {"score": 3, "analysis": secret}
        method.batch_reflection_with_scores = lambda *args: secret
        method.get_original_description = lambda tool: tool["description"]

        log_messages = []
        original_logger = self.toolcall.logger
        try:
            self.toolcall.logger = types.SimpleNamespace(info=lambda message: log_messages.append(message))
            result, _, _ = method.step({"name": "lookup", "description": "API key: " + secret})
        finally:
            self.toolcall.logger = original_logger
        self.assertEqual(executed[0]["arguments"]["api_key"], secret)
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, json.dumps(seen_by_model))
        self.assertNotIn(secret, json.dumps(log_messages))

    def test_api_keys_are_injected_after_model_call(self):
        secret = "test-secret-789"
        config = {"gen_model_id": "fake", "llm_api_key": "fake", "verbose": False}
        method = self.toolcall.APICallToExampleMethod(config, api_keys={"api_key": secret})
        prompts = []
        original = self.toolcall.get_rits_response
        try:
            self.toolcall.get_rits_response = lambda model, prompt, *args, **kwargs: (
                prompts.append(prompt) or {"name": "lookup", "arguments": {"api_key": "placeholder"}})
            result = method.generate_api_call_from_description(
                {"name": "lookup", "description": "Use an api_key"})
        finally:
            self.toolcall.get_rits_response = original
        self.assertNotIn(secret, prompts[0])
        self.assertEqual(result["arguments"]["api_key"], secret)

    def test_evaluator_does_not_return_or_reprompt_with_credentials(self):
        secret = "test-secret-eval"
        executed = []
        evaluated = []
        evaluator = self.evaluator.SimpleEval(
            api_wrapper=lambda tool, call: (executed.append(call) or json.dumps(
                {"response": {"access_token": secret}}), 0), config={})
        evaluator._generate_function_call = lambda *args: {
            "name": "lookup", "arguments": {"api_key": secret}}
        evaluator._evaluate_function_call_accuracy = lambda *args: 1.0
        evaluator._evaluate_output_effectiveness = lambda *args: (
            evaluated.append(args) or 1.0)
        result = evaluator._evaluate_single_example({
            "tool": {"name": "lookup", "description": "lookup"},
            "description": "lookup", "instruction": "Search", "answer": "Found",
            "expected_fn_call": {"name": "lookup", "arguments": {"api_key": secret}},
            "expected_output": "Found", "example_id": 0,
        })
        self.assertEqual(executed[0]["arguments"]["api_key"], secret)
        self.assertNotIn(secret, json.dumps(evaluated))
        self.assertNotIn(secret, json.dumps(result))

    def test_evaluator_exception_does_not_return_credential(self):
        secret = "test-secret-error"

        def fail_with_secret(*args):
            raise RuntimeError("Authorization: Bearer " + secret)

        evaluator = self.evaluator.SimpleEval(api_wrapper=fail_with_secret, config={})
        evaluator._generate_function_call = lambda *args: {
            "name": "lookup", "arguments": {"api_key": secret}}
        evaluator._evaluate_function_call_accuracy = lambda *args: 1.0
        evaluator._evaluate_output_effectiveness = lambda *args: 0.0
        result = evaluator._evaluate_single_example({
            "tool": {"name": "lookup", "description": "lookup"},
            "description": "lookup", "instruction": "Search", "answer": "None",
            "expected_fn_call": {"name": "lookup", "arguments": {"api_key": secret}},
            "expected_output": "None", "example_id": 0,
        })
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["execution_error"], {"error": "Tool execution failed"})

    def test_evaluator_injects_credentials_only_for_execution(self):
        secret = "test-secret-inject"
        executed = []
        evaluated = []
        evaluator = self.evaluator.SimpleEval(
            api_wrapper=lambda tool, call: (executed.append(call) or json.dumps(
                {"response": "ok"}), 0), config={})
        evaluator._generate_function_call = lambda *args: {
            "name": "lookup", "arguments": {"api_key": "placeholder"}}
        evaluator._evaluate_function_call_accuracy = lambda *args: 1.0
        evaluator._evaluate_output_effectiveness = lambda *args: (
            evaluated.append(args) or 1.0)
        result = evaluator(
            {"name": "lookup", "description": "lookup"}, "lookup",
            [("Search", {"name": "lookup", "arguments": {"api_key": "[REDACTED]"}},
              "ok", "ok")],
            execution_credentials={"api_key": secret},
        )
        self.assertEqual(executed[0]["arguments"]["api_key"], secret)
        self.assertEqual(result["score_avg"], 100.0)
        self.assertNotIn(secret, json.dumps(result["results"]))
        self.assertNotIn(secret, json.dumps(evaluated))

    def test_step_keeps_optional_scoring_with_safe_evaluator(self):
        secret = "test-secret-scoring"
        eval_calls = []

        def evaluate(*args, **kwargs):
            eval_calls.append((args, kwargs))
            return {"score_avg": 50.0}

        evaluate.supports_execution_credentials = True
        config = {"num_init_loop": 1, "num_refine_steps": 1,
                  "num_feedback_steps": 1, "score_eval_weight": 1.0, "verbose": False}
        method = self.toolcall.APICallToExampleMethod(
            config, api_call_fn=lambda *args: ("ok", 0), eval_fn=evaluate)
        method.generate_api_call_from_description = lambda *args, **kwargs: {
            "name": "lookup", "arguments": {"api_key": secret}}
        method.critique_api_call = lambda *args: {"err_code": 0, "analysis": ""}
        method.generate_instruction_from_api_call = lambda *args: "Search"
        method.produce_answer_from_api_call = lambda *args: "Found"
        method.critique_instruction = lambda *args: {"score": 3, "analysis": ""}
        method.batch_reflection_with_scores = lambda *args: "good"
        method.get_original_description = lambda tool: tool["description"]

        result, _, score = method.step({"name": "lookup", "description": "lookup"})
        self.assertEqual(score, 3.5)
        self.assertEqual(eval_calls[0][1]["execution_credentials"], {"api_key": secret})
        self.assertNotIn(secret, json.dumps(eval_calls[0][0]))
        self.assertNotIn(secret, json.dumps(result))

    def test_active_tool_wrapper_does_not_log_input_or_exception(self):
        secret = "test-secret-wrapper"
        log_messages = []

        def fail(call):
            raise RuntimeError("Authorization: Bearer " + secret)

        wrapper = self.wrapper.SimpleAPIWrapperFromCallable(fail, "lookup", {})
        original_logger = self.wrapper.logger
        try:
            self.wrapper.logger = types.SimpleNamespace(
                info=lambda message: log_messages.append(message),
                error=lambda message: log_messages.append(message),
            )
            result, status = wrapper(
                {"name": "lookup"}, {"name": "lookup", "arguments": {"api_key": secret}})
        finally:
            self.wrapper.logger = original_logger
        self.assertEqual(status, 12)
        self.assertNotIn(secret, result + json.dumps(log_messages))

    def test_pipeline_sanitizes_tool_and_existing_example_file(self):
        secret = "test-secret-file"
        seen_tools = []

        class BeamSearch:
            def __init__(self, **kwargs):
                pass

            def search(self, tool):
                seen_tools.append(tool)
                return [[{"fn_call": {"arguments": {"api_key": secret}},
                          "tool_results": "Authorization: Bearer " + secret}]]

        def module_with(name, attribute, value):
            module = types.ModuleType(name)
            setattr(module, attribute, value)
            return module

        fake_modules = {
            PREFIX + ".sensitive_data": self.sensitive,
            PREFIX + ".beam_search": module_with(PREFIX + ".beam_search", "BeamSearch", BeamSearch),
            PREFIX + ".customized_api": module_with(
                PREFIX + ".customized_api", "SimpleAPIWrapperFromCallable", lambda *args: None),
            PREFIX + ".customized_eval": module_with(
                PREFIX + ".customized_eval", "SimpleEval", lambda **kwargs: None),
            PREFIX + ".description_example_method": module_with(
                PREFIX + ".description_example_method", "ToolDescriptionMethod", lambda *args: None),
            PREFIX + ".toolcall_example_method": module_with(
                PREFIX + ".toolcall_example_method", "APICallToExampleMethod", lambda *args, **kwargs: None),
            "openjiuwen.core.common.logging": module_with(
                "openjiuwen.core.common.logging", "logger",
                types.SimpleNamespace(info=lambda *args: None)),
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "lookup.json"
            output_path.write_text(json.dumps([[
                {"fn_call": {"arguments": {"password": secret}}}
            ]]), encoding="utf-8")
            with patch.dict(sys.modules, fake_modules):
                pipeline = load_module(
                    "test_customized_pipeline", UTILS / "customized_pipline.py")
            config = {"save_dir": temp_dir, "beam_width": 1, "expand_num": 1,
                      "max_depth": 1, "num_workers": 1, "verbose": False, "top_k": 1}
            result = pipeline.customized_pipeline(
                "example", {"name": "lookup", "description": "API key: " + secret},
                config, tool_callable=lambda *args: None)
            self.assertNotIn(secret, json.dumps(seen_tools))
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
