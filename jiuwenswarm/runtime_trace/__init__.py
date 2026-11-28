# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-local runtime trace collection for AgentServer.

This package must stay inert on import: the feature is disabled by default and
enabling it is the only path that may load OpenTelemetry, OpenJiuwen, or
SQLite. Do not import submodules here.
"""
