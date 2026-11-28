# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the doctor result summarizer and blocker selection gates."""

from jiuwenswarm.common.startup_diagnostics import (
    select_blocking_doctor_check,
    summarize_doctor_result,
)


class TestSummarizeDoctorResult:
    def test_ok_status_reports_pass(self):
        summary = summarize_doctor_result({"status": "ok"})
        assert "自检通过" in summary

    def test_failed_checks_are_listed_with_message(self):
        result = {
            "status": "environment_error",
            "checks": [
                {
                    "name": "playwright",
                    "display_name": "浏览器组件",
                    "status": "failed",
                    "message": "加载失败详情",
                },
                {"name": "python", "status": "passed"},
                "not-a-dict",
            ],
        }
        summary = summarize_doctor_result(result)
        assert "浏览器组件: 加载失败详情" in summary
        assert "python" not in summary
        assert "请修复系统环境后重新运行自检" in summary

    def test_missing_message_falls_back_to_generic_text(self):
        result = {
            "status": "environment_error",
            "checks": [{"name": "foo", "status": "failed"}],
        }
        summary = summarize_doctor_result(result)
        assert "foo: 加载失败" in summary

    def test_no_failed_checks_mentions_nothing_specific(self):
        result = {
            "status": "environment_error",
            "checks": [{"name": "python", "status": "passed"}],
        }
        summary = summarize_doctor_result(result)
        assert "python" not in summary
        assert "请修复系统环境后重新运行自检" in summary


class TestSelectBlockingDoctorCheckGates:
    def test_missing_doctor_result_returns_none(self):
        assert select_blocking_doctor_check(None, None) is None

    def test_non_environment_error_status_returns_none(self):
        assert (
            select_blocking_doctor_check({"status": "ok"}, None) is None
        )

    def test_non_list_checks_returns_none(self):
        assert (
            select_blocking_doctor_check(
                {"status": "environment_error", "checks": "junk"}, None
            )
            is None
        )

    def test_no_failed_checks_returns_none(self):
        assert select_blocking_doctor_check(
            {"status": "environment_error", "checks": [{"status": "passed"}]}, None
        ) is None

    def test_non_native_startup_failure_returns_none(self):
        doctor = {
            "status": "environment_error",
            "checks": [
                {"name": "playwright", "status": "failed", "kind": "native_import"}
            ],
        }
        failure = {"error_type": "RuntimeError", "message": "boom", "traceback": ""}
        assert select_blocking_doctor_check(doctor, failure) is None

    def test_matching_native_check_is_selected(self):
        doctor = {
            "status": "environment_error",
            "checks": [
                {
                    "name": "foo.bar",
                    "status": "failed",
                    "kind": "native_import",
                }
            ],
        }
        failure = {
            "error_type": "ModuleNotFoundError",
            "message": "No module named 'foo.bar'",
            "traceback": "Traceback ... import foo.bar",
        }
        selected = select_blocking_doctor_check(doctor, failure)
        assert selected is not None
        assert selected["name"] == "foo.bar"

    def test_single_failed_check_without_startup_failure_is_returned(self):
        doctor = {
            "status": "environment_error",
            "checks": [
                {"name": "only", "status": "failed", "kind": "config"}
            ],
        }
        selected = select_blocking_doctor_check(doctor, None)
        assert selected == doctor["checks"][0]
