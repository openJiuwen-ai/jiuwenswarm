import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass

import aiohttp

logger = logging.getLogger(__name__)

WS_BASE = os.environ.get(
    "VIBESKILL_WS_BASE",
    "ws://127.0.0.1:19003/api/v1/messages",
)


@dataclass
class ClientResult:
    session_id: str
    agent_id: str
    ok: bool
    done_at: float = 0.0
    package_seen: bool = False
    error: str = ""


def _build_auto_answers(questions: list[dict]) -> list[list[str]]:
    """为 question.asked 自动选择每题第一个选项标签。"""
    answers: list[list[str]] = []
    for q in questions:
        options = q.get("options", []) if isinstance(q, dict) else []
        first_label = ""
        if options and isinstance(options[0], dict):
            first_label = str(options[0].get("label") or "")
        answers.append([first_label] if first_label else [])
    return answers


async def _run_one_client(session_id: str, agent_id: str, query: str, timeout_seconds: int = 1200) -> ClientResult:
    uri = f"{WS_BASE}?sessionID={session_id}"
    start_at = time.time()
    logger.info("[client %s] connecting: %s", agent_id, uri)

    try:
        async with aiohttp.ClientSession() as client:
            async with client.ws_connect(uri) as ws:
                # 等待 server.connected
                ack = await asyncio.wait_for(ws.receive(), timeout=10)
                if ack.type != aiohttp.WSMsgType.TEXT:
                    return ClientResult(
                        session_id=session_id,
                        agent_id=agent_id,
                        ok=False,
                        error=f"unexpected ack type: {ack.type}",
                    )
                logger.info("[client %s] ack: %s", agent_id, ack.data)

                send_msg = {
                    "type": "message.send",
                    "sessionID": session_id,
                    "agent_id": agent_id,
                    "parts": [{"type": "text", "text": query}],
                    "model": {
                        "providerID": "llm_OpenAI",
                        "modelID": "deepseek-v3-250324",
                    },
                    "agent": "coder",
                }
                await ws.send_str(json.dumps(send_msg, ensure_ascii=False))
                logger.info("[client %s] message.send sent", agent_id)

                deadline = start_at + timeout_seconds
                package_in_progress_seen = False
                while True:
                    now = time.time()
                    if now >= deadline:
                        return ClientResult(
                            session_id=session_id,
                            agent_id=agent_id,
                            ok=False,
                            error=f"timeout waiting full skill flow after {timeout_seconds}s",
                        )
                    msg = await asyncio.wait_for(ws.receive(), timeout=max(1.0, deadline - now))
                    if msg.type == aiohttp.WSMsgType.CLOSED:
                        return ClientResult(
                            session_id=session_id,
                            agent_id=agent_id,
                            ok=False,
                            error="websocket closed before skill flow completed",
                        )
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        logger.debug("[client %s] recv ws frame type=%s (non-text, skip)", agent_id, msg.type)
                        continue
                    data = json.loads(msg.data)
                    msg_type = str(data.get("type") or "")
                    props = data.get("properties", {}) if isinstance(data.get("properties"), dict) else {}

                    if msg_type in ("server.heartbeat"):
                        continue

                    logger.info("[client %s] recv event type=%s", agent_id, msg_type)

                    if msg_type == "todo.updated":
                        todos = props.get("todos", [])
                        if isinstance(todos, list):
                            for todo in todos:
                                if not isinstance(todo, dict):
                                    continue
                                todo_id = str(todo.get("id") or "").strip()
                                todo_status = str(todo.get("status") or "").strip()
                                if todo_id == "package" and todo_status == "completed":
                                    package_in_progress_seen = True
                                    break
                        continue

                    if msg_type == "session.status":
                        status = props.get("status", {}) if isinstance(props.get("status"), dict) else {}
                        status_type = str(status.get("type") or "").strip()
                        if package_in_progress_seen and status_type == "idle":
                            return ClientResult(
                                session_id=session_id,
                                agent_id=agent_id,
                                ok=True,
                                done_at=time.time(),
                                package_seen=True,
                            )
                        continue

                    # SkillDev 澄清问题：自动回填答案，继续流程
                    if msg_type == "question.asked":
                        qid = str(props.get("id") or "")
                        questions = props.get("questions", [])
                        answers = _build_auto_answers(questions if isinstance(questions, list) else [])
                        reply_msg = {
                            "type": "question.replied",
                            "properties": {
                                "sessionID": session_id,
                                "requestID": qid,
                                "answers": answers,
                            },
                        }
                        await ws.send_str(json.dumps(reply_msg, ensure_ascii=False))
                        logger.info("[client %s] question.replied sent requestID=%s", agent_id, qid)
                        continue

                    if msg_type == "review.asked":
                        rid = str(props.get("id") or "")
                        reply_msg = {
                            "type": "review.replied",
                            "properties": {
                                "id": rid,
                                "sessionID": session_id,
                                "accept": True,
                            },
                        }
                        await ws.send_str(json.dumps(reply_msg, ensure_ascii=False))
                        logger.info("[client %s] review.replied sent id=%s accept=true", agent_id, rid)
                        continue
    except Exception as exc:
        return ClientResult(
            session_id=session_id,
            agent_id=agent_id,
            ok=False,
            error=str(exc),
        )


async def main() -> int:
    if len(sys.argv) < 3:
        logger.error(
            "Usage: uv run python scripts/vibeskill/test_ws_multi_tenant_concurrent.py <session_id_a> <session_id_b>"
        )
        return 2

    session_a = sys.argv[1].strip()
    session_b = sys.argv[2].strip()
    if not session_a or not session_b:
        logger.error("session ids must be non-empty")
        return 2
    if session_a == session_b:
        logger.error("session ids must be different")
        return 2

    query_a = "请为计算大数字乘法生成一个 skill"
    query_b = "请为生成文章摘要生成一个skill"

    started_at = time.time()
    result_a, result_b = await asyncio.gather(
        _run_one_client(session_a, "tenant-A", query_a),
        _run_one_client(session_b, "tenant-B", query_b),
    )
    elapsed = time.time() - started_at

    results = (result_a, result_b)
    logger.info("========== 并发全流程结果（两个 skill 生成） ==========")
    for result in results:
        if result.ok:
            logger.info(
                "[OK] session=%s agent_id=%s package_seen=%s done_at=%.3f",
                result.session_id,
                result.agent_id,
                result.package_seen,
                result.done_at,
            )
        else:
            logger.info(
                "[FAIL] session=%s agent_id=%s error=%s",
                result.session_id,
                result.agent_id,
                result.error,
            )
    logger.info("elapsed=%.2fs", elapsed)

    if not all(r.ok for r in results):
        return 1

    done_times = [r.done_at for r in results]
    spread = max(done_times) - min(done_times)
    logger.info("completion-spread(max-min)=%.2fs", spread)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    raise SystemExit(asyncio.run(main()))
