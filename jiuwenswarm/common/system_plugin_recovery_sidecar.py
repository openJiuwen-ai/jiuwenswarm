# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loopback-only emergency page for restoring system plugin states."""

from __future__ import annotations

import argparse
import html
import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from jiuwenswarm.common.system_plugin_recovery import (
    RecoveryRecordNotFound,
    SystemPluginRecovery,
    begin_system_plugin_recovery_session,
)

_PATH = "/system-plugin-recovery"
_MAX_BODY_BYTES = 16 * 1024
_PAGE_STYLE = """
:root {
  color-scheme: light dark;
  --background: #f7f8fa;
  --card: #fff;
  --text: #1f2329;
  --muted: #646a73;
  --line: #dfe1e6;
  --primary: #3370ff;
  --danger: #d83931;
}
@media (prefers-color-scheme: dark) {
  :root {
    --background: #17191c;
    --card: #24272b;
    --text: #f3f4f5;
    --muted: #a6abb4;
    --line: #3b4047;
    --primary: #5b8cff;
    --danger: #f26d65;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--text);
  background: var(--background);
  font: 14px/1.57 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main {
  width: min(920px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 40px 0 72px;
}
h1 { margin: 0 0 8px; font-size: 24px; line-height: 32px; }
p { margin: 0; color: var(--muted); }
.description { display: grid; gap: 4px; margin-bottom: 24px; }
#status { min-height: 22px; margin-bottom: 16px; color: var(--muted); }
ul {
  display: grid;
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}
li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--card);
}
.copy { min-width: 0; }
.title { color: var(--text); font-weight: 600; }
.time { margin-top: 3px; color: var(--muted); font-size: 12px; }
button {
  flex: 0 0 auto;
  min-width: 68px;
  height: 32px;
  padding: 0 14px;
  color: #fff;
  border: 0;
  border-radius: 6px;
  background: var(--primary);
  cursor: pointer;
}
button:disabled { opacity: .56; cursor: not-allowed; }
dialog {
  width: min(420px, calc(100vw - 32px));
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--card);
  box-shadow: 0 12px 36px rgb(0 0 0 / 24%);
}
dialog::backdrop { background: rgb(0 0 0 / 42%); }
dialog h2 { margin: 0 0 8px; font-size: 16px; }
dialog p { margin-bottom: 20px; }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; }
.dialog-actions .secondary { color: var(--text); border: 1px solid var(--line); background: transparent; }
.dialog-actions .danger { background: var(--danger); }
"""
_PAGE_SCRIPT = """
<script>
const token = __TOKEN__;
const endpoint = "__ENDPOINT__";
const texts = __TEXTS__;
const status = document.querySelector('#status');
const records = document.querySelector('#records');
const confirmDialog = document.querySelector('#confirm-dialog');
const confirmTitle = document.querySelector('#confirm-title');
const confirmDescription = document.querySelector('#confirm-description');
const confirmCancel = document.querySelector('#confirm-cancel');
const confirmRestoreButton = document.querySelector('#confirm-restore');
let pendingRestore = null;

function recordTitle(record) {
  const names = record.package_ids?.join(', ');
  const action = texts.actions[record.action] || record.action;
  return names ? `${action}: ${names}` : action;
}

async function load() {
  const query = encodeURIComponent(token);
  const response = await fetch(`${endpoint}/api/history?token=${query}`);
  if (!response.ok) throw new Error('load failed');
  const data = await response.json();
  records.replaceChildren(...data.records.map((record) => row(record)));
}

function row(record) {
  const item = document.createElement('li');
  const copy = document.createElement('div');
  const name = document.createElement('div');
  const time = document.createElement('div');
  const button = document.createElement('button');
  copy.className = 'copy';
  name.className = 'title';
  time.className = 'time';
  name.textContent = recordTitle(record);
  time.textContent = new Date(record.timestamp).toLocaleString(texts.locale);
  button.textContent = texts.restore;
  button.onclick = () => confirmRestore(record.id, button);
  copy.append(name, time);
  item.append(copy, button);
  return item;
}

function confirmRestore(id, button) {
  pendingRestore = { id, button };
  confirmTitle.textContent = texts.restoreConfirmTitle;
  confirmDescription.textContent = texts.restoreConfirmDescription;
  confirmCancel.textContent = texts.cancel;
  confirmRestoreButton.textContent = texts.restoreAndRestart;
  confirmDialog.showModal();
}

confirmCancel.onclick = () => confirmDialog.close();
confirmDialog.addEventListener('cancel', () => { pendingRestore = null; });
confirmRestoreButton.onclick = () => {
  const request = pendingRestore;
  pendingRestore = null;
  confirmDialog.close();
  if (request) void restore(request.id, request.button);
};

async function restore(id, button) {
  button.disabled = true;
  status.textContent = texts.restoring;
  try {
    const query = encodeURIComponent(token);
    const response = await fetch(`${endpoint}/api/restore?token=${query}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ record_id: id }),
    });
    if (!response.ok) throw new Error('restore failed');
    status.textContent = texts.restored;
    await load();
  } catch {
    status.textContent = texts.restoreFailed;
    button.disabled = false;
  }
}

load().catch(() => {
  status.textContent = texts.loadFailed;
});
</script>
"""


class _RecoveryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], recovery: SystemPluginRecovery, token: str) -> None:
        super().__init__(address, _RecoveryHandler)
        self.recovery = recovery
        self.token = token


class _RecoveryHandler(BaseHTTPRequestHandler):
    server: _RecoveryServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path, query = _request_target(self.path)
        if not self._authorized(query):
            self._forbidden()
            return
        if path in {_PATH, f"{_PATH}/"}:
            locale = query.get("locale", [""])[0]
            self._html(_page(self.server.token, locale))
            return
        if path == f"{_PATH}/api/history":
            self._json({"records": self.server.recovery.records()})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path, query = _request_target(self.path)
        if not self._authorized(query):
            self._forbidden()
            return
        if path != f"{_PATH}/api/restore":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = self._body()
        record_id = body.get("record_id") if isinstance(body, dict) else None
        if not isinstance(record_id, str):
            self._json({"error": "record_id is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            record = self.server.recovery.restore(record_id)
        except RecoveryRecordNotFound:
            self._json({"error": "record not found"}, status=HTTPStatus.NOT_FOUND)
            return
        except (OSError, ValueError):
            self._json({"error": "recovery could not update managed packages"}, status=HTTPStatus.CONFLICT)
            return
        self._json({"record": record, "restart_requested": True})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - standard library signature
        del format, args

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        values = query.get("token")
        if values is None or len(values) != 1:
            return False
        try:
            supplied = values[0].encode("utf-8")
            expected = self.server.token.encode("ascii")
        except UnicodeError:
            return False
        return secrets.compare_digest(supplied, expected)

    def _body(self) -> object:
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError:
            return None
        if length < 0 or length > _MAX_BODY_BYTES:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return None

    def _forbidden(self) -> None:
        self.send_error(HTTPStatus.FORBIDDEN)

    def _json(self, payload: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _html(self, value: str) -> None:
        encoded = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the system plugin recovery page.")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    if args.port < 0 or args.port > 65535:
        raise SystemExit("--port must be between 0 and 65535")
    session_id = begin_system_plugin_recovery_session()
    recovery = SystemPluginRecovery()
    token = secrets.token_urlsafe(32)
    server = _RecoveryServer(("127.0.0.1", args.port), recovery, token)
    port = server.server_address[1]
    recovery.publish_endpoint(f"http://127.0.0.1:{port}{_PATH}?token={token}", session_id)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def _request_target(value: str) -> tuple[str, dict[str, list[str]]]:
    parsed = urlparse(value)
    return parsed.path, parse_qs(parsed.query, keep_blank_values=True)


def _page(token: str, locale: str) -> str:
    texts = _texts(locale)
    script = _PAGE_SCRIPT.replace("__TOKEN__", json.dumps(token))
    script = script.replace("__ENDPOINT__", _PATH)
    script = script.replace("__TEXTS__", json.dumps(texts, ensure_ascii=False))
    return f"""<!doctype html>
<html lang="{texts['locale']}">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(texts['title'])}</title>
<style>
{_PAGE_STYLE}
</style>
<main>
  <h1>{html.escape(texts['title'])}</h1>
  <div class="description">
    <p>{html.escape(texts['description'])}</p>
    <p>{html.escape(texts['restartNote'])}</p>
  </div>
  <div id="status"></div>
  <ul id="records"></ul>
</main>
<dialog id="confirm-dialog">
  <h2 id="confirm-title"></h2>
  <p id="confirm-description"></p>
  <div class="dialog-actions">
    <button id="confirm-cancel" class="secondary" type="button"></button>
    <button id="confirm-restore" class="danger" type="button"></button>
  </div>
</dialog>
{script}"""


def _texts(locale: str) -> dict[str, object]:
    if locale.lower().startswith("en"):
        return {
            "locale": "en",
            "title": "System plugin recovery",
            "description": (
                "View the latest 50 system plugin state records. Every application startup and each "
                "successful install, uninstall, enable, disable, and restore operation is recorded."
            ),
            "restartNote": (
                "Restore keeps the system plugins currently installed, restores the system plugin enabled "
                "states at the selected time, and automatically restarts the background services."
            ),
            "restore": "Restore",
            "cancel": "Cancel",
            "restoreConfirmTitle": "Restore system plugins?",
            "restoreConfirmDescription": (
                "Restoring will recover the system plugin enabled states at this point and restart the "
                "background services. Tasks in progress will be interrupted."
            ),
            "restoreAndRestart": "Restore and restart",
            "restoring": "Restoring…",
            "restored": "Restored. Restarting background services.",
            "restoreFailed": "Restore failed.",
            "loadFailed": "Could not load recovery records.",
            "actions": {
                "startup": "Application started",
                "install": "Installed",
                "uninstall": "Uninstalled",
                "enable": "Enabled",
                "disable": "Disabled",
                "restore": "Restored",
            },
        }
    return {
        "locale": "zh-CN",
        "title": "系统插件还原",
        "description": "查看最近 50 条系统插件状态记录。每次应用启动及成功的安装、卸载、启用、禁用和还原操作都会记录。",
        "restartNote": "还原会保留当前已安装的系统插件，恢复所选时刻的系统插件启用状态，并自动重启后台服务。",
        "restore": "还原",
        "cancel": "取消",
        "restoreConfirmTitle": "确认还原系统插件？",
        "restoreConfirmDescription": "还原将恢复此刻对应的系统插件启用状态，并重启后台，正在处理的任务将中断。",
        "restoreAndRestart": "还原并重启",
        "restoring": "正在还原…",
        "restored": "已还原，正在重启后台服务。",
        "restoreFailed": "还原失败。",
        "loadFailed": "无法读取还原记录。",
        "actions": {
            "startup": "程序启动",
            "install": "安装",
            "uninstall": "卸载",
            "enable": "启用",
            "disable": "禁用",
            "restore": "还原",
        },
    }


if __name__ == "__main__":
    main()
