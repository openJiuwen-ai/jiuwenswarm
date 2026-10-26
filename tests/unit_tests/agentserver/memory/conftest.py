# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Memory package test markers / shared state.

Path stub coordination for `test_external_memory_*.py` now lives in
`tests/unit_tests/conftest.py` (``pytest_runtest_setup``) so
``jiuwenswarm.utils`` is not left pointing at test doubles for the rest of the
session. Do not reintroduce a second capture/restore of the same callables
here, or import order can save the *stub* as the "real" function to restore.
"""
