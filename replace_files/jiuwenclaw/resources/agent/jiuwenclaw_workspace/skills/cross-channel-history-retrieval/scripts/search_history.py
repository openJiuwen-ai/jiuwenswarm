from __future__ import annotations

import argparse
import json
import locale
import logging
import re
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

# 固定首行标记：在 mcp_exec_command 回显或日志里 grep `SKILL=cross-channel-history-retrieval` 即可确认本脚本被运行。
_SKILL_ID = "cross-channel-history-retrieval"

_REPORT_LOG = logging.getLogger("jiuwenclaw.skill.cross_channel_history_retrieval")


def _configure_report_logging() -> None:
    """仅输出纯文本行（与原先 print 一致），默认写到 stderr，供 mcp_exec_command 一并捕获。"""
    if _REPORT_LOG.handlers:
        return
    _REPORT_LOG.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _REPORT_LOG.addHandler(handler)
    _REPORT_LOG.propagate = False


def _report_line(s: str = "") -> None:
    _REPORT_LOG.info("%s", s)


def _align_stdio_for_pipe_capture() -> None:
    """非 TTY（如 mcp_exec_command 管道捕获）时，按系统首选编码写出 stdout/stderr。

    与 agentserver `mcp_exec_command` 的 `locale.getpreferredencoding(False)` 解码一致，
    避免子进程默认 UTF-8 与父进程按 GBK/CP936 解码产生乱码。交互终端不改动，以免干扰本机 UTF-8 控制台。
    """
    if sys.stdout.isatty():
        return
    enc = locale.getpreferredencoding(False) or "utf-8"
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        try:
            reconf(encoding=enc, errors="replace")
        except Exception:
            warnings.warn(
                f"Failed to reconfigure stream to {enc!r}; output encoding may mismatch the parent process.",
                UserWarning,
                stacklevel=1,
            )


@dataclass
class Hit:
    session_id: str
    role: str
    channel_id: str
    timestamp: float
    content: str
    request_id: str


def _default_sessions_root() -> Path:
    home = Path.home()
    return home / ".jiuwenclaw" / "agent" / "sessions"


def _to_float_ts(v: Any, tz: timezone) -> float | None:
    if v is None:
        return None

    if isinstance(v, (int, float)):
        f = float(v)
        if f > 1e12:
            return f / 1000.0
        return f

    s = str(v).strip()
    if not s:
        return None

    if re.fullmatch(r"\d{13,}", s):
        return float(s) / 1000.0
    if re.fullmatch(r"\d{10,12}", s):
        return float(s)

    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.timestamp()
    except Exception:
        return None


def _parse_user_dt(s: str, tz: timezone) -> datetime:
    s = s.strip()
    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=tz)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except Exception as exc:
        raise ValueError(f"无法解析时间: {s}") from exc


def _read_history_file(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            out.append(item)
    return out


def _build_keywords(args: argparse.Namespace) -> list[str]:
    kws: list[str] = []
    for k in args.keyword or []:
        kk = str(k).strip()
        if kk:
            kws.append(kk)
    q = str(args.query or "").strip()
    if q:
        kws.extend([p for p in q.split() if p.strip()])
    seen: set[str] = set()
    uniq: list[str] = []
    for k in kws:
        lk = k.lower()
        if lk in seen:
            continue
        seen.add(lk)
        uniq.append(k)
    return uniq


def _match_keywords(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return True
    lt = text.lower()
    return all(k.lower() in lt for k in keywords)


def _iter_session_dirs(sessions_root: Path, max_sessions: int) -> list[Path]:
    if not sessions_root.exists():
        return []
    dirs = [p for p in sessions_root.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dirs[: max(1, int(max_sessions))]


def _session_match(session_id: str, channel: str | None, exact_session_id: str | None) -> bool:
    if exact_session_id:
        return session_id == exact_session_id
    if channel:
        return session_id.startswith(f"{channel}_") or session_id == channel
    return True


def _search_once(
    *,
    sessions_root: Path,
    channel: str | None,
    session_id: str | None,
    start_ts: float,
    end_ts: float,
    keywords: list[str],
    limit: int,
    max_sessions: int,
    tz: timezone,
) -> tuple[list[Hit], int, int]:
    hits: list[Hit] = []
    scanned_sessions = 0
    scanned_records = 0

    for d in _iter_session_dirs(sessions_root, max_sessions=max_sessions):
        sid = d.name
        if not _session_match(sid, channel=channel, exact_session_id=session_id):
            continue

        scanned_sessions += 1
        f = d / "history.json"
        if not f.exists():
            continue
        items = _read_history_file(f)
        for it in items:
            scanned_records += 1
            ts = _to_float_ts(it.get("timestamp"), tz=tz)
            if ts is None or ts < start_ts or ts > end_ts:
                continue
            content = str(it.get("content") or "")
            if not content.strip():
                continue
            if not _match_keywords(content, keywords):
                continue
            hits.append(
                Hit(
                    session_id=sid,
                    role=str(it.get("role") or "unknown"),
                    channel_id=str(it.get("channel_id") or ""),
                    timestamp=ts,
                    content=content.strip(),
                    request_id=str(it.get("request_id") or ""),
                )
            )

    hits.sort(key=lambda h: h.timestamp, reverse=True)
    return hits[:limit], scanned_sessions, scanned_records


def _fmt_iso(ts: float, tz: timezone) -> str:
    return datetime.fromtimestamp(ts, tz=tz).isoformat()


def _emit_search_report(
    *,
    hits: list[Hit],
    scanned_sessions: int,
    scanned_records: int,
    start_ts: float,
    end_ts: float,
    keywords: list[str],
    channel: str | None,
    session_id: str | None,
    timezone_name: str,
    auto_expanded: bool,
    tz: timezone,
) -> None:
    _report_line(f"SKILL={_SKILL_ID}")
    _report_line("HISTORY_SEARCH_SUMMARY_BEGIN")
    _report_line(f"timezone={timezone_name}")
    _report_line(f"start={_fmt_iso(start_ts, tz)}")
    _report_line(f"end={_fmt_iso(end_ts, tz)}")
    _report_line(f"channel={channel or '-'}")
    _report_line(f"session_id={session_id or '-'}")
    _report_line(f"keywords={','.join(keywords) if keywords else '-'}")
    _report_line(f"scanned_sessions={scanned_sessions}")
    _report_line(f"scanned_records={scanned_records}")
    _report_line(f"hit_count={len(hits)}")
    _report_line(f"auto_expanded={str(auto_expanded).lower()}")
    _report_line("HISTORY_SEARCH_SUMMARY_END")
    _report_line()

    _report_line("HISTORY_CONTEXT_BLOCK_BEGIN")
    if not hits:
        _report_line("未命中任何历史记录。")
    else:
        for idx, h in enumerate(hits, start=1):
            _report_line(
                f"[{idx}] time={_fmt_iso(h.timestamp, tz)} session={h.session_id} "
                f"role={h.role} channel={h.channel_id or '-'} request_id={h.request_id or '-'}"
            )
            _report_line(h.content)
            _report_line("---")
    _report_line("HISTORY_CONTEXT_BLOCK_END")


def main() -> int:
    _align_stdio_for_pipe_capture()
    _configure_report_logging()
    parser = argparse.ArgumentParser(description="Search session history.json by channel/session/time/keywords.")
    parser.add_argument(
        "--sessions-root",
        type=str,
        default="",
        help="Path to sessions root. Default: ~/.jiuwenclaw/agent/sessions",
    )
    parser.add_argument("--channel", type=str, default="", help="Filter by channel prefix in session_id, e.g. feishu")
    parser.add_argument("--session-id", type=str, default="", help="Exact session_id")
    parser.add_argument("--query", type=str, default="", help="Space-separated keywords, all must match")
    parser.add_argument("--keyword", action="append", default=[], help="Exact keyword, can be passed multiple times")
    parser.add_argument("--start", type=str, default="", help="Start time")
    parser.add_argument("--end", type=str, default="", help="End time")
    parser.add_argument("--at", type=str, default="", help="Center time")
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=120,
        help="Time window minutes for --at or default search",
    )
    parser.add_argument("--timezone", type=str, default="Asia/Shanghai", help="Timezone name")
    parser.add_argument("--limit", type=int, default=20, help="Max hits to emit in the report")
    parser.add_argument("--max-sessions", type=int, default=200, help="Max session folders to scan")
    parser.add_argument("--auto-expand", action="store_true", default=True, help="Auto expand to 72h if no hits")
    parser.add_argument("--no-auto-expand", dest="auto_expand", action="store_false", help="Disable auto expansion")
    args = parser.parse_args()

    if ZoneInfo is not None:
        tz = ZoneInfo(args.timezone)
    else:
        tz = timezone(timedelta(hours=8))

    now = datetime.now(tz=tz)
    window_minutes = max(1, int(args.window_minutes))

    start_ts: float
    end_ts: float
    if args.start or args.end:
        start_dt = _parse_user_dt(args.start, tz) if args.start else (now - timedelta(hours=24))
        end_dt = _parse_user_dt(args.end, tz) if args.end else now
        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()
    elif args.at:
        center = _parse_user_dt(args.at, tz)
        half = timedelta(minutes=window_minutes / 2)
        start_ts = (center - half).timestamp()
        end_ts = (center + half).timestamp()
    else:
        start_ts = (now - timedelta(hours=24)).timestamp()
        end_ts = now.timestamp()

    if start_ts > end_ts:
        start_ts, end_ts = end_ts, start_ts

    sessions_root = (
        Path(args.sessions_root).expanduser()
        if str(args.sessions_root).strip()
        else _default_sessions_root()
    )
    channel = str(args.channel or "").strip() or None
    session_id = str(args.session_id or "").strip() or None
    keywords = _build_keywords(args)
    limit = max(1, int(args.limit))
    max_sessions = max(1, int(args.max_sessions))

    hits, scanned_sessions, scanned_records = _search_once(
        sessions_root=sessions_root,
        channel=channel,
        session_id=session_id,
        start_ts=start_ts,
        end_ts=end_ts,
        keywords=keywords,
        limit=limit,
        max_sessions=max_sessions,
        tz=tz,
    )

    auto_expanded = False
    if not hits and args.auto_expand:
        if not args.start and not args.end:
            auto_expanded = True
            start_ts = (now - timedelta(hours=72)).timestamp()
            end_ts = now.timestamp()
            hits, scanned_sessions, scanned_records = _search_once(
                sessions_root=sessions_root,
                channel=channel,
                session_id=session_id,
                start_ts=start_ts,
                end_ts=end_ts,
                keywords=keywords,
                limit=limit,
                max_sessions=max_sessions,
                tz=tz,
            )

    _emit_search_report(
        hits=hits,
        scanned_sessions=scanned_sessions,
        scanned_records=scanned_records,
        start_ts=start_ts,
        end_ts=end_ts,
        keywords=keywords,
        channel=channel,
        session_id=session_id,
        timezone_name=args.timezone,
        auto_expanded=auto_expanded,
        tz=tz,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
