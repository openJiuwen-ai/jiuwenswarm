# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""
Tool domain optimizer base class: fixes domain=tool, default_targets=[enabled, max_retries],
unifies filtering and logging semantics. Subclasses implement _backward / _update.
"""

import os
from typing import List

from openjiuwen.agent_evolving.optimizer.tool.utils.customized_pipline import customized_pipeline
from openjiuwen.agent_evolving.optimizer.tool.utils.customized_reviewer import ToolDescriptionReviewer
from openjiuwen.agent_evolving.optimizer.tool.utils.default_configs import default_config_desc, default_config_eg
from openjiuwen.agent_evolving.optimizer.tool.utils.schema_extractor import extract_schema
from openjiuwen.agent_evolving.optimizer.base import BaseOptimizer
from openjiuwen.core.common.logging import logger


class ToolOptimizerBase(BaseOptimizer):
    """
    Tool dimension optimizer base class: optimizes tunables exposed by ToolCallOperator (e.g., enabled, max_retries).
    """

    domain: str = "tool"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.max_turns = kwargs.get("max_turns", 5)
        self.llm_api_key = kwargs.get("llm_api_key", "")
        self.config_eg = kwargs.get("config_eg", default_config_eg)
        self.config_desc = kwargs.get("config_desc", default_config_desc)
        self.path_save_dir = kwargs.get("path_save_dir", "./tool_optimizer_results")
        self.config_eg['save_dir'] = os.path.join(self.path_save_dir, "examples")
        self.config_desc['save_dir'] = os.path.join(self.path_save_dir, "descriptions")
        self.config_desc['examples_dir'] = self.config_eg['save_dir']
        self.config_desc['neg_ex_input_path'] = os.path.join(
            self.path_save_dir, 
            f"{kwargs.get('tool_name','tool')}.json"
        )

    def default_targets(self) -> List[str]:
        return ["enabled", "max_retries"]

    def optimize_tool(self, tool, tool_callable):
        """Optimize tool given its description and callable."""
        result_examples = []
        result_descs = []

        original_desc = tool["description"]
        # iter_count from config originally 
        for i in range(self.max_turns):
            # update desc for after iters
            if i > 0:
                latest_description = result_desc[-1][0]["description"]
                tool["description"] = latest_description

            # stage 1 - example
            default_config_desc['llm_api_key'] = self.llm_api_key
            default_config_eg['llm_api_key'] = self.llm_api_key
            result_example = customized_pipeline(
                "example",
                tool,
                tool_callable=tool_callable,
                config=self.config_eg
            )
            result_examples.append(result_example)
            logger.info("=== EXAMPLE STAGE FINISHED ===")

            # # stage 2 - description
            result_desc = customized_pipeline(
                "description",  
                tool, 
                tool_callable=tool_callable,
                config=self.config_desc
            )
            result_descs.append(result_desc)

        # description final reviewer
        output_desc = result_desc[-1][-1]["description"]
        eval_model_id = self.config_desc.get("eval_model_id")
        processor = ToolDescriptionReviewer(
            eval_model_id=eval_model_id,
            llm_api_key=self.llm_api_key
        )

        schema = extract_schema(original_desc)
        processed = processor.process(
            data=output_desc, 
            ori_tool=tool["description"], 
            steps=["clean", "cross_check", "translate"]
        )
        final_desc = processor.format(schema, processed, example=None)

        return final_desc
 
    def _update(self):
        updates = {}
        for operator in self.operators.items():
            return
