"""Run ``lark`` as a subprocess, and turn its output into values or ProviderError.

Why a CLI rather than the REST API: the official client already carries app-token
acquisition and refresh, wiki-token resolution, the comment-to-block mapping and the
event long connection. Reimplementing those against the HTTP API is work the platform
has already done, and the parts most likely to drift.

Why the CLI is not exposed to the agent: its own safety model is prompt-level -- rules
written for a model to read -- and handing a model 200 commands is an open set. It is
the same reason MCP was dropped in §3.4, and more so here: the closed tool set is
what makes an unattended turn safe to run, and it cannot be closed around a shell.

**Every command is issued ``--as bot``.** Acting as a user would misattribute the
write, and the loop prohibition depends on the agent recognising its own events by
author -- an event the agent produced under a person's identity is indistinguishable
from one that person produced.

Credentials are not passed per call. ``lark-cli`` has no ``--app-id`` flag: an app is
registered once with ``config init --app-id --app-secret-stdin``, which stores the
secret outside the process, and a deployment selects among several with ``--profile``.
That is better than the shape assumed before reading it -- a secret on an argument
list is visible to anyone who can read ``/proc`` -- so this class carries a profile
name and never the secret itself.

Verified against 1.0.89: every command this module issues is accepted by the real
binary, checked with ``--dry-run``, which reports an unknown flag as ``validation``
and a well-formed call as ``config``/``not_configured``. Errors arrive as JSON on
stderr with a stable ``error.type``, which is what classification reads.

Where the binary and its configuration come from is the host's choice, not this
module's. The host already installs lark-cli in two ways: connecting Feishu in the
MCP panel installs the hub package's pinned version on PATH and registers an app as
the default profile (``config init --new``), and JiuwenMate prepares a private
binary and a bot profile per agent, reached through the environment its runtime
hands out. ``binary``, ``profile`` and ``env`` accept either without this package
importing the host. Re-verified against 1.0.94, the version the hub package pins.

What no help text can answer is the shape of a response from a live tenant. Those
assumptions are marked at their sites in the provider, with the spike (§17.2) that
settles each. The seam is the point: one place where a command is built, run and
classified, so a correction is a local edit rather than a search.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import weakref
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import psutil

from jiuwenswarm.clouddoc.providers.base import ProviderError

logger = logging.getLogger(__name__)

# Long enough for a document fetch on a slow tenant, short enough that a hung
# subprocess does not hold a watcher tick open indefinitely.
DEFAULT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class LarkResult:
    """One command's outcome, before it is interpreted as a document or a comment."""

    stdout: str
    stderr: str
    code: int

    @property
    def ok(self) -> bool:
        return self.code == 0


# The CLI reports failures as JSON on stderr, with a stable ``error.type`` and a
# finer ``error.subtype``. Measured against 1.0.89: a bad flag is "validation", a
# missing credential is "config"/"not_configured", and the process exits non-zero.
# These are read first, and the message text only when they are absent.
_TYPE_MAP = {
    "validation": "invalid",
    "config": "auth",
    "auth": "auth",
    "permission": "forbidden",
    "not_found": "not_found",
    "rate_limit": "rate_limited",
    "network": "transport",
    "timeout": "transport",
}


def _structured(stderr: str) -> tuple[str, str, str] | None:
    """Read the CLI's JSON error envelope, or None when it is not one.

    Returns the kind its ``type`` maps to, the message a person reads (message and
    hint), and the platform's numeric ``code``, kept apart from the prose so a rule
    keyed on a code holds whatever the message says, and a code's digits never match a
    prose rule.
    """
    text = (stderr or "").strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    err = data.get("error")
    if not isinstance(err, dict):
        return None
    etype = str(err.get("type") or "").lower()
    subtype = str(err.get("subtype") or "")
    message = str(err.get("message") or "").strip()
    hint = str(err.get("hint") or "").strip()
    code = str(err.get("code") or "").strip()
    # The hint is the actionable half -- "run config init" is what a person needs to
    # read, and dropping it turns a fixable state into a bare "not configured".
    full = " ".join(x for x in (message, hint) if x) or subtype or etype
    return _TYPE_MAP.get(etype, "unknown"), full, code


def _classify(code: int, stderr: str) -> ProviderError:
    """Map a failed command onto the provider's error vocabulary.

    The vocabulary is the one the tools already branch on, so a Feishu failure reaches
    a person as the same kind of sentence a Google failure does.

    The structured fields are preferred over the prose, because prose is what changes
    between releases. Text matching stays as the fallback for a failure that arrives
    without them, and an unrecognised failure becomes ``unknown`` rather than being
    guessed at -- a wrong kind sends the caller down the wrong repair.
    """
    detail = _structured(stderr)
    if detail is not None:
        kind, message, platform_code = detail
        if kind != "unknown":
            return ProviderError(kind, message)
        # An envelope whose ``type`` this map does not know still carries a message,
        # and the text rules below often know it. Returning ``unknown`` here threw
        # that away: the live tenant answers a deleted document with
        # ``type: "api", subtype: "unknown", message: "Resource is deleted"`` -- a
        # settled failure the prose rules classify correctly and the structured path
        # did not. Measured: one deleted document failed admission every poll for
        # 1827 consecutive polls, because ``unknown`` reads as transient.
        #
        # Structure is still preferred; this only stops an unmapped type from
        # discarding an answer the same envelope already contains.
        low = message.lower()
        # What a person reads is the envelope's message and hint, not the raw JSON.
        text = message
    else:
        low = (stderr or "").lower()
        text = (stderr or "").strip()
        platform_code = ""
    if "permission" in low or "forbidden" in low or "not authorized" in low:
        return ProviderError("forbidden", text or "no permission")
    if "has been delete" in low or "resource is deleted" in low:
        # Feishu code 1061007: the file is already in the recycle bin. Said as
        # that, not as a bare not_found -- the person reading it should know the
        # document exists but is trashed (observed on the live tenant).
        return ProviderError("not_found", "该文档已在回收站（平台：file has been deleted）")
    if platform_code == "20008" or ("20008" in low and "user does not exist" in low):
        # Feishu code 20008 is a catch-all whose text is actively misleading: it says
        # "The user does not exist" for things that have nothing to do with a user.
        # **Two causes were measured on 2026-09-10, and the message must not pick one**
        # -- an earlier draft of this asserted the first alone, which is the same
        # mistake in the other direction:
        #
        #  1. a worksheet name that is not in the workbook -- reproduced by reading
        #     A1:B2 of 云文档台账 in a workbook holding only Sheet1;
        #  2. the first attempt of the wiki-container retry on a wiki-hosted file,
        #     which then succeeds as ``--type wiki`` -- observed on a write to
        #     Sheet1, a sheet that plainly exists.
        #
        # Left as the platform's own prose, this cost a recording session: the model
        # read it as a fault, retried, saw the retry succeed, concluded "intermittent",
        # and went looking at CLI versions -- having an hour earlier, from the same
        # dead end, reached for the platform CLI through a shell and created a
        # worksheet with no receipt. A refusal that does not say what to do next gets
        # one invented, so this names both causes and the check that separates them.
        return ProviderError(
            "not_found",
            "平台错误码 20008：字面文本「The user does not exist」与实际原因无关。"
            "两种已知成因：(1) 工作表名不在这个工作簿里——用 clouddoc_read 核对表名，"
            "或用 clouddoc_add_page 新建；(2) wiki 托管文件按错误的容器类型访问——"
            "drive 系列命令会自动按 wiki 容器重试，看到这句说明重试也失败了，按 (1) 核对。"
            "**不要去查 CLI 版本或权限，也不要改用 shell 调平台命令行**。",
        )
    if "not found" in low or "no such" in low or "does not exist" in low:
        return ProviderError("not_found", text or "not found")
    if "rate limit" in low or "too many requests" in low or "429" in low:
        return ProviderError("rate_limited", text or "rate limited")
    if "timeout" in low or "timed out" in low:
        return ProviderError("transport", text or "timed out")
    if "unauthorized" in low or "invalid token" in low or "401" in low:
        return ProviderError("auth", text or "authentication failed")
    return ProviderError("unknown", text or f"lark exited with {code}")


# One command at a time per credential store and profile, across every LarkCli in
# the process. The CLI keeps its token cache on disk, and two processes refreshing it
# concurrently is the kind of race whose symptom is an occasional auth failure with
# nothing in the logs to explain it -- whether those processes came from one instance
# or from two built on the same profile. Keyed by event loop as well, because an
# asyncio lock belongs to the loop it first waits on.
_STORE_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, str], asyncio.Lock]
] = weakref.WeakKeyDictionary()


def _store_lock(store: tuple[str, str]) -> asyncio.Lock:
    locks = _STORE_LOCKS.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(store, asyncio.Lock())


async def _stop_tree(proc: asyncio.subprocess.Process) -> None:
    """Stop a command and everything it started.

    The npm installation's ``lark-cli`` is a Node launcher that runs the real binary
    as a child, so killing the launcher alone leaves the binary running. JiuwenMate's
    CLI runtime stops its commands the same way.
    """
    children: list[psutil.Process] = []
    if proc.returncode is None:
        try:
            children = psutil.Process(proc.pid).children(recursive=True)
        except psutil.Error:
            pass
    if os.name != "nt":
        # Killed whether or not the launcher is still alive: a launcher that exited
        # after starting a child leaves that child in the group, holding the output
        # pipes the timeout fired on. The group is this command's own (a new session
        # at start, so its id is the launcher's pid), and a group that still has a
        # member keeps that id from being reused; an empty one raises and is skipped.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    for child in reversed(children):
        try:
            child.kill()
        except psutil.Error:
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        logger.warning("lark-cli pid %s did not exit after being killed", proc.pid)


class LarkCli:
    """The seam between the provider and the ``lark`` executable."""

    def __init__(
        self,
        *,
        binary: str = "lark-cli",
        profile: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._binary = binary
        # The whole environment the subprocess runs with, when the host prepared one
        # -- JiuwenMate's per-agent runtime selects its private binary and profile
        # through PATH and LARKSUITE_CLI_CONFIG_DIR. None inherits this process's.
        self._env = dict(env) if env is not None else None
        # Which registered app to act as, when a deployment has more than one. The
        # secret lives in the CLI's own store and never passes through here.
        self._profile = profile
        self._timeout = timeout_seconds
        # The store the CLI will read: the injected config directory, else this
        # process's, else its default. Commands on the same store and profile share
        # one lock (see _STORE_LOCKS).
        source = self._env if self._env is not None else os.environ
        self._store = (source.get("LARKSUITE_CLI_CONFIG_DIR", ""), profile)

    def _executable(self) -> str | None:
        # Resolved on the PATH the subprocess will run with, not this process's, so a
        # private installation named only by the injected environment is found. The
        # resolved path is what runs: on Windows the npm launcher is lark-cli.cmd, and
        # process creation does not apply PATHEXT to a bare name.
        # An injected environment without PATH finds nothing on PATH, as the
        # subprocess running with that environment would not.
        path = self._env.get("PATH", "") if self._env is not None else None
        return shutil.which(self._binary, path=path)

    @property
    def available(self) -> bool:
        """Whether the executable can be found at all.

        Read at startup so a deployment missing the binary is told once, plainly,
        rather than discovering it as a failure on every poll.
        """
        return self._executable() is not None

    def _base_args(self, executable: str | None = None) -> list[str]:
        # --as bot is not a default to be overridden: see the module docstring.
        args = [executable or self._binary, "--as", "bot"]
        if self._profile:
            args += ["--profile", self._profile]
        return args

    async def run(self, args: Sequence[str], *, timeout: float | None = None) -> LarkResult:
        """Run one command and return its raw result, raising only for transport faults.

        A non-zero exit is returned rather than raised: some commands answer a question
        by failing -- asking about a document the app cannot see -- and the caller is
        better placed to decide whether that is an error or an answer.
        """
        executable = self._executable()
        if executable is None:
            # Nothing was sent, so this is a refusal before any mutation; ``auth`` is
            # the kind an unconfigured CLI ("config") already maps to -- the
            # deployment cannot reach the platform until a person sets it up.
            raise ProviderError(
                "auth",
                f"未找到 {self._binary} 可执行文件；飞书 provider 需要它作为执行底座。",
            )
        cmd = self._base_args(executable) + list(args)
        limit = self._timeout if timeout is None else timeout
        async with _store_lock(self._store):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._env,
                    # Its own process group, so a stop reaches the whole tree.
                    start_new_session=os.name != "nt",
                )
            except OSError as exc:
                raise ProviderError("transport", f"无法启动 {self._binary}：{exc}") from exc
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(), timeout=limit
                )
            except asyncio.TimeoutError as exc:
                # Stopped rather than left behind: an abandoned subprocess holds its
                # token file open and the next call would queue behind nothing.
                await _stop_tree(proc)
                raise ProviderError(
                    "transport", f"{self._binary} 超时（{limit:.0f}s）"
                ) from exc
            except asyncio.CancelledError:
                # A cancelled turn or watcher tick stops its command too.
                await _stop_tree(proc)
                raise
        return LarkResult(
            stdout=(out or b"").decode("utf-8", errors="replace"),
            stderr=(err or b"").decode("utf-8", errors="replace"),
            code=proc.returncode or 0,
        )

    async def json(self, args: Sequence[str], *, timeout: float | None = None) -> Any:
        """Run a command whose output is expected to be JSON.

        Unparseable output is an error with the first of the text attached. A CLI that
        answers with a human sentence where JSON was expected -- a login prompt, a
        deprecation notice -- otherwise surfaces as a confusing KeyError much later.
        """
        res = await self.run(args, timeout=timeout)
        if not res.ok:
            raise _classify(res.code, res.stderr)
        text = res.stdout.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise ProviderError(
                "unknown",
                f"{self._binary} 输出不是 JSON：{text[:200]}",
            ) from exc
        # Every command answers in the same envelope: {"ok", "identity", "data"}.
        # Unwrapping here means each caller reads the payload it asked about rather
        # than remembering to step over the envelope, and an ok:false that arrived on
        # stdout is classified the same way as one that arrived on stderr.
        if isinstance(payload, dict) and "ok" in payload:
            if not payload.get("ok"):
                raise _classify(res.code, text)
            return payload.get("data")
        return payload
