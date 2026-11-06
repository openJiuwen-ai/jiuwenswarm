"""Dual-package parity test for cron job store mutations.

The CronSchedulerService unit suite lives in the agent-gateway repo and runs
in its CI, so swarm no longer mirrors it here. This file keeps only the
parity case below: it exercises swarm-owned code (``jiuwenswarm.runtime.cron``)
against the gateway copy (``agent_gateway.runtime.cron``) and therefore cannot
move upstream.
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
