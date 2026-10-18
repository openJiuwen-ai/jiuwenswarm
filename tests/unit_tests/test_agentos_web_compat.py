"""Frozen-client compatibility must not mutate Runtime data or tool payloads."""

import copy
import json
import unittest
from unittest.mock import patch

from jiuwenswarm.common.agentos_runtime import is_agentos_workspace_fallback
from jiuwenswarm.common.mode_matrix import NEW_CANONICAL_MODE_RESOLUTION
from jiuwenswarm.gateway.channel_manager.web.agentos_compat import project_agentos_web_frame


class AgentOSWebCompatibilityTest(unittest.TestCase):
    def test_web_send_projects_only_for_agentos_router(self):
        from jiuwenswarm.gateway.routing.base_ws_channel import BaseWsChannel
        from jiuwenswarm.gateway.channel_manager.web.web_connect import WebChannel

        router_type = type("AgentOSRouterClient", (), {
            "__module__": "jiuwenswarm.extensions.agentos.agentos_router.router_client",
        })
        frame = {"type": "res", "payload": {"sessions": [
            {"session_id": "s", "mode": "team.code.normal", "work_mode": "code"},
        ]}}
        original = copy.deepcopy(frame)
        channel = object.__new__(WebChannel)
        ws = object()
        for client, expected in ((object(), "team.code.normal"), (router_type(), "team")):
            with self.subTest(mode=expected), patch.object(BaseWsChannel, "_enqueue_send") as send:
                channel.agent_client = client
                channel._enqueue_send(ws, frame)
                sent = send.call_args.args[1]
                self.assertEqual(sent["payload"]["sessions"][0]["mode"], expected)
                self.assertEqual(sent["payload"]["sessions"][0]["work_mode"], "code")
        self.assertEqual(frame, original)

    def test_all_canonical_modes_and_unchanged_nested_fields(self):
        for mode, (role, environment, _) in NEW_CANONICAL_MODE_RESOLUTION.items():
            with self.subTest(mode=mode):
                frame = {"type": "res", "id": "r", "payload": {
                    "sessions": [{"session_id": "s", "mode": mode, "work_mode": environment,
                                  "tool": {"mode": mode}}],
                }}
                original = copy.deepcopy(frame)
                projected = project_agentos_web_frame(frame)
                session = projected["payload"]["sessions"][0]
                self.assertEqual(session["mode"], role)
                self.assertEqual(session["work_mode"], environment)
                self.assertEqual(session["tool"]["mode"], mode)
                self.assertEqual(frame, original)

    def test_history_string_preserves_markers_and_content(self):
        record = {"mode": "team.code.plan", "role": "teammate",
                  "content": '{"mode":"team.code.plan"}'}
        frame = {"type": "event", "event": "history.message", "seq": 3,
                 "payload": {"message": json.dumps(record), "session_id": "s",
                             "page_idx": 2, "status": "done"}}
        result = project_agentos_web_frame(frame)
        self.assertEqual(json.loads(result["payload"]["message"]), {**record, "mode": "team"})
        self.assertEqual(result["seq"], 3)
        self.assertEqual(result["payload"]["page_idx"], 2)
        self.assertEqual(result["payload"]["status"], "done")

    def test_member_and_unknown_modes_are_not_rewritten(self):
        for event, mode in (("team.member", "human"), ("chat.final", "subagent"),
                            ("session.updated", "auto_harness")):
            frame = {"type": "event", "event": event, "payload": {"mode": mode}}
            self.assertEqual(project_agentos_web_frame(frame), frame)

    def test_plan_exit_push_projects_mode_and_retains_plan_fields(self):
        frame = {"type": "event", "event": "plan.mode_exited", "payload": {
            "session_id": "s", "mode": "team.code.normal", "plan_id": "p", "reason": "approved",
        }}
        result = project_agentos_web_frame(frame)
        self.assertEqual(result["payload"], {**frame["payload"], "mode": "team"})

    def test_workspace_requires_trusted_runtime_and_web_default(self):
        params = dict(channel_id="web", project_dir="/home/agentos/workspace/", project_id=None)
        with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": ""}):
            self.assertFalse(is_agentos_workspace_fallback(**params))
        with patch.dict("os.environ", {"JIUWENSWARM_RUNTIME_PROFILE": "agentos"}):
            self.assertTrue(is_agentos_workspace_fallback(**params))
            self.assertFalse(is_agentos_workspace_fallback(**{**params, "channel_id": "tui"}))
            self.assertFalse(is_agentos_workspace_fallback(**{**params, "project_id": "proj_1"}))
            self.assertFalse(is_agentos_workspace_fallback(**{**params, "project_dir": "/tmp/demo"}))

    def test_same_name_backup_keeps_branch_selection_key_on_wire(self):
        models = [
            {"model_name": "other", "origin_index": 0},
            {"model_name": "shared", "origin_index": 1, "is_default": True},
            {"model_name": "shared", "origin_index": 2, "is_default": False, "is_agentos": True},
            {"model_name": "shared", "origin_index": 3, "is_agentos": True, "alias": "backup"},
            {"model_name": "unique", "origin_index": 4, "is_agentos": True},
        ]
        frame = {"type": "res", "payload": {"models": models, "active_model": "other"}}
        original = copy.deepcopy(frame)
        projected = project_agentos_web_frame(frame)
        self.assertEqual(projected["payload"]["models"][2]["alias"], "shared#2")
        self.assertEqual(projected["payload"]["models"][3]["alias"], "backup")
        self.assertEqual(projected["payload"]["models"][:2], models[:2])
        self.assertEqual(projected["payload"]["models"][4], models[4])
        self.assertEqual(projected["payload"]["active_model"], "other")
        self.assertEqual(frame, original)

    def test_model_projection_requires_valid_index_and_response(self):
        for index in (None, True, -1, "1"):
            models = [{"model_name": "shared"}, {"model_name": "shared", "is_agentos": True,
                       "origin_index": index}]
            frame = {"type": "res", "payload": {"models": models}}
            self.assertEqual(project_agentos_web_frame(frame), frame)
        frame = {"type": "event", "event": "tool.result", "payload": {"models": [
            {"model_name": "shared"}, {"model_name": "shared", "is_agentos": True, "origin_index": 1},
        ]}}
        self.assertEqual(project_agentos_web_frame(frame), frame)


if __name__ == "__main__":
    unittest.main()
