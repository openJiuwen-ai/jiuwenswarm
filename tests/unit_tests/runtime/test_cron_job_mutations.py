"""Cron job mutation guards shared by the gateway and jw cron packages.

``test_cron_store_rejects_groups_on_create_and_update`` moved here from
``tests/unit_tests/gateway/test_cron_scheduler.py``: the upstream mirror copy
parametrizes ``agent_gateway.runtime.cron`` twice, while the jw copy replaced
one entry with ``jiuwenswarm.runtime.cron`` — that parametrization was the
only coverage of ``jiuwenswarm/runtime/cron/cron_job_mutations.py``. Both
packages are kept here so the jw-owned guard is not lost with the mirror layer.
"""
from __future__ import annotations

from importlib import import_module

import pytest


@pytest.mark.parametrize("package", ["agent_gateway.runtime.cron", "jiuwenswarm.runtime.cron"])
def test_cron_store_rejects_groups_on_create_and_update(package):
    mutations = import_module(f"{package}.cron_job_mutations")
    params = {
        "name": "single model task",
        "description": "report",
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "targets": "web",
    }
    group = {"type": "model_group", "id": "group"}
    with pytest.raises(ValueError, match="only support a single model"):
        mutations.build_new_cron_job(**params, model_selection=group)
    job = mutations.build_new_cron_job(**params, model_name="configured-model")
    assert job.model_name == "configured-model"
    with pytest.raises(ValueError, match="only support a single model"):
        mutations.apply_cron_job_patch(job, {"model_selection": group})
    assert job.model_name == "configured-model"
