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
  --card: #fff;
  --text: #1f2329;
  --muted: #646a73;
  --line: #dfe1e6;
  --primary: #3370ff;
  --danger: #d83931;
  --disabled-background: #e8eaed;
  --disabled-text: #8f959e;
  --record-hover: #f1f5ff;
  --record-open: #e8efff;
  --package-background: #f7f8fa;
}
@media (prefers-color-scheme: dark) {
  :root {
    --card: #24272b;
    --text: #f3f4f5;
    --muted: #a6abb4;
    --line: #3b4047;
    --primary: #5b8cff;
    --danger: #f26d65;
    --disabled-background: #3b4047;
    --disabled-text: #a6abb4;
    --record-hover: #202b40;
    --record-open: #253354;
    --package-background: #1b1f25;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--text);
  background: transparent;
  font: 400 14px/1.55 "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  letter-spacing: -0.02em;
}
main {
  width: min(920px, calc(100vw - 32px));
  margin: 0 auto;
  padding: 40px 0 72px;
}
h1 { margin: 0 0 8px; font-size: 24px; line-height: 32px; }
p { margin: 0; color: var(--muted); }
.page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 16px;
}
.page-copy { min-width: 0; }
.description { margin: 0; }
#status { margin-bottom: 16px; color: var(--muted); }
#status:empty { display: none; }
ul {
  display: grid;
  gap: 12px;
  margin: 0;
  padding: 0;
  list-style: none;
}
li {
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--card);
  overflow: hidden;
}
.record-header { display: flex; align-items: stretch; }
.record-selector {
  flex: 0 0 auto;
  align-self: center;
  width: 16px;
  height: 16px;
  margin: 0 0 0 16px;
  accent-color: var(--primary);
  cursor: pointer;
}
.record-selector:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.record-toggle {
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: auto;
  min-width: 0;
  min-height: 68px;
  padding: 16px 16px 16px 12px;
  color: var(--text);
  border-radius: 0;
  background: transparent;
  text-align: left;
}
.record-toggle:hover { background: var(--record-hover); }
.record-toggle:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
.record-toggle[aria-expanded="true"] { background: var(--record-open); }
.copy { min-width: 0; }
.title { color: var(--text); font-weight: 600; }
.time { margin-top: 3px; color: var(--muted); font-size: 12px; }
.chevron {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  width: 32px;
  height: 32px;
  margin-left: 16px;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 50%;
  background: var(--package-background);
  transition: transform .16s ease;
}
.chevron::before {
  width: 9px;
  height: 9px;
  border-right: 2px solid currentColor;
  border-bottom: 2px solid currentColor;
  content: '';
  transform: rotate(45deg) translate(-1px, -1px);
}
.record-toggle:hover .chevron { color: var(--primary); border-color: var(--primary); }
.record-toggle[aria-expanded="true"] .chevron { transform: rotate(180deg); }
.record-detail {
  display: none;
  padding: 16px;
  border-top: 1px solid var(--line);
}
.record-detail.is-open {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: 20px;
}
.record-detail-copy { min-width: 0; }
.record-detail-title { margin: 0 0 10px; color: var(--text); font-size: 13px; font-weight: 600; }
.package-state-list { display: flex; flex-wrap: wrap; gap: 8px; }
.package-state {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  padding: 6px 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
  color: var(--text);
  background: var(--package-background);
  font-size: 12px;
}
.package-name { overflow-wrap: anywhere; }
.package-status { flex: 0 0 auto; color: var(--primary); font-weight: 600; }
.package-status.is-disabled { color: var(--muted); }
.empty-state { color: var(--muted); font-size: 13px; }
button {
  flex: 0 0 auto;
  min-width: 68px;
  height: 32px;
  padding: 0 14px;
  color: #fff;
  font: inherit;
  border: 0;
  border-radius: 6px;
  background: var(--primary);
  cursor: pointer;
}
button:disabled {
  color: var(--disabled-text);
  background: var(--disabled-background);
  cursor: not-allowed;
}
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
@media (max-width: 560px) {
  main { width: min(100% - 24px, 920px); padding-top: 28px; }
  .page-header { align-items: stretch; flex-direction: column; gap: 14px; }
  .page-header > button { align-self: flex-start; }
  .record-detail.is-open { grid-template-columns: 1fr; gap: 14px; }
  .record-detail > button { width: 100%; }
}
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
const restoreButton = document.querySelector('#restore');
const page = document.querySelector('main');
let pendingRestore = null;
let expandedRow = null;
let previewRequestId = 0;
let selectedRecordId = null;
let heightAnimationFrame = null;

function reportContentHeight() {
  heightAnimationFrame = null;
  const height = Math.ceil(page.getBoundingClientRect().height);
  if (height > 0) {
    window.parent.postMessage({ type: 'jiuwenswarm.system-plugin-recovery.height', height }, '*');
  }
}

function scheduleContentHeight() {
  if (heightAnimationFrame === null) {
    heightAnimationFrame = window.requestAnimationFrame(reportContentHeight);
  }
}

if ('ResizeObserver' in window) {
  new ResizeObserver(scheduleContentHeight).observe(page);
}
window.addEventListener('load', scheduleContentHeight);

function recordAction(record) {
  const names = record.package_ids?.join(', ');
  const action = texts.actions[record.action] || record.action;
  return names ? `${action}${texts.labelSeparator}${names}` : action;
}

function recordTitle(record) {
  const time = new Date(record.timestamp).toLocaleString(texts.locale);
  return `${texts.archivePoint}${texts.labelSeparator}${time}`;
}

async function load() {
  const query = encodeURIComponent(token);
  const response = await fetch(`${endpoint}/api/history?token=${query}`);
  if (!response.ok) throw new Error('load failed');
  const data = await response.json();
  expandedRow = null;
  selectedRecordId = null;
  restoreButton.disabled = true;
  records.replaceChildren(...data.records.map((record) => row(record)));
}

function row(record) {
  const item = document.createElement('li');
  const header = document.createElement('div');
  const selector = document.createElement('input');
  const toggle = document.createElement('button');
  const copy = document.createElement('div');
  const name = document.createElement('div');
  const time = document.createElement('div');
  const chevron = document.createElement('span');
  const detail = document.createElement('div');
  const detailCopy = document.createElement('div');
  const detailTitle = document.createElement('p');
  const packages = document.createElement('div');
  const detailId = `record-detail-${record.id}`;
  selector.type = 'radio';
  selector.name = 'recovery-record';
  selector.value = record.id;
  selector.className = 'record-selector';
  selector.setAttribute('aria-label', `${texts.selectRestorePoint}${texts.labelSeparator}${recordTitle(record)}`);
  selector.onchange = () => selectRecord(record.id);
  header.className = 'record-header';
  toggle.type = 'button';
  toggle.className = 'record-toggle';
  toggle.setAttribute('aria-expanded', 'false');
  toggle.setAttribute('aria-controls', detailId);
  copy.className = 'copy';
  name.className = 'title';
  time.className = 'time';
  chevron.className = 'chevron';
  chevron.setAttribute('aria-hidden', 'true');
  detail.className = 'record-detail';
  detail.id = detailId;
  detailCopy.className = 'record-detail-copy';
  detailTitle.className = 'record-detail-title';
  detailTitle.textContent = texts.snapshotTitle;
  packages.className = 'package-state-list';
  name.textContent = recordTitle(record);
  time.textContent = recordAction(record);
  copy.append(name, time);
  toggle.append(copy, chevron);
  detailCopy.append(detailTitle, packages);
  detail.append(detailCopy);
  toggle.onclick = () => toggleDetail(item, toggle, detail, record, packages);
  header.append(selector, toggle);
  item.append(header, detail);
  return item;
}

function selectRecord(recordId) {
  selectedRecordId = recordId;
  restoreButton.disabled = false;
}

function closeDetail(entry) {
  entry.toggle.setAttribute('aria-expanded', 'false');
  entry.detail.classList.remove('is-open');
}

function toggleDetail(item, toggle, detail, record, packages) {
  if (expandedRow && expandedRow.item !== item) {
    closeDetail(expandedRow);
  }
  const expanding = !expandedRow || expandedRow.item !== item;
  toggle.setAttribute('aria-expanded', String(expanding));
  detail.classList.toggle('is-open', expanding);
  expandedRow = expanding ? { item, toggle, detail } : null;
  if (expanding) void loadPreview(record.id, packages, detail);
}

async function loadPreview(recordId, container, detail) {
  const requestId = String(++previewRequestId);
  detail.dataset.previewRequestId = requestId;
  renderPackageMessage(container, texts.loadingPreview);
  try {
    const query = encodeURIComponent(token);
    const recordQuery = encodeURIComponent(recordId);
    const response = await fetch(`${endpoint}/api/preview?token=${query}&record_id=${recordQuery}`);
    if (!response.ok) throw new Error('preview failed');
    const data = await response.json();
    if (!detail.classList.contains('is-open') || detail.dataset.previewRequestId !== requestId) return;
    renderPackageStates(container, data.packages);
  } catch {
    if (!detail.classList.contains('is-open') || detail.dataset.previewRequestId !== requestId) return;
    renderPackageMessage(container, texts.previewFailed);
  }
}

function renderPackageStates(container, value) {
  container.replaceChildren();
  const packages = Array.isArray(value) ? value : [];
  for (const packageState of packages) {
    if (!packageState || typeof packageState !== 'object') continue;
    const packageId = typeof packageState.package_id === 'string' ? packageState.package_id : '';
    const version = typeof packageState.version === 'string' ? packageState.version : '';
    const enabled = packageState.enabled === true;
    if (!packageId || !version) continue;
    const state = document.createElement('span');
    const name = document.createElement('span');
    const stateLabel = document.createElement('span');
    state.className = 'package-state';
    name.className = 'package-name';
    stateLabel.className = `package-status${enabled ? '' : ' is-disabled'}`;
    name.textContent = `${packageId} ${version}`;
    stateLabel.textContent = enabled ? texts.enabled : texts.disabled;
    state.append(name, stateLabel);
    container.append(state);
  }
  if (!container.childElementCount) {
    const empty = document.createElement('span');
    empty.className = 'empty-state';
    empty.textContent = texts.noPackages;
    container.append(empty);
  }
}

function renderPackageMessage(container, message) {
  container.replaceChildren();
  const value = document.createElement('span');
  value.className = 'empty-state';
  value.textContent = message;
  container.append(value);
}

function confirmRestore(id, button) {
  pendingRestore = { id, button };
  confirmTitle.textContent = texts.restoreConfirmTitle;
  confirmDescription.textContent = texts.restoreConfirmDescription;
  confirmCancel.textContent = texts.cancel;
  confirmRestoreButton.textContent = texts.restoreAndRestart;
  confirmDialog.showModal();
}

restoreButton.textContent = texts.restore;
restoreButton.disabled = true;
restoreButton.onclick = () => {
  if (selectedRecordId) confirmRestore(selectedRecordId, restoreButton);
};

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
        if path == f"{_PATH}/api/preview":
            record_ids = query.get("record_id")
            if record_ids is None or len(record_ids) != 1 or not record_ids[0]:
                self._json({"error": "record_id is required"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                packages = self.server.recovery.preview(record_ids[0])
            except RecoveryRecordNotFound:
                self._json({"error": "record not found"}, status=HTTPStatus.NOT_FOUND)
                return
            except (OSError, ValueError):
                self._json({"error": "recovery preview is unavailable"}, status=HTTPStatus.CONFLICT)
                return
            self._json({"packages": packages})
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
  <header class="page-header">
    <div class="page-copy">
      <h1>{html.escape(texts['title'])}</h1>
      <p class="description">{html.escape(texts['description'])}</p>
    </div>
    <button id="restore" type="button"></button>
  </header>
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
            "description": "Select a system plugin recovery point from the archive list below.",
            "restore": "Restore",
            "cancel": "Cancel",
            "archivePoint": "Restore point",
            "labelSeparator": ": ",
            "selectRestorePoint": "Select recovery point",
            "snapshotTitle": "System plugin states after restore",
            "enabled": "Enabled",
            "disabled": "Disabled",
            "noPackages": "No system plugins would remain installed after restore.",
            "loadingPreview": "Loading plugin states after restore…",
            "previewFailed": "Could not load the plugin states after restore.",
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
                "startup": "Started",
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
        "description": "从下述存档列表中选取系统插件还原点",
        "restore": "还原",
        "cancel": "取消",
        "archivePoint": "存档点",
        "labelSeparator": "：",
        "selectRestorePoint": "选择还原点",
        "snapshotTitle": "还原后系统插件状态",
        "enabled": "已启用",
        "disabled": "已禁用",
        "noPackages": "还原后没有已安装的系统插件。",
        "loadingPreview": "正在读取还原后的插件状态…",
        "previewFailed": "无法读取还原后的插件状态。",
        "restoreConfirmTitle": "确认还原系统插件？",
        "restoreConfirmDescription": "还原将恢复此刻对应的系统插件启用状态，并重启后台，正在处理的任务将中断。",
        "restoreAndRestart": "还原并重启",
        "restoring": "正在还原…",
        "restored": "已还原，正在重启后台服务。",
        "restoreFailed": "还原失败。",
        "loadFailed": "无法读取还原记录。",
        "actions": {
            "startup": "启动",
            "install": "安装",
            "uninstall": "卸载",
            "enable": "启用",
            "disable": "禁用",
            "restore": "还原",
        },
    }


if __name__ == "__main__":
    main()
