"""Stdio MCP host. Tool output retains its content blocks and history metadata."""
import argparse
import asyncio
import logging
import os
import signal
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from ..client.diagnostics import SDK_VERSION
from ..client.preview import latest_preview_sink
from ..protocol.primitives import trim
from ..protocol.schema import obj, string
from .history import HISTORY_KEY, history_request
from .session import ExecutionSession

DESCRIPTION = ('Execute Python with top-level await in a persistent Computer Use '
               'session on the MCP host. App selectors and file paths refer to that '
               'computer. Preloaded: computer, print(*values), printImage(PNG bytes). '
               'Variables survive calls. Await every operation. Read the computer-use '
               'skill for the SDK API and input rules. Actions emit dispatched/verified '
               'receipts only, without automatically reading trees or screenshots, even '
               'on failure. Explicitly call read() to observe current UI and verify '
               'outcomes. Reads automatically present text; diffs require '
               'host-confirmed retained history, otherwise full text is used. '
               "state.text is always complete for its bounded view. Use read({'full': "
               'True}) for full presentation, print() for other values, and '
               'printImage() for images. printImage labels dimensions and source '
               'without reading again. One call at a time; 45 s without native command '
               'progress; command starts/completions renew the watchdog, printing does '
               'not. Individual commands keep their own deadlines. Timeout/cancellation '
               'clears variables and observations; never automatically replay actions. '
               "Executes with the MCP server user's "
               'permissions.')
RESET_DESCRIPTION = ("Reset this connection's Computer Use execution session and stop its "
                     'window capture streams. Clears all Python variables, imports, tasks, '
                     'App/Element handles and observations. Works even if a script is stuck. '
                     'Does not close apps, undo actions or delete files, logs or saved '
                     'packages. Call when finished using the computer, or to start a clean '
                     'environment; afterwards recreate variables and read before acting. '
                     'Idempotent; no '
                     'parameters.')
_input = obj(dict(title=string(min=1, max=200).optional(), code=string(min=1, max=65536)))
_empty = obj({})


def capabilities():
    platform = "macos" if sys.platform == "darwin" else "windows" if sys.platform == "win32" else sys.platform
    return types.ServerCapabilities(
        tools=types.ToolsCapability(), experimental={
            "computer-use-host": dict(version=1, platform=platform), "computer-use-preview": dict(version=2),
            "computer-use-demo-foreground": dict(version=1), HISTORY_KEY: dict(version=1)
        })


def _supports(client_capabilities, key, version):
    value = client_capabilities.get(key, {})
    return isinstance(value, dict) and type(value.get("version")) in (int, float) and value["version"] == version


class ComputerUseServer:

    def __init__(self):
        self.server = Server("computer-use", version=SDK_VERSION)
        self.session = None

        @self.server.list_tools()
        async def list_tools():
            return [
                types.Tool(
                    name="computer_use", description=DESCRIPTION, inputSchema=dict(
                        type="object", properties=dict(
                            title=dict(
                                type="string", minLength=1, maxLength=200,
                                description=("A short sentence describing what this call will do, in the user's "
                                             'language. Describe intent, not an unverified successful outcome. Used '
                                             'as the visible call '
                                             'title.')),
                            code=dict(type="string", minLength=1, maxLength=65536,
                                      description="Python code. Use print() / printImage() for output.")),
                        required=["code"], additionalProperties=False),
                    annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False,
                                                      openWorldHint=True)),
                types.Tool(
                    name="computer_use_reset", description=RESET_DESCRIPTION,
                    inputSchema=dict(type="object", properties={}, additionalProperties=False),
                    annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                                      openWorldHint=False))
            ]

        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            if name == "computer_use_reset":
                _empty.parse(arguments)
                result = await self.session.reset() if self.session is not None else dict(content=[
                    dict(
                        type="text", text=('No active execution session or capture streams. The next computer_use '
                                           'call starts a fresh '
                                           'environment.'))
                ])
                return types.CallToolResult.model_validate(result)
            if name != "computer_use":
                raise ValueError("Unknown tool: " + name)
            # Validate UTF-16 lengths consistently with the existing protocol;
            # the SDK's JSON Schema validation uses Python character counts.
            arguments = dict(arguments)
            if isinstance(arguments.get("title"), str):
                arguments["title"] = trim(arguments["title"])
            args = _input.parse(arguments)
            if not trim(args["code"]):
                raise ValueError("code must not be blank")
            context = self.server.request_context
            client = context.session.client_params
            experimental = client.capabilities.experimental or {} if client is not None else {}
            metadata = context.meta.model_dump() if context.meta is not None else {}
            if any(key in metadata
                   for key in ("computer-use-learning", "computer-use/decision", "computer-use-experience")):
                raise ValueError(
                    "Learning, model delegation and experience extensions are not supported. No code was executed.")
            if self.session is None:

                async def preview(event):
                    notice = types.ServerNotification.model_construct(
                        root=types.Notification(method="notifications/computer-use/preview", params=event))
                    await context.session.send_notification(notice)

                self.session = ExecutionSession(
                    45000,
                    latest_preview_sink(preview) if _supports(experimental, "computer-use-preview", 2) else None,
                    _supports(experimental, "computer-use-demo-foreground", 1)
                    and experimental["computer-use-demo-foreground"].get("enabled") is True)
            progress_token = metadata.get("progressToken")
            progress = 0

            async def report_progress():
                nonlocal progress
                progress += 1
                try:
                    await context.session.send_progress_notification(progress_token, progress,
                                                                     related_request_id=context.request_id)
                except Exception:
                    logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)

            result = await self.session.execute(
                args["code"],
                history=history_request(metadata.get(HISTORY_KEY)) if _supports(experimental, HISTORY_KEY, 1) else None,
                title=args.get("title"), on_progress=report_progress if progress_token is not None else None)
            return types.CallToolResult.model_validate(result)

    async def close(self):
        if self.session is not None:
            await asyncio.shield(self.session.close())

    async def run(self):
        try:
            async with stdio_server() as (read, write):
                await self.server.run(
                    read, write,
                    InitializationOptions(server_name="computer-use", server_version=SDK_VERSION,
                                          capabilities=capabilities()))
        finally:
            await self.close()


async def main():
    host = ComputerUseServer()
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except RuntimeError:
            logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)
    try:
        await host.run()
    except asyncio.CancelledError:
        logging.getLogger(__name__).debug("Best-effort cleanup or preview delivery failed", exc_info=True)


def cli(argv=None):
    parser = argparse.ArgumentParser(description="Computer Use stdio MCP host")
    parser.add_argument(
        "--desktop", choices=("shared", "exclusive"), help="Override COMPUTER_USE_DESKTOP for this MCP process")
    args = parser.parse_args(argv)
    if args.desktop is not None:
        os.environ["COMPUTER_USE_DESKTOP"] = args.desktop
    asyncio.run(main())


if __name__ == "__main__":
    cli()
