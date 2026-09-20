# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compatibility alias for :mod:`jiuwenswarm.channels.cli._terminal`."""

from importlib import import_module
import sys

sys.modules[__name__] = import_module("jiuwenswarm.channels.cli._terminal")
