# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Drive three representative flows to generate telemetry for the E2E verifier.

Flows:
  1. Pure LLM (no tool)
  2. Single tool call
  3. Nested tool calls (tool -> llm -> tool)
  4. One deliberately failing tool (to populate gen_ai.tool.error.count)

Wire this up to the project's existing CLI/WS test client. The caller must
adapt to the local gateway protocol — leaving the flow bodies here would
freeze an assumption about message format. Raises NotImplementedError until
an operator wires it to the actual client (see `tests/system_tests/test_cli_channel_ws.py`
for a reference harness).
"""

from __future__ import annotations


def drive_pure_llm() -> None:
    raise NotImplementedError(
        "Wire this to the project's CLI or WS client: send a prompt that "
        "produces a single assistant message with no tool calls."
    )


def drive_single_tool() -> None:
    raise NotImplementedError(
        "Wire this to the project's CLI or WS client: send a prompt that "
        "triggers exactly one tool call."
    )


def drive_nested_tool() -> None:
    raise NotImplementedError(
        "Wire this to the project's CLI or WS client: send a prompt that "
        "triggers a tool call whose result leads to another tool call."
    )


def drive_error_tool() -> None:
    raise NotImplementedError(
        "Wire this to the project's CLI or WS client: send a prompt that "
        "triggers a tool that is known to raise."
    )


def main() -> None:
    drive_pure_llm()
    drive_single_tool()
    drive_nested_tool()
    drive_error_tool()


if __name__ == "__main__":
    main()
