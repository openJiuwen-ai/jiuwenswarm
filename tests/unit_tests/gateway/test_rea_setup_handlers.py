from __future__ import annotations

from jiuwenswarm.gateway.channel_manager.web import rea_setup_handlers


def test_base_steps_do_not_include_enhanced_components() -> None:
    steps = rea_setup_handlers._base_steps()

    assert [step["id"] for step in steps] == [
        "node_npx",
        "rea_package",
        "write_config",
        "verify_mcp",
        "final_scan",
    ]
    assert not any(step["id"].startswith("component_") for step in steps)
    assert all(
        step["expected_seconds_min"] <= step["expected_seconds_max"]
        for step in steps
    )


def test_enhanced_steps_include_dependency_detail_and_theoretical_time() -> None:
    plan = [
        {
            "id": "java",
            "label": "Java",
            "install_mode": "managed",
            "auto_install": True,
        },
        {
            "id": "ghidra",
            "label": "Ghidra",
            "install_mode": "managed",
            "auto_install": True,
        },
        {
            "id": "jadx",
            "label": "JADX",
            "install_mode": "managed",
            "auto_install": True,
        },
        {
            "id": "ida",
            "label": "IDA",
            "install_mode": "manual_license",
            "auto_install": False,
        },
        {
            "id": "pwntools",
            "label": "pwntools",
            "install_mode": "python_venv",
            "auto_install": True,
        },
        {
            "id": "gdb",
            "label": "GDB",
            "install_mode": "system",
            "auto_install": False,
        },
    ]

    steps = rea_setup_handlers._enhanced_steps(plan)
    ids = {step["id"] for step in steps}

    assert "enhanced_plan" in ids
    assert "component_java_download" in ids
    assert "component_ghidra_download" in ids
    assert "component_jadx_verified" in ids
    assert "component_pwntools_download" in ids
    assert "component_pwntools_verified" in ids
    assert not any(step_id.startswith("component_ida_") for step_id in ids)
    assert not any(step_id.startswith("component_gdb_") for step_id in ids)
    ghidra_download = next(step for step in steps if step["id"] == "component_ghidra_download")
    assert ghidra_download["expected_seconds_min"] == 120
    assert ghidra_download["expected_seconds_max"] == 2400


def test_transition_steps_marks_running_and_completed_phases() -> None:
    plan = [
        {
            "id": "jadx",
            "label": "JADX",
            "install_mode": "managed",
            "auto_install": True,
        }
    ]
    job = {
        "kind": "enhanced",
        "steps": rea_setup_handlers._enhanced_steps(plan),
        "current_step_id": None,
    }

    rea_setup_handlers._transition_steps(job, "enhanced_plan", "planning", None)
    rea_setup_handlers._transition_steps(
        job,
        "component_jadx_download",
        "downloading",
        {
            "phase": "downloading",
            "downloaded_bytes": 4,
            "total_bytes": 8,
            "bytes_per_second": 2,
            "eta_seconds": 2,
        },
    )

    steps = {step["id"]: step for step in job["steps"]}
    assert steps["enhanced_plan"]["status"] == "succeeded"
    assert steps["component_jadx_planning"]["status"] == "succeeded"
    assert steps["component_jadx_download"]["status"] == "running"
    assert job["current_step_id"] == "component_jadx_download"


def test_parallel_component_progress_never_moves_backwards() -> None:
    job = {
        "job_id": "job-1",
        "kind": "enhanced",
        "status": "running",
        "percent": 45,
        "steps": rea_setup_handlers._enhanced_steps(
            [
                {
                    "id": "java",
                    "label": "Java",
                    "install_mode": "managed",
                    "auto_install": True,
                },
                {
                    "id": "adb",
                    "label": "ADB",
                    "install_mode": "managed",
                    "auto_install": True,
                },
            ]
        ),
        "current_step_id": None,
        "updated_at": 1,
    }
    rea_setup_handlers._JOBS["job-1"] = job

    rea_setup_handlers._update_progress(
        "job-1",
        "component_adb_download",
        24,
        "ADB downloading",
        {
            "phase": "downloading",
            "component_result": {
                "id": "adb",
                "state": "installed",
                "ok": True,
                "verified": True,
            },
        },
    )

    assert rea_setup_handlers._JOBS["job-1"]["percent"] == 45
    assert rea_setup_handlers._JOBS["job-1"]["component_results"]["adb"]["verified"] is True
    rea_setup_handlers._JOBS.pop("job-1", None)


async def test_cancel_handler_requests_cooperative_cancellation() -> None:
    class FakeChannel:
        def __init__(self) -> None:
            self.methods = {}
            self.response = None

        def register_method(self, name, handler) -> None:
            self.methods[name] = handler

        async def send_response(self, _ws, _req_id, **kwargs) -> None:
            self.response = kwargs

    channel = FakeChannel()
    rea_setup_handlers.register_rea_setup_handlers(channel)
    job = rea_setup_handlers._create_job(
        kind="base",
        steps=rea_setup_handlers._base_steps(),
    )
    job_id = job["job_id"]

    await channel.methods["rea.setup.cancel"](object(), "req-1", {"job_id": job_id}, "sess")

    assert channel.response["ok"] is True
    assert rea_setup_handlers._JOB_CANCEL_EVENTS[job_id].is_set()

    rea_setup_handlers._JOBS.pop(job_id, None)
    rea_setup_handlers._JOB_CANCEL_EVENTS.pop(job_id, None)


async def test_base_status_scope_skips_enhanced_inventory(monkeypatch) -> None:
    class FakeChannel:
        def __init__(self) -> None:
            self.methods = {}
            self.response = None

        def register_method(self, name, handler) -> None:
            self.methods[name] = handler

        async def send_response(self, _ws, _req_id, **kwargs) -> None:
            self.response = kwargs

    calls = {}

    def fake_scan(**kwargs):
        calls.update(kwargs)
        return {
            "ready": True,
            "base": [],
            "enhanced": [],
            "enhanced_plan": [],
            "capability_cards": [],
        }

    async def fake_verify():
        return {"ok": True, "tool_count": 207, "error": None}

    async def fail_attach(_scan, _verification):
        raise AssertionError("base detection must not build enhanced capability cards")

    monkeypatch.setattr(rea_setup_handlers, "scan_rea_setup", fake_scan)
    monkeypatch.setattr(rea_setup_handlers, "_verify_rea_mcp", fake_verify)
    monkeypatch.setattr(rea_setup_handlers, "_attach_capability_cards", fail_attach)

    channel = FakeChannel()
    rea_setup_handlers.register_rea_setup_handlers(channel)
    await channel.methods["rea.status.get"](
        object(),
        "req-base",
        {"scope": "base"},
        "sess",
    )

    assert calls["include_enhanced"] is False
    assert channel.response["payload"]["capability_cards"] == []
