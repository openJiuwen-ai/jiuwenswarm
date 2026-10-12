# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Research capabilities the research-paper team calls: one module per capability.

Every module here imports with the standard library alone, so the review entry
and the --replay paths can load it by file path on a bare Python. A capability
that needs a third-party package imports it inside the function that uses it.
"""
