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

What no help text can answer is the shape of a response from a live tenant. Those
assumptions are marked at their sites in the provider, with the spike (§17.2) that
settles each. The seam is the point: one place where a command is built, run and
classified, so a correction is a local edit rather than a search.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from typing import Any, Sequence

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import ProviderError

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


def _structured(stderr: str) -> tuple[str, str] | None:
    """Read the CLI's JSON error envelope, or None when it is not one."""
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
    # The hint is the actionable half -- "run config init" is what a person needs to
    # read, and dropping it turns a fixable state into a bare "not configured".
    full = " ".join(x for x in (message, hint) if x) or subtype or etype
    return _TYPE_MAP.get(etype, "unknown"), full


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
        kind, message = detail
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
        low = (message or stderr or "").lower()
    else:
        low = (stderr or "").lower()
    if "permission" in low or "forbidden" in low or "not authorized" in low:
        return ProviderError("forbidden", stderr.strip() or "no permission")
    if "has been delete" in low or "resource is deleted" in low:
        # Feishu code 1061007: the file is already in the recycle bin. Said as
        # that, not as a bare not_found -- the person reading it should know the
        # document exists but is trashed (observed on the live tenant).
        return ProviderError("not_found", "该文档已在回收站（平台：file has been deleted）")
    if "20008" in low and "user does not exist" in low:
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
        return ProviderError("not_found", stderr.strip() or "not found")
    if "rate limit" in low or "too many requests" in low or "429" in low:
        return ProviderError("rate_limited", stderr.strip() or "rate limited")
    if "timeout" in low or "timed out" in low:
        return ProviderError("transport", stderr.strip() or "timed out")
    if "unauthorized" in low or "invalid token" in low or "401" in low:
        return ProviderError("auth", stderr.strip() or "authentication failed")
    return ProviderError("unknown", stderr.strip() or f"lark exited with {code}")


class LarkCli:
    """The seam between the provider and the ``lark`` executable."""

    def __init__(
        self,
        *,
        binary: str = "lark-cli",
        profile: str = "",
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._binary = binary
        # Which registered app to act as, when a deployment has more than one. The
        # secret lives in the CLI's own store and never passes through here.
        self._profile = profile
        self._timeout = timeout_seconds
        # One command at a time. The CLI keeps its own token cache on disk, and two
        # processes refreshing it concurrently is the kind of race whose symptom is an
        # occasional auth failure with nothing in the logs to explain it.
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """Whether the executable can be found at all.

        Read at startup so a deployment missing the binary is told once, plainly,
        rather than discovering it as a failure on every poll.
        """
        return shutil.which(self._binary) is not None

    def _base_args(self) -> list[str]:
        # --as bot is not a default to be overridden: see the module docstring.
        args = [self._binary, "--as", "bot"]
        if self._profile:
            args += ["--profile", self._profile]
        return args

    async def run(self, args: Sequence[str], *, timeout: float | None = None) -> LarkResult:
        """Run one command and return its raw result, raising only for transport faults.

        A non-zero exit is returned rather than raised: some commands answer a question
        by failing -- asking about a document the app cannot see -- and the caller is
        better placed to decide whether that is an error or an answer.
        """
        if not self.available:
            raise ProviderError(
                "unavailable",
                f"未找到 {self._binary} 可执行文件；飞书 provider 需要它作为执行底座。",
            )
        cmd = self._base_args() + list(args)
        async with self._lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                raise ProviderError("transport", f"无法启动 {self._binary}：{exc}") from exc
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout or self._timeout
                )
            except asyncio.TimeoutError as exc:
                # Killed rather than left behind: an abandoned subprocess holds its
                # token file open and the next call would queue behind nothing.
                proc.kill()
                await proc.wait()
                raise ProviderError(
                    "transport", f"{self._binary} 超时（{timeout or self._timeout:.0f}s）"
                ) from exc
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
                detail = _structured(text)
                if detail is not None:
                    raise ProviderError(*detail)
                raise ProviderError("unknown", text[:200])
            return payload.get("data")
        return payload
