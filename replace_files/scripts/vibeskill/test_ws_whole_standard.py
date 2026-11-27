"""Standard 模式端到端 WebSocket 联调（多轮交互）。

与 ``test_ws_whole.py``（SkillCreate 单次流水线）对应，本脚本针对 ``mode=Standard``
的会话，覆盖 jiuwenclaw 通用 chat 链路：

- ``POST /api/v1/session {"mode":"Standard"}`` 创建会话；
- 通过 ``message.send`` 触发 ``chat.send``；
- 实时展示 agent 流式输出（``chat.delta`` → ``message.part.delta``）、工具调用
  （``chat.tool_call`` / ``chat.tool_result`` → ``message.part.updated``）、Todo 与状态；
- 遇到结构化提问（``chat.ask_user_question`` → ``question.asked``）时在命令行交互，
  按 ``question.replied`` 回写，Channel 会把它派发为 ``chat.user_answer``；
- 一轮 ``task.completed`` 结束后回到 prompt，等待用户输入下一轮 query；
- 支持指令：``/exit`` 退出，``/cancel`` 调 ``POST /api/v1/session/{id}/abort`` 触发
  中断（验证 ``chat.interrupt_result`` 收口路径）。

用法::

    uv run python scripts/vibeskill/test_ws_whole_standard.py [sessionID] [--query ...]

不传 ``sessionID`` 或显式 ``--create`` 时自动创建 ``mode=Standard`` 的新会话。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

import aiohttp


logger = logging.getLogger("vibeskill.standard.cli")

HTTP_BASE = os.environ.get("VIBESKILL_HTTP_BASE", "http://127.0.0.1:19003")
WS_BASE = os.environ.get(
    "VIBESKILL_WS_BASE",
    "ws://127.0.0.1:19003/api/v1/messages",
)
DEFAULT_QUERY = os.environ.get(
    "VIBESKILL_STANDARD_DEFAULT_QUERY",
    "你好，介绍一下自己。",
)

# 模型/provider 故意不在脚本里覆盖：``message.send`` 不带 ``model`` 字段时，
# AgentServer 端会回落到 ``~/.jiuwenclaw/config/config.yaml`` 配置的默认 LLM
# （通常是 minimax）。脚本不该硬编码具体模型，否则在不同部署/账号下会撞模型白名单。

# Standard 模式下一轮 chat 可能很短也可能很长（含工具调用），把单次 receive 超时拉长，
# 由 MAX_CONSECUTIVE_TIMEOUTS 兜底防止永久挂死。
RECV_TIMEOUT = float(os.environ.get("VIBESKILL_RECV_TIMEOUT", "300"))
MAX_CONSECUTIVE_TIMEOUTS = int(os.environ.get("VIBESKILL_MAX_TIMEOUTS", "12"))

CMD_EXIT = "/exit"
CMD_QUIT = "/quit"
CMD_CANCEL = "/cancel"


# ---------------------------------------------------------------------------
# Pretty terminal printing (与 test_ws_whole.py 保持一致以便对比阅读)
# ---------------------------------------------------------------------------

class StreamPrinter:
    """跟踪当前正在流式输出的 part，保证 delta 拼接显示，并在切换时换行。"""

    def __init__(self) -> None:
        self._current_part_id: str | None = None
        self._current_kind: str | None = None

    def _flush_current(self) -> None:
        if self._current_part_id is not None:
            print("", flush=True)
        self._current_part_id = None
        self._current_kind = None

    def stream_text(self, part_id: str, kind: str, text: str) -> None:
        if not text:
            return
        if part_id != self._current_part_id or kind != self._current_kind:
            self._flush_current()
            prefix = "[think] " if kind == "reasoning" else "[agent] "
            print(prefix, end="", flush=True)
            self._current_part_id = part_id
            self._current_kind = kind
        print(text, end="", flush=True)

    def line(self, text: str = "") -> None:
        self._flush_current()
        if text:
            print(text, flush=True)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

async def _create_session(http: aiohttp.ClientSession) -> str:
    url = f"{HTTP_BASE}/api/v1/session"
    async with http.post(url, json={"mode": "Standard"}) as resp:
        body = await resp.json()
        if resp.status not in (200, 201):
            raise SystemExit(f"create session failed HTTP {resp.status}: {body}")
        sid = str(body.get("sessionID") or "").strip()
        if not sid:
            raise SystemExit(f"create session missing sessionID: {body}")
        return sid


async def _abort_session(http: aiohttp.ClientSession, session_id: str) -> None:
    """对应 ``/cancel`` 指令：触发 chat.interrupt → chat.interrupt_result。

    需要北向 WS 仍连接；脚本本身就是连接持有方，因此成功率高。返回 400
    ``websocket_not_connected`` 时只记 warning，不抛错（业务/网络重连场景常见）。
    """
    url = f"{HTTP_BASE}/api/v1/session/{session_id}/abort"
    async with http.post(url, json={}) as resp:
        body = await resp.json()
        if resp.status not in (200, 201, 202):
            logger.warning("abort HTTP %s: %s", resp.status, body)
        else:
            logger.info("abort response: %s", json.dumps(body, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------

async def _ainput(prompt: str) -> str:
    """协程友好的 ``input()``。"""
    return await asyncio.to_thread(input, prompt)


def _print_question_options(options: list[Any]) -> list[str]:
    labels: list[str] = []
    for idx, opt in enumerate(options):
        if not isinstance(opt, dict):
            continue
        label = str(opt.get("label") or "").strip()
        desc = str(opt.get("description") or "").strip()
        labels.append(label)
        suffix = f" — {desc}" if desc else ""
        print(f"    [{idx + 1}] {label}{suffix}", flush=True)
    return labels


async def _prompt_question_answers(questions: list[Any]) -> list[list[str]]:
    print("\n>>> Agent 请你回答以下问题：", flush=True)
    answers: list[list[str]] = []
    for q_idx, q in enumerate(questions):
        if not isinstance(q, dict):
            answers.append([])
            continue
        question_text = str(q.get("question") or "").strip()
        header = str(q.get("header") or "").strip()
        options_raw = q.get("options", []) if isinstance(q.get("options"), list) else []
        multiple = bool(q.get("multiple"))

        print(f"\n  Q{q_idx + 1}. {question_text}", flush=True)
        if header:
            print(f"     ({header})", flush=True)
        labels = _print_question_options(options_raw)

        hint = (
            "    多选请用逗号分隔编号；回车默认第一项；输入文本则按原样作为回答："
            if multiple
            else "    输入编号或自定义文本（回车默认第一项）："
        )
        raw = (await _ainput(hint)).strip()

        if not raw:
            answers.append(labels[:1])
            continue

        token_text = raw.replace(",", " ").replace("，", " ")
        tokens = [t for t in token_text.split() if t]
        is_all_digits = bool(tokens) and all(t.isdigit() for t in tokens)
        if is_all_digits and labels:
            picks: list[str] = []
            for token in tokens:
                idx = int(token) - 1
                if 0 <= idx < len(labels):
                    picks.append(labels[idx])
            if not multiple:
                picks = picks[:1]
            answers.append(picks if picks else labels[:1])
        else:
            answers.append([raw])

    return answers


# ---------------------------------------------------------------------------
# Reply builders
# ---------------------------------------------------------------------------

def _build_message_send(session_id: str, query: str) -> dict[str, Any]:
    # 故意不带 ``model`` 字段：让 AgentServer 走 ~/.jiuwenclaw/config/config.yaml
    # 配置的默认模型（如 minimax），避免脚本硬编码的 modelID 与服务端白名单不匹配。
    return {
        "type": "message.send",
        "sessionID": session_id,
        "parts": [{"type": "text", "text": query}],
        "agent": "coder",
    }


def _build_question_reply(session_id: str, request_id: str, answers: list[list[str]]) -> dict[str, Any]:
    return {
        "type": "question.replied",
        "properties": {
            "sessionID": session_id,
            "requestID": request_id,
            "answers": answers,
        },
    }


# ---------------------------------------------------------------------------
# Session runner
# ---------------------------------------------------------------------------

class StandardSessionRunner:
    """Standard 模式多轮交互的状态机。

    与 SkillCreate 版本最大的差别：

    - 不存在导出动作；一轮 ``task.completed`` 仅表示「本轮 Agent 已结束流式输出，
      可以开始下一轮 user input」，会话本身不会进入 ``completed``。
    - 退出条件由用户主动决定（``/exit``）或网络异常；``session.status idle`` 不触发退出。
    """

    def __init__(self, session_id: str, initial_query: str) -> None:
        self.session_id = session_id
        self.initial_query = initial_query
        self.printer = StreamPrinter()
        self.should_exit = False
        # 一轮 chat 是否已结束（task.completed / task.error / session.status idle 任一）。
        self.turn_done = asyncio.Event()
        self._initial_sent = False
        self._pending_reply: dict[str, Any] | None = None
        self._tool_logged: dict[str, str] = {}

    # ----------------- WS connection lifecycle -----------------

    async def run(self, http: aiohttp.ClientSession) -> None:
        uri = f"{WS_BASE}?sessionID={self.session_id}"
        reconnect_attempt = 0
        max_reconnect = int(os.environ.get("VIBESKILL_MAX_RECONNECT", "10"))

        while not self.should_exit:
            logger.info("connecting WS %s", uri)
            try:
                async with http.ws_connect(uri, heartbeat=30) as ws:
                    reconnect_attempt = 0
                    await self._on_connected(ws)
                    await self._interactive_loop(ws, http)
            except aiohttp.ClientError as exc:
                logger.warning("ws connect error: %s", exc)

            if self.should_exit:
                break

            reconnect_attempt += 1
            if reconnect_attempt > max_reconnect:
                raise AssertionError(
                    f"WS reconnect exceeded {max_reconnect} attempts before user exit"
                )
            backoff = min(1.0 * reconnect_attempt, 5.0)
            self.printer.line(f"[ws] disconnected, reconnecting in {backoff:.1f}s...")
            await asyncio.sleep(backoff)

    async def _on_connected(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        ack = await ws.receive()
        if ack.type == aiohttp.WSMsgType.TEXT:
            logger.info("server hello: %s", ack.data)

        if not self._initial_sent:
            await ws.send_str(json.dumps(_build_message_send(self.session_id, self.initial_query), ensure_ascii=False))
            self._initial_sent = True
            logger.info("sent initial message.send query=%r", self.initial_query)
            self.printer.line(f"[you] {self.initial_query}")
            self.turn_done.clear()

        if self._pending_reply is not None:
            await ws.send_str(json.dumps(self._pending_reply, ensure_ascii=False))
            logger.info("re-sent pending reply: %s", self._pending_reply.get("type"))
            self._pending_reply = None

    async def _interactive_loop(
        self, ws: aiohttp.ClientWebSocketResponse, http: aiohttp.ClientSession
    ) -> None:
        """并行跑两件事：
        1. ``_receive_loop``：消费 WS 出站事件，每轮结束时 set ``turn_done``；
        2. ``_user_input_loop``：等本轮结束 → prompt user → 发下一条 ``message.send``。
        任一任务结束即整体退出（要么 should_exit，要么 WS 断开）。
        """
        recv_task = asyncio.create_task(self._receive_loop(ws), name="vibeskill-recv")
        input_task = asyncio.create_task(self._user_input_loop(ws, http), name="vibeskill-input")
        try:
            done, pending = await asyncio.wait(
                {recv_task, input_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in pending:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    raise exc
        finally:
            if not ws.closed:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _user_input_loop(
        self, ws: aiohttp.ClientWebSocketResponse, http: aiohttp.ClientSession
    ) -> None:
        """轮询：等本轮结束 → 读用户输入 → 发下一条 message.send（或处理指令）。"""
        while not self.should_exit:
            await self.turn_done.wait()
            if self.should_exit:
                return
            if ws.closed:
                return

            try:
                raw = (await _ainput("\n>>> 输入下一条 query（/exit 退出，/cancel 中止当前任务）：")).strip()
            except EOFError:
                self.should_exit = True
                self.printer.line("[exit] EOF received, bye.")
                return

            if not raw:
                continue

            lowered = raw.lower()
            if lowered in (CMD_EXIT, CMD_QUIT):
                self.should_exit = True
                self.printer.line("[exit] user requested exit, bye.")
                return

            if lowered == CMD_CANCEL:
                self.printer.line(f"[cancel] POST /api/v1/session/{self.session_id}/abort")
                try:
                    await _abort_session(http, self.session_id)
                except aiohttp.ClientError as exc:
                    logger.warning("abort http error: %s", exc)
                # abort 后等 chat.interrupt_result 进来；turn_done 会由
                # session.status idle / task.completed 触发，无需主动 set。
                continue

            self.turn_done.clear()
            self.printer.line(f"[you] {raw}")
            await self._send_or_queue(ws, _build_message_send(self.session_id, raw))

    async def _receive_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        consecutive_timeouts = 0
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=RECV_TIMEOUT)
                consecutive_timeouts = 0
            except asyncio.TimeoutError:
                consecutive_timeouts += 1
                logger.warning(
                    "recv timeout %d/%d", consecutive_timeouts, MAX_CONSECUTIVE_TIMEOUTS
                )
                if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                    raise AssertionError(
                        f"stopped: too many consecutive recv timeouts "
                        f"(should_exit={self.should_exit})"
                    )
                continue

            if msg.type in (
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSE,
            ):
                logger.info("ws closed by server (code=%s)", msg.data)
                # 让 turn_done 触发一次，input loop 可以发现 ws.closed 并退出。
                self.turn_done.set()
                return
            if msg.type == aiohttp.WSMsgType.ERROR:
                logger.warning("ws error frame: %s", ws.exception())
                self.turn_done.set()
                return
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue

            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                logger.debug("non-json frame ignored: %r", msg.data[:200])
                continue

            await self._dispatch_event(ws, data)

    # ----------------- event dispatch -----------------

    async def _dispatch_event(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        data: dict[str, Any],
    ) -> None:
        mtype = str(data.get("type") or "")
        props = data.get("properties") if isinstance(data.get("properties"), dict) else {}

        if mtype == "session.updated":
            title = str(props.get("title") or "").strip()
            if title:
                self.printer.line(f"[session.updated] title={title}")
            return

        if mtype == "session.status":
            status = props.get("status") if isinstance(props.get("status"), dict) else {}
            stype = str(status.get("type") or "").strip()
            if stype == "idle":
                # Standard 模式下，idle 表示「Agent 已交还控制权，可以收下一条 user input」。
                self.turn_done.set()
            elif stype == "busy":
                logger.debug("session.status busy")
            elif stype == "completed":
                # Standard chat 通常不会进入 completed，但若收到也按结束处理。
                self.turn_done.set()
            return

        if mtype == "task.completed":
            self.printer.line("[task.completed]")
            self.turn_done.set()
            return

        if mtype == "task.error":
            err = str(props.get("error") or props.get("message") or "unknown")
            self.printer.line(f"[task.error] {err}")
            # 不抛错——Standard 多轮会话允许用户继续发起下一轮。
            self.turn_done.set()
            return

        if mtype == "todo.updated":
            self._print_todos(props.get("todos", []))
            return

        if mtype == "message.updated":
            # 仅作为 message 帧的存在性公告，逐 part 的渲染由 message.part.* 完成。
            return

        if mtype == "message.part.updated":
            self._handle_part_updated(props)
            return

        if mtype == "message.part.delta":
            self._handle_part_delta(props)
            return

        if mtype == "question.asked":
            await self._handle_question_asked(ws, props)
            return

        logger.debug("unhandled type=%s", mtype)

    # ----------------- display helpers -----------------

    def _print_todos(self, todos: Any) -> None:
        if not isinstance(todos, list) or not todos:
            return
        self.printer.line("[todos]")
        marks = {"completed": "x", "in_progress": "~", "cancelled": "-", "pending": " "}
        for todo in todos:
            if not isinstance(todo, dict):
                continue
            status = str(todo.get("status") or "pending")
            label = str(todo.get("label") or todo.get("content") or "").strip()
            mark = marks.get(status, "?")
            print(f"  [{mark}] {label}", flush=True)

    def _handle_part_updated(self, props: dict[str, Any]) -> None:
        part_type = str(props.get("type") or "").strip()
        part_id = str(props.get("partID") or props.get("id") or "").strip()
        if part_type in ("text", "reasoning"):
            text = str(props.get("text") or "")
            self.printer.stream_text(part_id, part_type, text)
            return
        if part_type == "tool":
            self._print_tool_part(part_id, props)
            return

    def _handle_part_delta(self, props: dict[str, Any]) -> None:
        part_type = str(props.get("type") or "").strip() or "text"
        part_id = str(props.get("partID") or props.get("id") or "").strip()
        text = str(props.get("text") or "")
        if not text:
            return
        if part_type in ("text", "reasoning"):
            self.printer.stream_text(part_id, part_type, text)

    def _print_tool_part(self, part_id: str, props: dict[str, Any]) -> None:
        tool_name = str(props.get("tool") or props.get("name") or "").strip() or "tool"
        state = props.get("state") if isinstance(props.get("state"), dict) else {}
        status = str(state.get("status") or "").strip()
        title = str(state.get("title") or "").strip()
        signature = f"{tool_name}::{status}"
        if self._tool_logged.get(part_id) == signature:
            return
        self._tool_logged[part_id] = signature
        line = f"[tool] {tool_name}"
        if status:
            line += f" [{status}]"
        if title:
            line += f" — {title}"
        self.printer.line(line)

        if status == "running":
            tool_input = state.get("input")
            if tool_input is not None:
                rendered = (
                    tool_input
                    if isinstance(tool_input, str)
                    else json.dumps(tool_input, ensure_ascii=False)
                )
                if len(rendered) > 400:
                    rendered = rendered[:400] + "...(truncated)"
                print(f"       input:  {rendered}", flush=True)

        if status in ("completed", "error"):
            output = state.get("output")
            if output is not None:
                rendered = (
                    output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
                )
                if len(rendered) > 400:
                    rendered = rendered[:400] + "...(truncated)"
                print(f"       output: {rendered}", flush=True)

    # ----------------- interactive replies -----------------

    async def _send_or_queue(
        self, ws: aiohttp.ClientWebSocketResponse, reply: dict[str, Any]
    ) -> None:
        try:
            if ws.closed:
                raise aiohttp.ClientError("ws already closed before send")
            await ws.send_str(json.dumps(reply, ensure_ascii=False))
            logger.info("sent %s", reply.get("type"))
        except (aiohttp.ClientError, ConnectionResetError, RuntimeError) as exc:
            logger.warning(
                "send %s failed (%s); queueing for reconnect",
                reply.get("type"),
                exc,
            )
            self._pending_reply = reply

    async def _handle_question_asked(
        self, ws: aiohttp.ClientWebSocketResponse, props: dict[str, Any]
    ) -> None:
        request_id = str(props.get("id") or "")
        questions = props.get("questions", []) if isinstance(props.get("questions"), list) else []
        self.printer.line()
        answers = await _prompt_question_answers(questions)
        await self._send_or_queue(
            ws, _build_question_reply(self.session_id, request_id, answers)
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "session_id",
        nargs="?",
        default="",
        help="已创建的 Standard session ID；留空将自动创建一个新 session",
    )
    parser.add_argument(
        "--query",
        default="",
        help=f"首条 message.send 的 query（默认: {DEFAULT_QUERY!r}）",
    )
    parser.add_argument(
        "--no-prompt-query",
        action="store_true",
        help="不在脚本开始时提示输入 query，直接使用 --query 或默认值",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="即使传入了 session_id 也忽略，强制创建新的 Standard session",
    )
    return parser.parse_args(argv)


async def _resolve_query(args: argparse.Namespace) -> str:
    query = (args.query or "").strip()
    if query:
        return query
    if args.no_prompt_query:
        return DEFAULT_QUERY
    try:
        entered = (
            await _ainput(f"请输入初始 query（回车使用默认 {DEFAULT_QUERY!r}）：")
        ).strip()
    except EOFError:
        entered = ""
    return entered or DEFAULT_QUERY


async def main(args: argparse.Namespace) -> None:
    async with aiohttp.ClientSession() as http:
        session_id = "" if args.create else (args.session_id or "").strip()
        if not session_id:
            session_id = await _create_session(http)
            print(f"[created Standard session] {session_id}", flush=True)

        query = await _resolve_query(args)
        runner = StandardSessionRunner(session_id, query)
        await runner.run(http)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    try:
        asyncio.run(main(_parse_args(sys.argv[1:])))
        logger.info("Done.")
    except AssertionError as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("interrupted by user")
        sys.exit(130)
    except Exception as exc:  # pragma: no cover - top-level debug aid
        logger.exception("unexpected error: %s", exc)
        sys.exit(1)
