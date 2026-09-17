# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

default_config_eg = {
    "gen_model_id": "gpt-5-mini",
    "eval_model_id": "gpt-5-mini",
    "verbose": 1,
    "num_init_loop": 1,
    "num_refine_steps": 1,
    "num_feedback_steps": 2,
    "score_eval_weight": 0.0,
    "beam_width": 2,
    "expand_num": 3,
    "max_depth": 2,
    "num_workers": 2,
    "top_k": 5
}
default_config_desc = {
    "gen_model_id": "gpt-5-mini",
    "eval_model_id": "gpt-5-mini",
    "verbose": 1,
    "num_init_loop": 1,
    "num_feedback_steps": 2,
    "score_eval_weight": 0.0,
    "num_examples_for_desc": 4,
    "beam_width": 2,
    "expand_num": 2,
    "max_depth": 2,
    "num_workers": 2,
    "top_k": 3
}