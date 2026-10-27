"""Qwen Omni Realtime tool definitions and request validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any


QWEN_OMNI_DELEGATE_TOOL_NAME = "jiuwen_delegate"
QWEN_OMNI_RESEARCH_TOOL_NAME = "jiuwen_research"
_MAX_CALL_ID_CHARS = 200
_MAX_TASK_CHARS = 16_000
_TASK_TOOLS = {
    "jiuwen_task_query",
    "jiuwen_task_cancel",
    "jiuwen_task_modify",
    "jiuwen_task_reorder",
    "jiuwen_task_answer",
}
_DELEGATE_ARGUMENT_NAMES = ("task", "query", "instruction", "request")


def delegate_matches_input(question: str, task: str) -> bool:
    """Reject a new task with no recognizable subject from a full voice turn.

    Short answers such as "十九点" depend on earlier conversation and remain valid.
    This check only catches clear drift; it is not a semantic classifier.
    """
    source = re.sub(r"[^\u4e00-\u9fff]", "", question)
    target = re.sub(r"[^\u4e00-\u9fff]", "", task)
    if len(source) >= 6 and len(target) >= 4:
        source_pairs = {source[index:index + 2] for index in range(len(source) - 1)}
        target_pairs = {target[index:index + 2] for index in range(len(target) - 1)}
        if source_pairs & target_pairs:
            return True
        # A Latin product, path, or name may be the shared subject.
        source_words = set(re.findall(r"[a-z0-9]{3,}", question.casefold()))
        target_words = set(re.findall(r"[a-z0-9]{3,}", task.casefold()))
        return bool(source_words & target_words)
    return True


@dataclass(frozen=True)
class QwenOmniToolCall:
    name: str
    call_id: str
    arguments: dict[str, Any]
    task: str

    @property
    def query(self) -> str:
        """Compatibility alias for existing video search job fields."""
        return self.task


def qwen_omni_tools(*, queue_manager: bool = False) -> list[dict[str, Any]]:
    """Return fresh Qwen-compatible tool definitions for each session."""
    return ([queue_management_tool(), next(
        tool for tool in task_management_tools() if tool["function"]["name"] == "jiuwen_task_answer"
    )] if queue_manager else task_management_tools()) + [
        {
            "type": "function",
            "function": {
                "name": QWEN_OMNI_DELEGATE_TOOL_NAME,
                "description": (
                    "Delegate any request that cannot be completed directly from the current "
                    "audio, video, and conversation to the full Jiuwen Core Agent. Jiuwen may "
                    "use all of its available capabilities, including research, files, document "
                    "processing, calculation, code execution, and computer or browser tools. Prior results can still be acted on after their creation invocation completed."
                    " Call this when the user explicitly requests background Agent work. If the Agent must "
                    "ask about budget or other information first, include asking and waiting in task and "
                    "delegate immediately; do not substitute your own question for task creation."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string", "maxLength": 80,
                            "description": "Short action-and-object task name, usually 8-20 Chinese characters. Resolve references from context; do not invent goals or claim success. Display only; task must retain all requirements.",
                        },
                        "independent": {
                            "type": "boolean",
                            "description": (
                                "True only for a self-contained new task that may run alongside other work. Changes to"
                                " existing work must use task_modify. Default false preserves ordering."
                            ),
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Exact job IDs whose successful results are required. "
                                "Query to resolve IDs; never guess."
                            ),
                        },
                        "resources": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Shared resources this task may access or change; use canonical absolute file paths or"
                                " stable resource names. Same resources serialize. Declare known shared modification "
                                "targets. Omission means unspecified, not exclusive scheduling. Separate new documents"
                                " in default per-task workspaces need no resource declaration. This is scheduling "
                                "metadata, not file access authorization."
                            ),
                        },
                        "task": {
                            "type": "string",
                            "description": (
                                "A complete, self-contained task preserving the user's requested "
                                "action, target, path or name, output format, and constraints."
                            ),
                        },
                    },
                    "required": ["task"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def parse_qwen_omni_tool_call(value: Any) -> QwenOmniToolCall:
    """Validate the current delegation tool and legacy research calls."""
    if not isinstance(value, dict):
        raise ValueError("tool call must be an object")

    name = str(value.get("name") or "").strip()
    if (
        name
        not in {QWEN_OMNI_DELEGATE_TOOL_NAME, QWEN_OMNI_RESEARCH_TOOL_NAME}
        | _TASK_TOOLS | {"jiuwen_queue_manage"}
    ):
        raise ValueError(f"unsupported Qwen tool: {name or '<empty>'}")

    call_id = str(value.get("call_id") or "").strip()
    if not call_id or len(call_id) > _MAX_CALL_ID_CHARS:
        raise ValueError(f"call_id must contain 1-{_MAX_CALL_ID_CHARS} characters")

    raw_arguments = value.get("arguments")
    if isinstance(raw_arguments, str):
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            raise ValueError("arguments must be valid JSON") from exc
    elif isinstance(raw_arguments, dict):
        arguments = dict(raw_arguments)
    else:
        raise ValueError("arguments must be a JSON object")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    if name == "jiuwen_queue_manage":
        instruction = arguments.get("instruction")
        if set(arguments) != {"instruction"} or not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 16000:
            raise ValueError("Queue management requires a nonempty instruction")
        return QwenOmniToolCall(name, call_id, {"instruction": instruction.strip()}, instruction.strip())
    if "title" in arguments:
        title = arguments["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 80 or "\n" in title or "\r" in title:
            raise ValueError("Invalid title")
        arguments["title"] = title.strip()
    if name in _TASK_TOOLS:
        if name == "jiuwen_task_reorder" and "queue_version" in arguments:
            version = arguments["queue_version"]
            if isinstance(version, str) and (
                version == "0" or (version.isascii() and version.isdecimal() and not version.startswith("0"))
            ) and len(version) <= 16:
                version = int(version)
            if type(version) is not int or not 0 <= version <= 2**53 - 1:
                # The model may miscopy this large internal token. Let the
                # server read the current version instead of rejecting the action.
                arguments.pop("queue_version")
        schema = next(
            t["function"]["parameters"]
            for t in task_management_tools()
            if t["function"]["name"] == name
        )
        if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(
            arguments
        ):
            raise ValueError("Invalid task tool arguments")
        for key, item in arguments.items():
            if key == "answers":
                if not isinstance(item, list) or not 1 <= len(item) <= 32:
                    raise ValueError("Invalid answers")
            elif key in {"revision", "offset", "queue_version"}:
                # Some providers quote JSON integers despite the integer schema.
                # Normalize only canonical decimal queue tokens; never coerce
                # booleans, fractions, signs, exponents or arbitrary text.
                if key == "queue_version" and isinstance(item, str) and (
                    item == "0" or (item.isascii() and item.isdecimal() and not item.startswith("0"))
                ) and len(item) <= 16:
                    item = int(item)
                    arguments[key] = item
                if type(item) is not int or item < (1 if key == "revision" else 0):
                    raise ValueError(f"Invalid {key}")
                if key == "queue_version" and item > 2**53 - 1:
                    raise ValueError("Invalid queue_version: exceeds JavaScript safe integer range")
            elif name == "jiuwen_task_answer" and key == "job_id" and item == "":
                continue  # A typed-chat approval card has no job; it is answered with job_id "".
            elif (
                not isinstance(item, str)
                or not item.strip()
                or len(item) > (4000 if key == "instruction" else 256)
            ):
                raise ValueError(f"Invalid {key}")
        if name == "jiuwen_task_query" and "status" in arguments:
            if arguments["status"] not in schema["properties"]["status"]["enum"]:
                raise ValueError("Invalid task status")
        if name == "jiuwen_task_reorder":
            if arguments["action"] not in {"next", "before"}:
                raise ValueError("Invalid queue action: use next or before; before_job_id is a separate field")
            if (arguments["action"] == "before") != bool(
                arguments.get("before_job_id")
            ):
                raise ValueError("Only before requires before_job_id")
        return QwenOmniToolCall(name, call_id, arguments, "")
    task_fields = set(arguments) - {"independent", "depends_on", "resources", "title"}
    if len(task_fields) != 1 or (
        name == QWEN_OMNI_RESEARCH_TOOL_NAME and len(arguments) != 1
    ):
        raise ValueError("arguments must contain exactly one task field")
    if not isinstance(arguments.get("independent", False), bool):
        raise ValueError("independent must be a boolean")
    for key in ("depends_on", "resources"):
        values = arguments.get(key, [])
        if (
            not isinstance(values, list)
            or len(values) > 32
            or any(
                not isinstance(v, str) or not v.strip() or len(v) > 256 for v in values
            )
        ):
            raise ValueError(f"Invalid {key}")
    if name == QWEN_OMNI_DELEGATE_TOOL_NAME:
        argument_name = next(
            (key for key in _DELEGATE_ARGUMENT_NAMES if key in arguments), None
        )
    else:
        argument_name = "query" if "query" in arguments else None
    if argument_name is None:
        raise ValueError("arguments must contain a supported task field")

    raw_task = arguments.get(argument_name)
    if not isinstance(raw_task, str):
        raise ValueError(f"{argument_name} must be a string")
    task = raw_task.strip()
    if not task or len(task) > _MAX_TASK_CHARS:
        raise ValueError(f"{argument_name} must contain 1-{_MAX_TASK_CHARS} characters")
    return QwenOmniToolCall(
        name=name,
        call_id=call_id,
        arguments=arguments,
        task=task,
    )


def queue_management_tool():
    return {
        "type": "function",
        "function": {
            "name": "jiuwen_queue_manage",
            "description": (
                "Send any existing task queue query, reorder, cancellation, modification or information answer "
                "to the dedicated text queue manager. Include the full user request and resolved references. "
                "For 'first X, then Y' involving existing tasks, call this before speaking. "
                "The manager queries real tasks and executes controls; report only its returned receipts."
            ),
            "parameters": {
                "type": "object", "properties": {"instruction": {"type": "string", "maxLength": 16000}},
                "required": ["instruction"], "additionalProperties": False,
            },
        },
    }


def task_management_tools():
    specs = [
        (
            "jiuwen_task_reorder",
            (
                "Reorder waiting tasks only, without stopping any running task. Query the queue first."
                " Clarify ambiguous targets. Priority never bypasses dependencies or resources and "
                "does not promise immediate execution."
                " Use returned opaque IDs, not task names. action must be exactly next or before;"
                " for before pass the other waiting task ID in the separate before_job_id field."
            ),
            {
                "job_id": {"type": "string"},
                "queue_version": {"type": "integer", "minimum": 0,
                                  "description": "Optional legacy concurrency token. Omit it; the server reads the current queue version."},
                "action": {"type": "string", "enum": ["next", "before"]},
                "before_job_id": {"type": "string"},
            },
            ["job_id", "action"],
        ),
        (
            "jiuwen_task_answer",
            (
                "Answer an observed pending Agent information question or approval card so the original task "
                "continues. Query exact job_id, interaction.id and questions first; pass interaction.id as "
                "interaction_id. Clarify which task if ambiguous. Never create a new task for an answer, and "
                "never use a new request (e.g. 打开微信) as an answer. Approval cards: choose the allow option "
                "only when the user clearly says 同意/允许/可以/批准, the reject option when they clearly say "
                "拒绝/不要/取消这个操作, the session option (会话内记住) only for 本会话保持同意/本会话同意/这个会话都允许; the permanent option (永久记住) only for an explicit 永久同意/永久允许/以后都允许; "
                "if unclear or several cards are pending, ask first and do not submit. Say 已批准 only after "
                "the receipt or a later query shows the task resumed. answers is an array of plain text "
                "strings, one per observed question in the SAME order, e.g. [\"5000元\", \"2人\"]. "
                "Use only explicit user answers; ask for missing answers instead of inventing them. "
                "Accepted is not completed."
            ),
            {
                "job_id": {"type": "string"},
                "interaction_id": {"type": "string"},
                "answers": {
                    "type": "array",
                    "description": (
                        "Plain text answers in observed question order, e.g. [\"5000元\"]. "
                        "Answer every question exactly once; never copy or paraphrase the question."
                    ),
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {"type": "string", "maxLength": 4000},
                },
            },
            ["job_id", "interaction_id", "answers"],
        ),
        (
            "jiuwen_task_query",
            (
                "Find Core invocations and their results in this conversation, plus the user's scheduled "
                "tasks (定时任务) from all conversations in scheduled_jobs. Query before selecting an "
                "ambiguous task; use exact returned job_id and revision for controls. "
                "query is a literal substring of the task title or original task text, not a question or "
                "a list of keywords. Prefer a known job_id. If no task matches, shorten query "
                "or list without query; no match does NOT prove completion or absence of files."
            ),
            {
                "job_id": {"type": "string"},
                "query": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "status": {
                    "type": "string",
                    "enum": [
                        "queued", "running", "waiting_user", "cancelling", "unknown",
                        "completed", "failed", "cancelled", "unfinished",
                    ],
                },
            },
            [],
        ),
        (
            "jiuwen_task_cancel",
            "Request cancellation of the exact Core invocation, not reversal of its completed effects. Completed invocations cannot be stopped. To remove, disable or undo resulting effects requested by the user, use task_modify or delegate that full action to Core; completion is not grounds to refuse. Accepted does not mean stopped. Query for the final state.",
            {"job_id": {"type": "string"}},
            ["job_id"],
        ),
        (
            "jiuwen_task_modify",
            (
                "Change the exact task's requirements. Queued input is updated; running changes wait "
                "for a model checkpoint; completed work creates a linked Core revision carrying the original result. Receipt never proves"
                " the requirement is satisfied."
            ),
            {
                "job_id": {"type": "string"},
                "revision": {"type": "integer", "minimum": 1},
                "instruction": {"type": "string", "maxLength": 4000},
                "title": {"type": "string", "maxLength": 80, "description": "Updated short action-and-object title when the requested goal changes. Omit to retain the existing title."},
            },
            ["job_id", "revision", "instruction"],
        ),
    ]
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
        }
        for name, description, properties, required in specs
    ]
