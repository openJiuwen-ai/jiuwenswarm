# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for exec: credential references.

Covers: prewarm, dispatch lookup, background queue self-heal, degraded
semantics, segment-wise registry matching, identity-form comparisons,
${VAR} unchanged, the save-path reference preservation, raw-form echo,
the save round-trip, duplicate-key semantics, and the pending claim.
"""

from __future__ import annotations

import shlex
import sys
import threading
import time

import pytest

from jiuwenswarm.common.config import _identity_api_key, resolve_env_vars
from jiuwenswarm.common.credentials import registry, resolver


@pytest.fixture(autouse=True)
def _clear_caches():
    registry._CACHE = None
    resolver._RESOLVED_SECRETS.clear()
    resolver._WARN_LAST_AT.clear()
    yield
    registry._CACHE = None
    resolver._RESOLVED_SECRETS.clear()
    resolver._WARN_LAST_AT.clear()


def _register(monkeypatch, pattern: str = "models.*.model_client_config.api_key") -> None:
    monkeypatch.setattr(registry, "_load_patterns", lambda: [p.split(".") for p in [pattern]])


def _echo_command(text: str) -> str:
    return "exec:" + " ".join(
        shlex.quote(x) for x in (sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])")
    ) + " " + shlex.quote(text)


def _walk_resolved(cfg):
    return cfg["models"]["defaults"][0]["model_client_config"]["api_key"]


class TestExecResolution:
    """exec success/failure/timeout degrade; never raises."""

    def test_exec_success_returns_stdout(self, monkeypatch):
        """First query misses → "" + background enqueue; worker resolves →
        the next query hits (self-heal)."""
        _register(monkeypatch)
        cfg = {"models": {"defaults": [{"model_client_config": {"api_key": _echo_command("sk-live")}}]}}
        assert _walk_resolved(resolve_env_vars(cfg)) == ""  # miss: queued, dispatch returns ""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:  # worker consumes → next query hits
            if _walk_resolved(resolve_env_vars(cfg)) == "sk-live":
                return
            time.sleep(0.1)
        pytest.fail("background queue did not resolve the reference within 10s")

    def test_exec_failure_degrades_to_empty(self, monkeypatch):
        _register(monkeypatch)
        cfg = {"models": {"defaults": [{"model_client_config": {"api_key": "exec:this-cmd-missing-xyz"}}]}}
        assert _walk_resolved(resolve_env_vars(cfg)) == ""

    def test_exec_empty_stdout_degrades(self, monkeypatch):
        _register(monkeypatch)
        ref = "exec:" + " ".join(shlex.quote(x) for x in (sys.executable, "-c", "pass"))
        cfg = {"models": {"defaults": [{"model_client_config": {"api_key": ref}}]}}
        assert _walk_resolved(resolve_env_vars(cfg)) == ""

    def test_exec_timeout_degrades(self, monkeypatch):
        _register(monkeypatch)
        monkeypatch.setattr(resolver, "_EXEC_TIMEOUT_SECONDS", 0.3)
        ref = "exec:" + " ".join(shlex.quote(x) for x in (sys.executable, "-c", "import time; time.sleep(2)"))
        cfg = {"models": {"defaults": [{"model_client_config": {"api_key": ref}}]}}
        assert _walk_resolved(resolve_env_vars(cfg)) == ""

    def test_dispatch_never_executes_on_query_path(self, monkeypatch):
        """resolve_env_vars must only look up, never run commands.

        All executions happen in the background worker; the dispatch branch
        returns "" and enqueues. Assert no command ran during the query.
        """
        _register(monkeypatch)
        ran = []
        monkeypatch.setattr(resolver, "_exec_secret_command", lambda r: ran.append(r) or (None, "x"))
        cfg = {"models": {"defaults": [{"model_client_config": {"api_key": "exec:new-cmd"}}]}}
        out = resolve_env_vars(cfg)
        assert _walk_resolved(out) == ""  # degrade immediately
        assert ran == []  # dispatch did NOT execute


class TestVarUnchanged:
    """${VAR} unset keeps current silent-empty behavior."""

    def test_unset_var_silent_empty(self, monkeypatch):
        monkeypatch.delenv("XYZ_UNSET", raising=False)
        assert resolve_env_vars({"api_key": "${XYZ_UNSET}"}) == {"api_key": ""}

    def test_set_var_resolves(self, monkeypatch):
        monkeypatch.setenv("XYZ_SET", "v1")
        assert resolve_env_vars({"api_key": "${XYZ_SET}"}) == {"api_key": "v1"}


class TestRegistrySegmentMatch:
    """Segment-wise registry matching precision."""

    def test_seven_real_segments_match(self):
        for path in (
            "models.defaults.model_client_config.api_key",
            "models.agentos.model_client_config.api_key",
            "models.vision.model_client_config.api_key",
            "models.default.model_client_config.api_key",
        ):
            assert registry.is_credential_field(path), path

    def test_depth_and_prefix_rejected(self):
        assert not registry.is_credential_field("models.x.y.z.model_client_config.api_key")
        assert not registry.is_credential_field("models.defaults.model_config_obj.api_key")
        assert not registry.is_credential_field("channels.feishu.app_secret")
        assert not registry.is_credential_field("")


class TestBackgroundQueue:
    """Background serial queue — self-heal without event-loop execution."""

    def test_miss_enqueues_and_worker_resolves(self, monkeypatch):
        _register(monkeypatch)
        ref = "exec:this-cmd-missing-abc --x"
        cfg = {"models": {"defaults": [{"model_client_config": {"api_key": ref}}]}}
        assert _walk_resolved(resolve_env_vars(cfg)) == ""  # miss → "" + enqueue
        # Now make the command resolvable and let the queue consumer run it.
        monkeypatch.setattr(resolver, "_exec_secret_command", lambda r: ("sk-healed", ""))
        resolver._enqueue_resolve(ref, field_path="f")  # same shape as the worker consumes
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with resolver._RESOLVED_LOCK:
                e = resolver._RESOLVED_SECRETS.get(ref)
            if e and e["status"] == "resolved":
                break
            time.sleep(0.05)
        assert resolver.resolve_credential_ref(ref, field_path="f") == "sk-healed"

    def test_failed_backoff_skips_retry_within_window(self, monkeypatch):
        ref = "exec:flaky"
        resolver._record(ref, "", "failed")  # fresh failure → backoff active
        ran = []
        monkeypatch.setattr(
            resolver, "_exec_secret_command", lambda r: ran.append(r) or (None, "exit=1")
        )
        assert resolver.resolve_credential_ref(ref, field_path="f") == ""
        assert ran == []  # backoff short-circuits: no retry, no command

    def test_backoff_expiry_allows_retry(self, monkeypatch):
        """Failed entries retry after the backoff window (self-heal):
        window-expired miss enqueues, the worker executes, next query hits."""
        ref = "exec:flaky2"
        resolver._record(ref, "", "failed")
        with resolver._RESOLVED_LOCK:
            e = resolver._RESOLVED_SECRETS[ref]
            e["ts"] -= resolver._BACKOFF_SECONDS + 1  # expire backoff
        monkeypatch.setattr(resolver, "_exec_secret_command", lambda r: ("sk-ok", ""))
        assert resolver.resolve_credential_ref(ref, field_path="f") == ""  # miss → enqueue
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if resolver.resolve_credential_ref(ref, field_path="f") == "sk-ok":
                return
            time.sleep(0.05)
        pytest.fail("queue did not retry after backoff expiry within 5s")

    def test_concurrent_misses_execute_once(self, monkeypatch):
        """Serial worker dedups concurrent misses to one execution.

        Six threads miss the same new reference; every miss only enqueues
        (the dispatch branch never executes). The single worker consumes the
        queue serially → exactly one command execution; once resolved, all
        later queries hit the store and the cached hit never reruns.
        """
        _register(monkeypatch)
        calls = []
        lock = threading.Lock()
        first_done = threading.Event()

        def slow_exec(r):
            with lock:
                calls.append(r)
            time.sleep(0.05)
            first_done.set()
            return "sk-c", ""

        monkeypatch.setattr(resolver, "_exec_secret_command", slow_exec)
        ref = _echo_command("c")
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(resolver.resolve_credential_ref(ref, field_path="f")))
            for _ in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        first_done.wait(5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not results or "" in results:
            if results and all(r == "sk-c" for r in results):
                break
            time.sleep(0.05)
            results = [resolver.resolve_credential_ref(ref, field_path="f") for _ in range(6)]
        assert len(calls) == 1  # single worker: one execution despite 6 misses
        assert results == ["sk-c"] * 6
        before = len(calls)
        assert resolver.resolve_credential_ref(ref, field_path="f") == "sk-c"
        assert len(calls) == before  # cached hit: no rerun


class TestIdentityApiKey:
    """Identity-form comparisons use the stored form, never resolved."""

    def test_reference_verbatim(self):
        assert _identity_api_key({"api_key": "exec:op read x"}) == "exec:op read x"

    def test_plaintext_verbatim(self):
        assert _identity_api_key({"api_key": "sk-p"}) == "sk-p"

    def test_empty(self):
        assert _identity_api_key(None) == ""
        assert _identity_api_key({"api_key": ""}) == ""

    def test_ciphertext_decrypts_with_provider(self, monkeypatch):
        class FakeCrypto:
            def encrypt(self, p, **k):
                return "ENC::" + p

            def decrypt(self, c, **k):
                return c.removeprefix("ENC::")

        from jiuwenswarm.common.config import get_crypto_provider

        monkeypatch.setattr("jiuwenswarm.common.config.get_crypto_provider", lambda: FakeCrypto())
        assert _identity_api_key({"api_key": "ENC::sk-real"}) == "sk-real"


class TestProtectModelKeyShortCircuit:
    """exec: reference skips encryption; round-trip preserves it."""

    def test_reference_not_encrypted(self, monkeypatch):
        import jiuwenswarm.common.config as m

        class FakeCrypto:
            def encrypt(self, p, **k):
                return "ENC::" + p

            def decrypt(self, c, **k):
                raise ValueError("not ciphertext")

        monkeypatch.setattr(m, "get_crypto_provider", lambda: FakeCrypto())
        update = {"model_client_config": {"api_key": "exec:op read x"}}
        current = {"model_client_config": {"api_key": "exec:op read x"}}
        m._protect_model_key(update, current)
        assert update["model_client_config"]["api_key"] == "exec:op read x"

    def test_plain_still_encrypted(self, monkeypatch):
        import jiuwenswarm.common.config as m

        class FakeCrypto:
            def encrypt(self, p, **k):
                return "ENC::" + p

            def decrypt(self, c, **k):
                return c.removeprefix("ENC::")

        monkeypatch.setattr(m, "get_crypto_provider", lambda: FakeCrypto())
        update = {"model_client_config": {"api_key": "sk-new"}}
        current = {"model_client_config": {"api_key": "sk-old"}}
        m._protect_model_key(update, current)
        assert update["model_client_config"]["api_key"] == "ENC::sk-new"


class TestRawEcho:
    """Display surfaces must echo the stored form, never resolved plaintext."""

    def _write_config(self, tmp_path, monkeypatch, api_key):
        import yaml as _yaml

        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            _yaml.dump({"models": {"defaults": [{
                "model_id": "mdl-1",
                "model_client_config": {"model_name": "m1", "api_key": api_key},
            }]}}),
            encoding="utf-8",
        )
        import jiuwenswarm.common.config as m

        # Path (not str): _yaml_file_stamp stat()s it directly.
        monkeypatch.setattr(m, "CONFIG_YAML_PATH", config_file)
        monkeypatch.setattr(m, "get_config_file", lambda: config_file)
        return m

    def test_raw_api_key_returns_reference_after_resolution(self, tmp_path, monkeypatch):
        """get_config() resolves the store hit; the raw lookup still returns
        the reference string (display never leaks the resolved secret)."""
        _register(monkeypatch)
        m = self._write_config(tmp_path, monkeypatch, _echo_command("sk-leak"))
        resolver._record(_echo_command("sk-leak"), "sk-leak", "resolved")
        resolved_cfg = m.get_config()
        assert resolved_cfg["models"]["defaults"][0]["model_client_config"]["api_key"] == "sk-leak"
        assert m.raw_api_key_by_model_id("mdl-1") == _echo_command("sk-leak")
        assert m.raw_api_keys_by_model_id()["mdl-1"] == _echo_command("sk-leak")

    def test_raw_api_key_unknown_or_missing(self, tmp_path, monkeypatch):
        m = self._write_config(tmp_path, monkeypatch, "sk-plain")
        assert m.raw_api_key_by_model_id("mdl-nope") is None
        assert m.raw_api_key_by_model_id("") is None

    def test_raw_map_skips_non_reference_forms(self, tmp_path, monkeypatch):
        """Only exec: references enter the raw map. A ${VAR} entry must fall
        back to the resolved display value: echoing the literal placeholder
        made replace_all encrypt it into ENC::${API_KEY} (review-4 P2-1)."""
        import yaml as _yaml

        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(_yaml.dump({"models": {"defaults": [
            {"model_id": "mdl-var", "model_client_config": {"api_key": "${API_KEY}"}},
            {"model_id": "mdl-plain", "model_client_config": {"api_key": "sk-x"}},
        ]}}), encoding="utf-8")
        import jiuwenswarm.common.config as m

        monkeypatch.setattr(m, "CONFIG_YAML_PATH", config_file)
        monkeypatch.setattr(m, "get_config_file", lambda: config_file)
        assert m.raw_api_keys_by_model_id() == {}
        assert m.raw_api_key_by_model_id("mdl-var") is None

    def test_raw_map_covers_agentos_references(self, tmp_path, monkeypatch):
        """agentos reference entries must not fall back to resolved plaintext
        in list responses (review-4 P2-2)."""
        import yaml as _yaml

        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        ref = _echo_command("sk-agentos")
        config_file.write_text(_yaml.dump({"models": {
            "defaults": [{"model_id": "mdl-1", "model_client_config": {"api_key": "sk-x"}}],
            "agentos": [{"model_id": "mdl-backup", "model_client_config": {"api_key": ref}}],
        }}), encoding="utf-8")
        import jiuwenswarm.common.config as m

        monkeypatch.setattr(m, "CONFIG_YAML_PATH", config_file)
        monkeypatch.setattr(m, "get_config_file", lambda: config_file)
        assert m.raw_api_keys_by_model_id() == {"mdl-backup": ref}


class TestSaveRoundTrip:
    """A reference echoed back and re-saved stays a reference on disk."""

    @pytest.fixture(autouse=True)
    def _bypass_file_lock_if_broken(self, monkeypatch):
        """Skip the cross-process file lock when pywintypes is unavailable.

        Some sandboxed interpreters ship a pywin32 without the compiled
        pywintypes module; portalocker then raises on acquire. The lock only
        guards cross-process writes — irrelevant to a single-process test.
        """
        try:
            import portalocker  # noqa: F401
            import pywintypes  # noqa: F401
        except ImportError:
            import jiuwenswarm.common.config as m
            from contextlib import contextmanager

            @contextmanager
            def _null_lock(*_args, **_kwargs):
                yield

            monkeypatch.setattr(m.portalocker, "Lock", _null_lock)

    def test_upsert_unchanged_reference_preserved(self, tmp_path, monkeypatch):
        import yaml as _yaml

        _register(monkeypatch)
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        ref = _echo_command("sk-live")
        config_file.write_text(
            _yaml.dump({"models": {"defaults": [{
                "model_id": "mdl-1",
                "is_default": True,
                "model_client_config": {
                    "model_name": "m1", "api_base": "https://x", "api_key": ref,
                    "client_provider": "OpenAI",
                },
            }]}}),
            encoding="utf-8",
        )
        import jiuwenswarm.common.config as m

        monkeypatch.setattr(m, "CONFIG_YAML_PATH", config_file)
        monkeypatch.setattr(m, "get_config_file", lambda: config_file)
        resolver._record(ref, "sk-live", "resolved")

        # The save path receives the echoed reference (what the edit dialog
        # sends back when the user did not touch api_key).
        saved = m.upsert_model_resource({
            "model_id": "mdl-1",
            "model_client_config": {"model_name": "m2", "api_key": ref},
        })
        assert saved["model_client_config"]["api_key"] == ref
        on_disk = _yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert on_disk["models"]["defaults"][0]["model_client_config"]["api_key"] == ref

    def test_var_placeholder_not_corrupted_by_save(self, tmp_path, monkeypatch):
        """A ${VAR} entry echoes the resolved display value (not the literal
        placeholder), so a re-save compares equal and never encrypts the
        placeholder into ciphertext (review-4 P2-1 regression)."""
        import yaml as _yaml

        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            _yaml.dump({"models": {"defaults": [{
                "model_id": "mdl-var",
                "is_default": True,
                "model_client_config": {
                    "model_name": "m1", "api_base": "https://x",
                    "api_key": "${DUP_RT_KEY}", "client_provider": "OpenAI",
                },
            }]}}),
            encoding="utf-8",
        )
        import jiuwenswarm.common.config as m

        monkeypatch.setattr(m, "CONFIG_YAML_PATH", config_file)
        monkeypatch.setattr(m, "get_config_file", lambda: config_file)
        monkeypatch.setenv("DUP_RT_KEY", "sk-from-env")
        # Display: the resolved value, not the literal ${DUP_RT_KEY}.
        assert m.raw_api_keys_by_model_id() == {}
        resolved = m.get_config()["models"]["defaults"][0]["model_client_config"]["api_key"]
        assert resolved == "sk-from-env"

        class NoCrypto:
            pass

        monkeypatch.setattr(m, "get_crypto_provider", lambda: None)
        # Save round-trip: frontend sends back what was displayed. Without a
        # crypto provider the submitted value is stored verbatim (baseline
        # behavior); the corruption being guarded is the placeholder being
        # *encrypted* into ENC::${...} — that must never appear on disk.
        saved = m.upsert_model_resource({
            "model_id": "mdl-var",
            "model_client_config": {"model_name": "m1", "api_key": resolved},
        })
        on_disk = _yaml.safe_load(config_file.read_text(encoding="utf-8"))
        on_disk_key = str(on_disk["models"]["defaults"][0]["model_client_config"]["api_key"])
        assert not on_disk_key.startswith("ENC::")  # placeholder never encrypted
        assert on_disk_key in {"${DUP_RT_KEY}", "sk-from-env"}


class TestDuplicateApiKeyMatches:
    """Duplicate detection semantics: effective value, empty skips, references literal."""

    def _run(self, stored, incoming):
        from jiuwenswarm.common.config import _duplicate_api_key_matches

        return _duplicate_api_key_matches({"api_key": stored}, {"api_key": incoming})

    def test_env_var_expansion_participates(self, monkeypatch):
        monkeypatch.setenv("DUP_KEY", "sk-same")
        assert self._run("sk-same", "${DUP_KEY}") is True
        assert self._run("sk-other", "${DUP_KEY}") is False

    def test_empty_side_skips_key_match(self, monkeypatch):
        monkeypatch.delenv("DUP_UNSET_A", raising=False)
        monkeypatch.delenv("DUP_UNSET_B", raising=False)
        assert self._run("${DUP_UNSET_A}", "${DUP_UNSET_B}") is None
        assert self._run("", "sk-x") is None

    def test_ciphertext_compares_by_plaintext(self, monkeypatch):
        import jiuwenswarm.common.config as m

        class FakeCrypto:
            def decrypt(self, c, **k):
                return c.removeprefix("ENC::")

        monkeypatch.setattr(m, "get_crypto_provider", lambda: FakeCrypto())
        assert self._run("ENC::sk-1", "sk-1") is True
        assert self._run("ENC::sk-1", "sk-2") is False

    def test_reference_compares_by_declaration(self, monkeypatch):
        # Same declaration -> duplicate; different command -> distinct even
        # if both would resolve to the same secret; reference vs plaintext
        # -> distinct.
        assert self._run("exec:op read a", "exec:op read a") is True
        assert self._run("exec:op read a", "exec:op read b") is False
        assert self._run("exec:op read a", "sk-resolved-same") is False


class TestPendingClaim:
    """The pending placeholder keeps tracks from double-executing."""

    def test_pending_blocks_second_track(self):
        ref = "exec:cmd-x"
        assert resolver._claim_for_execution(ref) is True
        assert resolver._claim_for_execution(ref) is False  # second track: no-op

    def test_resolve_now_noop_while_pending(self, monkeypatch):
        ref = "exec:cmd-y"
        ran = []
        monkeypatch.setattr(resolver, "_exec_secret_command", lambda r: ran.append(r) or ("v", ""))
        resolver._RESOLVED_SECRETS[ref] = {"value": "", "status": "pending", "ts": time.monotonic()}
        resolver._resolve_now(ref, field_path="f")
        assert ran == []
        assert resolver._RESOLVED_SECRETS[ref]["status"] == "pending"

    def test_bypass_backoff_reclaims_failed_entry(self, monkeypatch):
        ref = "exec:cmd-z"
        resolver._record(ref, "", "failed")  # fresh failure -> backoff active
        ran = []
        monkeypatch.setattr(resolver, "_exec_secret_command", lambda r: ran.append(r) or ("v", ""))
        assert resolver._claim_for_execution(ref) is False  # queue track: backoff
        assert resolver._claim_for_execution(ref, bypass_backoff=True) is True  # save track
        assert len(ran) == 0  # claim alone does not execute

    def test_release_replaces_pending(self):
        ref = "exec:cmd-w"
        resolver._claim_for_execution(ref)
        resolver._release_claim(ref, "sk", "resolved")
        entry = resolver._RESOLVED_SECRETS[ref]
        assert entry["status"] == "resolved" and entry["value"] == "sk"

    def test_stale_pending_is_preempted(self, monkeypatch):
        """A pending claim older than 2x the subprocess timeout has a dead
        owner and must not block later tracks forever (review-4 P2-3).
        _resolve_now does its own claim, so no manual claim here."""
        ref = "exec:cmd-stale"
        resolver._RESOLVED_SECRETS[ref] = {
            "value": "", "status": "pending",
            "ts": time.monotonic() - resolver._STALE_PENDING_SECONDS - 1,
        }
        ran = []
        monkeypatch.setattr(resolver, "_exec_secret_command", lambda r: ran.append(r) or ("v", ""))
        resolver._resolve_now(ref, field_path="f")
        assert ran == [ref]
        assert resolver._RESOLVED_SECRETS[ref]["status"] == "resolved"

    def test_fresh_pending_not_preempted(self):
        ref = "exec:cmd-fresh"
        resolver._RESOLVED_SECRETS[ref] = {"value": "", "status": "pending", "ts": time.monotonic()}
        assert resolver._claim_for_execution(ref) is False


class TestSaveTrackOutcome:
    """The save track reports resolve success/failure to the save response."""

    def test_success_returns_value(self, monkeypatch):
        ref = "exec:save-ok"
        monkeypatch.setattr(resolver, "_exec_secret_command", lambda r: ("sk-saved", ""))
        ok, value = resolver.resolve_credential_ref_now(ref, field_path="f")
        assert ok is True and value == "sk-saved"

    def test_failure_reports_false(self, monkeypatch):
        ref = "exec:save-bad"
        monkeypatch.setattr(
            resolver, "_exec_secret_command", lambda r: (None, "exit=1")
        )
        ok, value = resolver.resolve_credential_ref_now(ref, field_path="f")
        assert ok is False and value == ""
        assert resolver._RESOLVED_SECRETS[ref]["status"] == "failed"

    def test_pending_is_not_reported_as_failure(self, monkeypatch):
        """A live pending entry means another track is resolving the same
        command; the save response must not report a failure for it
        (review-4 second-pass P3-B)."""
        ref = "exec:save-pending"
        # Another track holds a fresh claim; _exec_secret_command never runs
        # on this call (the claim is declined), so no stub is needed — but
        # guard against accidental execution anyway.
        ran = []
        monkeypatch.setattr(resolver, "_exec_secret_command", lambda r: ran.append(r) or ("v", ""))
        resolver._RESOLVED_SECRETS[ref] = {"value": "", "status": "pending", "ts": time.monotonic()}
        ok, value = resolver.resolve_credential_ref_now(ref, field_path="f")
        assert ok is True and value == ""
        assert ran == []
        assert resolver._RESOLVED_SECRETS[ref]["status"] == "pending"  # untouched

    def test_already_resolved_returns_cached(self, monkeypatch):
        ref = "exec:save-cached"
        ran = []
        monkeypatch.setattr(resolver, "_exec_secret_command", lambda r: ran.append(r) or ("v", ""))
        resolver._record(ref, "sk-cached", "resolved")
        ok, value = resolver.resolve_credential_ref_now(ref, field_path="f")
        assert ok is True and value == "sk-cached"
        assert ran == []  # cached: no rerun


class TestProbeErrorWording:
    """A broken reference must not surface as a generic 'field required' error."""

    def test_broken_reference_error_mentions_exec(self):
        """The probe error message names the failing command — the user filled
        api_key with a reference; a generic required-fields error makes them
        think the value vanished."""
        from jiuwenswarm.common.config_panel import models_handlers as mh

        captured: dict = {}

        class _Channel:
            async def send_response(self, ws, req_id, *, ok, error, code, payload=None):
                captured.update(ok=ok, error=error, code=code)

        import asyncio

        async def _run():
            await mh.config_validate_model_handler(
                _Channel(), None, "req-1",
                {
                    "api_base": "https://x",
                    "api_key": "exec:missing-cmd-xyz",
                    "model": "m",
                    "model_provider": "OpenAI",
                },
            )

        asyncio.run(_run())
        assert captured["ok"] is False
        assert "exec:" in captured["error"]
        assert "missing-cmd-xyz" in captured["error"]
        assert "failed to resolve" in captured["error"]

    def test_plain_missing_key_keeps_generic_error(self):
        """An actually-empty api_key (no reference) keeps the original generic
        wording — the specific message must not misfire on plain input."""
        from jiuwenswarm.common.config_panel import models_handlers as mh

        captured: dict = {}

        class _Channel:
            async def send_response(self, ws, req_id, *, ok, error, code, payload=None):
                captured.update(ok=ok, error=error, code=code)

        import asyncio

        async def _run():
            await mh.config_validate_model_handler(
                _Channel(), None, "req-1",
                {"api_base": "https://x", "api_key": "", "model": "m", "model_provider": "OpenAI"},
            )

        asyncio.run(_run())
        assert captured["ok"] is False
        assert "required" in captured["error"]
        assert "exec:" not in captured["error"]
