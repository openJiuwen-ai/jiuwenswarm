#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Controller package exports."""

from playwright_runtime.controllers.action import (
    ActionController,
    bind_runtime,
    bind_runtime_runner,
    clear_runtime_runner,
    describe_actions,
    get_default_controller,
    list_actions,
    register_action,
    register_action_spec,
    register_example_actions,
    run_action,
)
from playwright_runtime.controllers.base import BaseController

__all__ = [
    "BaseController",
    "ActionController",
    "get_default_controller",
    "bind_runtime",
    "bind_runtime_runner",
    "clear_runtime_runner",
    "register_action",
    "register_action_spec",
    "register_example_actions",
    "list_actions",
    "describe_actions",
    "run_action",
]
