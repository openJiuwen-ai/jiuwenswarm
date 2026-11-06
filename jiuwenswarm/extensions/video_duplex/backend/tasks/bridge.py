"""Duplex release checkpoint semantics over the gateway transport registry."""
from agent_gateway.extensions.video_duplex.backend.tasks.bridge import (
    EVENT,
    GatewayTaskEndpoint as _GatewayTaskEndpoint,
    handle_checkpoint_push,
)
from jiuwenswarm.server.runtime.video_task.bridge import RemoteTaskEndpoint, resolve_checkpoint_ack

__all__ = ["EVENT", "GatewayTaskEndpoint", "handle_checkpoint_push", "RemoteTaskEndpoint", "resolve_checkpoint_ack"]


class GatewayTaskEndpoint(_GatewayTaskEndpoint):
    def execute(self, command):
        action = command.get("action")
        with self.store.transaction() as db:
            task = self.store.get(db, command.get("task_id"))
            if (command.get("owner"), command.get("session_id"), command.get("request_id")) != (
                task["owner"], task["core_session_id"], task["request_id"]
            ):
                raise ValueError("TASK_EXECUTION_BINDING_STALE")
            if action == "status":
                return {"status": task["status"]}
            if action not in {
                "bind",
                "claim",
                "context_written",
                "observed",
                "close",
                "settle",
            }:
                raise ValueError("Unknown task checkpoint command")
            replay, fingerprint = self.store.replay(
                db, task["owner"], task["session"], command.get("command_id"), command
            )
            if replay is not None:
                return replay
            if action == "bind" and not task.get("execution_bound") and (
                task.get("execution_cancelled") or task["status"] == "cancelling"
            ):
                # A delayed request may arrive after its admission was revoked.
                # Return a normal ACK so the remote owner can exit before Core
                # starts, rather than failing the checkpoint transport.
                task.update(
                    checkpoint_open=False, execution_settled=True,
                    execution_cancelled=True, output_closed=True,
                    sequence=task["sequence"] + 1,
                )
                self.store.put(db, task)
                result = {"cancelled": True}
                self.store.command(db, task, command["command_id"], fingerprint, result)
                return result
            if action in {"bind", "claim", "context_written", "observed"}:
                if task["status"] not in {"running", "waiting_user"}:
                    raise RuntimeError("TASK_EXECUTION_NOT_RUNNING")
            result = {}
            if action == "bind":
                if task.get("execution_bound") or not task.get("checkpoint_open"):
                    raise RuntimeError("Task execution is not available for a new invocation")
                task["execution_bound"] = True
            elif not task.get("execution_bound"):
                raise RuntimeError("Task execution is not bound")
            elif action == "claim":
                if not task.get("checkpoint_open"):
                    raise RuntimeError("TASK_EXECUTION_BINDING_CLOSED")
                changes = [c for c in task["changes"] if c["state"] == "pending"]
                for change in changes:
                    change["state"] = "claimed"
                result = {"changes": changes}
            elif action in {"context_written", "observed"}:
                if not task.get("checkpoint_open"):
                    raise RuntimeError("TASK_EXECUTION_BINDING_CLOSED")
                before, after = ("claimed", "context_written") if action == "context_written" else (
                    "context_written", "model_input_observed"
                )
                for change in task["changes"]:
                    if change["id"] in command.get("change_ids", []) and change["state"] == before:
                        change["state"] = after
            elif action == "close":
                task["checkpoint_open"] = False
            elif action == "settle":
                if command.get("cancelled"):
                    # Close and settle in one transaction so a second cancel cannot split them.
                    task["checkpoint_open"] = False
                elif task.get("checkpoint_open"):
                    raise RuntimeError("Task invocation is still open")
                task["execution_settled"] = True
                task["execution_cancelled"] = bool(command.get("cancelled"))
            task["sequence"] += 1
            self.store.put(db, task)
            self.store.command(db, task, command["command_id"], fingerprint, result)
            return result
