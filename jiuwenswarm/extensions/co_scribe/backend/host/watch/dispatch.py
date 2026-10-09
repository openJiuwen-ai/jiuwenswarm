"""Hand one cloud-document turn to the agentserver.

The split with the watcher is **policy there, transport here**. Prompts,
conventions injection and fencing are all worked out on the watcher side; this
module only manages the session, sends the turn, and handles timeout and cancel.

There are two timeouts and **this side must be the shorter one**: the transport
caps a request at 600s, so this side takes 540s (600 x 0.9). Matching 600 races
the transport and the exception type that surfaces is not predictable. Going
longer is worse: the wait_for here would never fire, nobody would send
CHAT_CANCEL, and the turn on the agentserver side would run on unbounded.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import CLOUDDOC_CHANNEL_ID
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions.co_scribe.backend.host.watch.comment_watcher import WatcherConfig
from jiuwenswarm.extensions.co_scribe.backend.host.cursor_store import CloudDocStore

logger = logging.getLogger(__name__)

# Rotate a document's session after this many turns. Without rotation the context
# grows without bound, until every turn replays hundreds of historical comments.
DEFAULT_SESSION_MAX_TURNS = 50


def make_cancel_fn(agent_client, *, now_fn: Callable[[], float]):
    """The dispatcher's cancel: a CHAT_CANCEL to the same session.

    The timeout branch in ``__call__`` calls this after it stops waiting; without it
    the agentserver keeps running the turn (it still holds the write tools) while
    the next tick dispatches the comment again. Same envelope the cron scheduler
    sends for its own timeouts.
    """

    async def cancel(*, session_id: str, request_id: str) -> None:
        envelope = e2a_from_agent_fields(
            request_id=f"clouddoc-cancel-{request_id}",
            channel_id=CLOUDDOC_CHANNEL_ID,
            session_id=session_id,
            req_method=ReqMethod.CHAT_CANCEL,
            params={"intent": "cancel", "request_id": request_id},
            is_stream=False,
            timestamp=now_fn(),
        )
        await agent_client.send_request(envelope)

    return cancel


class CloudDocDispatcher:
    def __init__(
        self,
        agent_client,
        store: CloudDocStore,
        cfg: WatcherConfig,
        *,
        now_fn: Callable[[], float],
        session_max_turns: int = DEFAULT_SESSION_MAX_TURNS,
        cancel_fn: Callable | None = None,
        work_mode: str = "",
    ) -> None:
        self._agent_client = agent_client
        self._store = store
        self._cfg = cfg
        self._now_fn = now_fn
        self._session_max_turns = int(session_max_turns)
        self._cancel_fn = cancel_fn
        self._work_mode = work_mode
        # Request ids carry a per-process counter beside the clock: two dispatches
        # for one comment inside a second (a mention and its follow-up) must not
        # share an id, and the injectable clock in tests resolves to whole seconds.
        self._request_seq = itertools.count(1)

    async def _session_for(self, doc_id: str) -> str:
        """The session id for this document, rotating it when due.

        The id carries a monotonic counter rather than a timestamp: ``now_fn`` is an
        injectable fake clock in tests, and two rotations inside one millisecond
        would otherwise collide on the same id.
        """
        sess = await self._store.get_session(doc_id)
        if sess and int(sess.get("turn_count", 0)) < self._session_max_turns:
            return str(sess["session_id"])
        generation = int((sess or {}).get("generation", 0)) + 1
        # The whole document id: a truncated one let two documents sharing a prefix
        # share a session, and with it each other's context.
        session_id = f"clouddoc_{doc_id}_{generation}"
        await self._store.set_session(doc_id, session_id)
        return session_id

    @staticmethod
    def _validated_model(name: str, *, source: str, doc_id: str = "") -> str:
        """Resolve one candidate model name, or return empty so the caller falls through.

        Validated on the way out, not only on the way in. The setters check the name
        they write, but a config file is editable by hand and a document's stored pin
        outlives the model it names, so a value that no longer resolves -- a renamed
        model, a removed one, an unexpanded ``${VAR}`` left by a template -- would
        otherwise be handed to the agentserver as if it were real, and every
        unattended turn under it would ask for a model that does not exist. An
        unusable value reads as unset: the next level down takes over, and the last
        of them is empty, which is the agentserver's own default -- the outcome the
        owner gets anyway and the only one that still works.

        With the pin now per document there are many places it can go stale rather
        than one, so the warning names both the value and the level it came from.
        """
        if not name:
            return ""
        # A stored name can be a template the config never expanded -- cron's
        # validator hands back the entry's raw ``model_name``, so selecting the
        # deployment's own default writes ``${MODEL_NAME}`` straight back into the
        # config. Expanding here means the turn runs on the model that string was
        # always pointing at, instead of failing on a name nothing can resolve.
        # (The validator returning an unexpanded value is a defect in shared cron
        # code, tracked separately; this keeps co-scribe correct either way.)
        stored = name
        try:
            from jiuwenswarm.common.config import resolve_env_vars

            name = str(resolve_env_vars(stored) or "").strip()
        except Exception:  # noqa: BLE001 - expansion is a convenience, not a gate
            name = stored
        if not name:
            # The pin was set and then evaporated: a ``${VAR}`` whose variable is
            # not set expands to nothing. Falling through silently would hide a
            # misconfiguration the owner has every reason to want told.
            logger.warning(
                "[clouddoc] %s model_name=%r 展开后为空（环境变量未设置）%s，本回合改用%s",
                source,
                stored,
                f" doc={doc_id}" if doc_id else "",
                "部署默认模型" if source == "doc" else "agentserver 默认模型",
            )
            return ""
        try:
            from jiuwenswarm.runtime.cron.models import validate_cron_model

            return str(validate_cron_model(name) or "").strip()
        except ValueError:
            # Name both: the reader has to find the stored string in the config,
            # and has to see what it expanded to before it failed.
            shown = repr(stored) if name == stored else f"{stored!r}（展开为 {name!r}）"
            logger.warning(
                "[clouddoc] %s model_name=%s 解析不到模型%s，本回合改用%s；"
                "请在 文档 面板重新选择，或清空该项",
                source,
                shown,
                f" doc={doc_id}" if doc_id else "",
                "部署默认模型" if source == "doc" else "agentserver 默认模型",
            )
            return ""
        except Exception:  # noqa: BLE001 - validation must never stop a dispatch
            return ""

    @staticmethod
    def _deployment_model_name() -> str:
        """Read clouddoc.model_name live, so a change from the panel reaches the
        next turn without a restart -- the same reason the watcher reads mode live."""
        try:
            from jiuwenswarm.common.config import get_config

            section = get_config().get("clouddoc") or {}
            return str(section.get("model_name") or "").strip()
        except Exception:  # noqa: BLE001 - an unreadable config must not stop dispatch
            return ""

    async def _doc_model_name(self, doc_id: str) -> str:
        """This document's own pin, from ``panel_meta`` -- operational data, which is
        where the panel already keeps title, kind, url and checked_at.

        Deliberately not the watch registry: the registry records **authority**, and
        which model runs the turn is an operational setting, not part of what the
        document's owner delegated.
        """
        try:
            health = await self._store.doc_health(doc_id)
            meta = health.get("panel_meta") or {}
            return str(meta.get("model_name") or "").strip()
        except Exception:  # noqa: BLE001 - an unreadable store must not stop dispatch
            return ""

    async def _configured_model_name(self, doc_id: str) -> str:
        """Which model this document's turn runs on.

        Three levels, each validated and each falling through when it does not
        resolve: **this document's own pin**, then the deployment's
        ``clouddoc.model_name``, then empty -- which leaves ``model_name`` off the
        envelope and lets the agentserver pick its own default.
        """
        doc_pin = await self._doc_model_name(doc_id)
        resolved = self._validated_model(doc_pin, source="doc", doc_id=doc_id)
        if resolved:
            return resolved
        return self._validated_model(self._deployment_model_name(), source="clouddoc.model_name")

    async def __call__(self, doc_id: str, comment_id: str, payload: dict) -> str:
        session_id = await self._session_for(doc_id)
        prompt = str(payload.get("prompt") or "")
        request_id = f"clouddoc-{comment_id}-{int(self._now_fn())}-{next(self._request_seq)}"

        params = {
            "content": prompt,
            "query": prompt,
            "supports_user_interaction": False,
        }
        if self._work_mode:
            params["work_mode"] = self._work_mode
        model_name = await self._configured_model_name(doc_id)
        if model_name:
            params["model_name"] = model_name

        envelope = e2a_from_agent_fields(
            request_id=request_id,
            channel_id=CLOUDDOC_CHANNEL_ID,
            session_id=session_id,
            req_method=ReqMethod.CHAT_SEND,
            params=params,
            is_stream=False,
            timestamp=self._now_fn(),
            # The authorization scope passes through untouched; the watcher has already
            # guaranteed it holds nothing but doc_id. The progress reply rides as a
            # SIBLING key, never inside it: it grants nothing and gates nothing, and
            # the one rule this dictionary keeps is that everything in it is read to
            # decide something.
            metadata={
                "clouddoc": dict(payload.get("clouddoc") or {}),
                "clouddoc_progress": dict(payload.get("clouddoc_progress") or {}),
            },
        )

        # The watcher owns the inflight record, because it holds the placeholder reply
        # id and crash recovery runs off exactly that id. A second record written here
        # would have no placeholder, and sweep() would read it as a crash before dispatch.
        try:
            resp = await asyncio.wait_for(
                self._agent_client.send_request(envelope),
                timeout=self._cfg.clamped_turn_timeout(),
            )
        except asyncio.TimeoutError:
            logger.warning("[clouddoc] turn timed out doc=%s comment=%s", doc_id, comment_id)
            await self._cancel(session_id, request_id)
            return ""
        except Exception:  # noqa: BLE001 - one failed turn must not end the watcher loop
            logger.exception("[clouddoc] dispatch failed doc=%s comment=%s", doc_id, comment_id)
            return ""
        finally:
            await self._store.bump_turn_count(doc_id)

        return _text_of(resp, request_id=request_id)

    async def _cancel(self, session_id: str, request_id: str) -> None:
        """A timeout must cancel explicitly.

        Without the cancel, the turn on the agentserver side keeps running and
        **still holds the propose_edit tool**, while this side has already given up
        and the next tick dispatches again -- two proposals from one comment.
        """
        if self._cancel_fn is None:
            return
        try:
            await self._cancel_fn(session_id=session_id, request_id=request_id)
        except Exception:  # noqa: BLE001
            logger.exception("[clouddoc] cancel failed session=%s", session_id)


def _text_of(resp, *, request_id: str = "") -> str:
    """The turn's reply text; **any failure returns an empty string** and the watcher
    writes generic wording instead.

    ``error`` is deliberately absent from the extraction keys. It used to be there,
    and the result was that internal exceptions from the agent runtime were pasted
    verbatim into a user's document -- one real case read ``cannot access local
    variable 'close_agent_run_span'``. Errors are sanitised on the way out: generic
    wording for the reader, detail only in the log.

    A falsy ``ok`` also yields nothing. Even when a failed turn carries back partial
    text, that text went through no further processing and must not pose as a reply.
    """
    payload = getattr(resp, "payload", None)
    if not isinstance(payload, dict):
        return ""
    if getattr(resp, "ok", True) is False or payload.get("error"):
        logger.warning(
            "[clouddoc] turn failed request_id=%s detail=%s",
            request_id, str(payload.get("error") or payload)[:400],
        )
        return ""
    for key in ("content", "text", "result"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    # A turn that succeeded and said nothing. The reader gets generic wording either
    # way, so without this line the log holds no trace of the turn at all -- and the
    # cause is never in the turn itself but upstream (an interrupt with nobody to
    # answer it, a round that ended on a tool call), which is exactly what makes it
    # worth a line saying which request to go looking for.
    logger.warning(
        "[clouddoc] turn returned no text request_id=%s payload_keys=%s",
        request_id, sorted(payload),
    )
    return ""
