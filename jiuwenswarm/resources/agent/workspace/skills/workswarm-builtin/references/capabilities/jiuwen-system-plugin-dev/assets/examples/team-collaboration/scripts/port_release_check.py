"""端口生命周期取证工具（契约 §5.5 R8 模式 A：≤5s 无 LISTEN）。

三个能力：
1. `snapshot`：全量监听集快照（`netstat -ano` 解析，用于"泄漏比对"基线）。
2. `watch`：对给定端口做 TCP connect 探针，每 100ms 一次，记录"首次不可连接"的耗时（毫秒）。
3. `deadport`：对 iframe src 里的 URL 逐一发请求，判定是否存在"页签指向死端口"。

用法：
    python port_release_check.py snapshot --json-out before.json
    python port_release_check.py watch --ports 52301 --timeout 15 --json-out release.json
    python port_release_check.py deadport --url http://127.0.0.1:52301/web/team-info.html?sid=x

退出码：watch 在 --require-free 时限内未释放 → 1。
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

NETSTAT_LISTEN = re.compile(
    r"^\s*(TCP|TCPv6)\s+(\S+):(\d+)\s+(\S+):(\d+)\s+LISTENING\s+(\d+)\s*$",
    re.IGNORECASE,
)


def listen_snapshot() -> list[dict]:
    """全量 LISTENING 端口（Windows netstat -ano）。"""
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=30).stdout
    rows: list[dict] = []
    for line in out.splitlines():
        m = NETSTAT_LISTEN.match(line)
        if not m:
            continue
        rows.append({
            "proto": m.group(1).upper(),
            "local_addr": m.group(2),
            "port": int(m.group(3)),
            "remote_addr": m.group(4),
            "remote_port": int(m.group(5)),
            "pid": int(m.group(6)),
        })
    rows.sort(key=lambda r: (r["port"], r["pid"]))
    return rows


def _port_set(rows: list[dict]) -> set[tuple[int, int]]:
    return {(r["port"], r["pid"]) for r in rows}


def diff(before: list[dict], after: list[dict]) -> dict:
    b, a = _port_set(before), _port_set(after)
    return {
        "added": sorted(a - b),
        "removed": sorted(b - a),
    }


def is_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """TCP connect 探针：能建立连接即视为有监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def watch_ports(ports: list[int], limit: float = 15.0, interval: float = 0.1) -> dict:
    """轮询端口占用，返回每个端口「首次不可连接」的耗时（ms）。"""
    started = time.perf_counter()
    pending = list(dict.fromkeys(ports))
    freed: dict[str, float] = {}
    timeline: list[dict] = []
    while pending and (time.perf_counter() - started) <= limit:
        now_ms = round((time.perf_counter() - started) * 1000, 1)
        still: list[int] = []
        for port in pending:
            if is_listening(port):
                still.append(port)
            else:
                freed[str(port)] = now_ms
        timeline.append({"t_ms": now_ms, "listening": still})
        pending = still
        if pending:
            time.sleep(interval)
    for port in pending:
        freed[str(port)] = None  # 超时仍未释放
    return {
        "ports": [str(p) for p in ports],
        "limit_s": limit,
        "interval_s": interval,
        "freed_at_ms": freed,
        "all_freed_within_limit": all(v is not None for v in freed.values()),
        "max_release_ms": max([v for v in freed.values() if v is not None], default=None),
        "timeline": timeline,
    }


def deadport_probe(url: str, timeout: float = 5.0) -> dict:
    started = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"url": url, "ok": resp.status == 200, "status": resp.status,
                    "content_type": resp.headers.get("Content-Type"),
                    "bytes": len(resp.read()),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
    except urllib.error.HTTPError as exc:
        return {"url": url, "ok": False, "status": exc.code,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:  # noqa: BLE001  连接拒绝/超时都算死端口
        return {"url": url, "ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("snapshot")
    p1.add_argument("--json-out", default="")

    p2 = sub.add_parser("watch")
    p2.add_argument("--ports", required=True, help="逗号分隔端口")
    p2.add_argument("--timeout", type=float, default=15.0)
    p2.add_argument("--interval", type=float, default=0.1)
    p2.add_argument("--require-free-within", type=float, default=5.0,
                    help="契约 R8 模式 A 硬线：5 秒")
    p2.add_argument("--json-out", default="")

    p3 = sub.add_parser("deadport")
    p3.add_argument("--url", action="append", required=True)
    p3.add_argument("--json-out", default="")

    args = ap.parse_args()
    if args.cmd == "snapshot":
        data = listen_snapshot()
        payload = {"count": len(data), "rows": data}
        print(json.dumps({"count": len(data), "sample": data[:20]}, ensure_ascii=False, indent=2))
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        return 0

    if args.cmd == "watch":
        ports = [int(p.strip()) for p in args.ports.split(",") if p.strip()]
        result = watch_ports(ports, limit=args.timeout, interval=args.interval)
        ok = result["all_freed_within_limit"] and (
            result["max_release_ms"] is not None and result["max_release_ms"] <= args.require_free_within * 1000
        )
        result["require_free_within_s"] = args.require_free_within
        result["verdict"] = "PASS" if ok else "FAIL"
        print(json.dumps({k: v for k, v in result.items() if k != "timeline"}, ensure_ascii=False, indent=2))
        if args.json_out:
            with open(args.json_out, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False, indent=2)
        return 0 if ok else 1

    probes = [deadport_probe(u) for u in args.url]
    ok = all(p["ok"] for p in probes)
    print(json.dumps({"ok": ok, "probes": probes}, ensure_ascii=False, indent=2))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"ok": ok, "probes": probes}, fh, ensure_ascii=False, indent=2)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
