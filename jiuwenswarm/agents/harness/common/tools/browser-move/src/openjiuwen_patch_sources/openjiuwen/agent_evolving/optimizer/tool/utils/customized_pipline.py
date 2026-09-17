# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import json
import os
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

from openjiuwen.agent_evolving.optimizer.tool.utils.beam_search import BeamSearch
from openjiuwen.agent_evolving.optimizer.tool.utils.customized_api import SimpleAPIWrapperFromCallable
from openjiuwen.agent_evolving.optimizer.tool.utils.customized_eval import SimpleEval
from openjiuwen.agent_evolving.optimizer.tool.utils.description_example_method import ToolDescriptionMethod
from openjiuwen.agent_evolving.optimizer.tool.utils.toolcall_example_method import APICallToExampleMethod
from openjiuwen.core.common.logging import logger


def customized_pipeline(stage, tool, config, tool_callable=None):
    """Run pipeline

    Args:
        stage (str): Which stage to run. Expected options: ['example', 'description']
        config (dict): config parameters for running
        tool (dict): case details with ground truth - main input data
    """

    if "fn_call_path" in config:
        raise NotImplementedError("config based api wrapper is not implemented yet.")
    elif tool_callable is not None:
        call_api_fn = SimpleAPIWrapperFromCallable(tool_callable, tool['name'], config)
    else:
        raise ValueError("Either config or tool_callable must be provided.")
    eval_fn = SimpleEval(api_wrapper=call_api_fn, config=config)
    api_keys = None # api keys are templates offered to llm for params generation
    non_opt_params = []

    if stage == "example":
        method = APICallToExampleMethod(config, call_api_fn, eval_fn, api_keys=api_keys, non_opt_params=non_opt_params)
    elif stage == "description":
        method = ToolDescriptionMethod(config, eval_fn)
    else:
        raise ValueError(f"wrong stage: {stage}")
    
    logger.info("=== Starting SingleRoundSearch ===")
    single_search = BeamSearch(
        method=method,
        beam_width=config["beam_width"],
        expand_num=config["expand_num"],
        max_depth=config["max_depth"],
        num_workers=config["num_workers"],
        verbose=config["verbose"],
        early_stop=True,
        check_valid=True,
        max_score=3.0,
        top_k=config["top_k"],
    )
    result = single_search.search(tool)

    
    # save results
    save_filename = f"{tool['name']}.json"
    save_path = str(Path(config["save_dir"]) / save_filename)
    os.makedirs(config["save_dir"], exist_ok=True)
    
    # merge to old results if any
    if Path(save_path).exists():
        with open(save_path, "r", encoding="utf-8") as f:
            result = json.load(f) + result
    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result