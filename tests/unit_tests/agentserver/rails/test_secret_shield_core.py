# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure-function coverage for the secret-shield vault, detector and tokenizer."""

from __future__ import annotations

import base64

from jiuwenswarm.agents.harness.common.rails.secret_shield.config import (
    SecretShieldConfig,
    load_secret_shield_config,
)
from jiuwenswarm.agents.harness.common.rails.secret_shield.detect import (
    MAX_CANDIDATES,
    collect_candidates,
    looks_like_secret,
)
from jiuwenswarm.agents.harness.common.rails.secret_shield.tokenize import (
    MAX_HINTS_PER_RESULT,
    mask_for,
    mask_text,
    substitute_tokens,
    token_for,
    token_hints,
    tokenize_text,
)
from jiuwenswarm.agents.harness.common.rails.secret_shield.vault import (
    SecretVault,
)

SECRET = "s3cr3t-Value-01"
OTHER_SECRET = "another-Secret-02"


# --------------------------------------------------------------------------- #
# Vault
# --------------------------------------------------------------------------- #
def test_fingerprint_is_stable_and_session_keyed() -> None:
    first = SecretVault(session_key=b"a" * 32)
    second = SecretVault(session_key=b"b" * 32)

    fingerprint = first.fingerprint(SECRET)

    assert fingerprint == first.fingerprint(SECRET)
    assert len(fingerprint) == 16
    assert second.fingerprint(SECRET) != fingerprint


def test_remember_rejects_values_outside_length_bounds() -> None:
    vault = SecretVault()

    assert vault.remember("short") is None
    assert vault.remember("x" * 600) is None
    assert len(vault) == 0


def test_remember_dedupes_and_evicts_least_recently_used() -> None:
    vault = SecretVault(max_entries=2)

    first = vault.remember("first-Secret-01")
    second = vault.remember("second-Secret-02")
    assert first is not None and second is not None
    assert vault.remember("first-Secret-01") is None

    third = vault.remember("third-Secret-03")

    assert third is not None
    # Accessing the first entry above refreshed it; the untouched second
    # entry is the least recently used and is evicted first.
    assert vault.entry(first.fp) is not None
    assert vault.entry(second.fp) is None
    assert vault.evicted_count == 1


def test_defer_evict_keeps_batch_addressable_and_records_tombstones() -> None:
    vault = SecretVault(max_entries=2)

    first = vault.remember("first-Secret-01", defer_evict=True)
    vault.remember("second-Secret-02", defer_evict=True)
    third = vault.remember("third-Secret-03", defer_evict=True)
    assert first is not None and third is not None
    assert len(vault) == 3

    evicted = vault.evict_to_limit()

    assert [entry.fp for entry in evicted] == [first.fp]
    assert len(vault) == 2
    assert vault.was_evicted(first.fp) is True
    assert vault.was_evicted(third.fp) is False


def test_remember_precomputes_encoded_variants() -> None:
    vault = SecretVault()

    entry = vault.remember(SECRET)

    assert entry is not None
    assert base64.b64encode(SECRET.encode()).decode() in entry.variants
    assert SECRET.encode().hex() in entry.variants


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #
def test_collects_key_value_bearer_and_prefixed_credentials() -> None:
    text = (
        'DB_PASSWORD="db-pass-12345"\n'
        "Authorization: Bearer abcdefgh12345678\n"
        "key=sk-abcdefgh12345678\n"
        "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturePart1\n"
    )

    candidates = collect_candidates(text)

    assert "db-pass-12345" in candidates
    assert "abcdefgh12345678" in candidates
    assert "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturePart1" in candidates


def test_assignment_values_stop_at_escaped_line_breaks() -> None:
    text = (
        r"Stdout: DB_PASSWORD=e2e-secret-12345\n"
        r"API_KEY=sk-e2eabcdef12345678\n\nStderr: (empty)"
    )

    candidates = collect_candidates(text)

    assert set(candidates) == {"e2e-secret-12345", "sk-e2eabcdef12345678"}


def test_collects_quoted_json_keys() -> None:
    candidates = collect_candidates('{"api_key": "json-secret-123", "ok": true}')

    assert "json-secret-123" in candidates


def test_excludes_urls_data_uris_and_pure_hex_digests() -> None:
    text = (
        'href="data:text/plain;base64,YWJjZGVmZ2hpams="\n'
        f"sha256={('a' * 64)}\n"
        "token=https://example.com/path?x=1\n"
    )

    assert collect_candidates(text) == []


def test_candidates_are_deduped_and_bounded() -> None:
    text = "\n".join(f"token=value-{index:06d}" for index in range(MAX_CANDIDATES + 20))

    candidates = collect_candidates(text)

    assert len(candidates) == MAX_CANDIDATES
    assert len(set(candidates)) == len(candidates)


def test_looks_like_secret_rejects_noise() -> None:
    assert looks_like_secret("x") is False
    assert looks_like_secret("data:image/png;base64,AAAA") is False
    assert looks_like_secret("a" * 64) is False
    assert looks_like_secret(SECRET) is True


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #
def test_tokenize_text_replaces_plaintext_and_encoding_variants() -> None:
    vault = SecretVault()
    entry = vault.remember(SECRET)
    assert entry is not None
    encoded = base64.b64encode(SECRET.encode()).decode()
    text = f"raw={SECRET} encoded={encoded}"

    tokenized, hits = tokenize_text(text, vault)

    assert SECRET not in tokenized
    assert encoded not in tokenized
    assert token_for(entry.fp) in tokenized
    assert hits == [entry.fp]


def test_tokenize_text_prefers_longer_values() -> None:
    vault = SecretVault()
    long_entry = vault.remember("abcdefgh-1234")
    short_entry = vault.remember("abcdefgh")
    assert long_entry is not None and short_entry is not None

    tokenized, _ = tokenize_text("abcdefgh-1234", vault)

    assert tokenized == token_for(long_entry.fp)


def test_mask_text_masks_plaintext_tokens_and_variants() -> None:
    vault = SecretVault()
    entry = vault.remember(SECRET)
    assert entry is not None
    text = f"{SECRET} {token_for(entry.fp)}"

    masked, hits = mask_text(text, vault)

    assert masked == f"{mask_for(entry.fp)} {mask_for(entry.fp)}"
    assert hits == [entry.fp]


def test_substitute_tokens_resolves_known_and_keeps_unknown() -> None:
    vault = SecretVault()
    entry = vault.remember(SECRET)
    assert entry is not None
    unknown = "ffffffffffffffff"

    text = f"{token_for(entry.fp)} {token_for(unknown)}"
    resolved_text, resolved = substitute_tokens(text, vault)

    assert resolved_text == f"{SECRET} {token_for(unknown)}"
    assert resolved == [entry.fp]


def test_token_hints_are_deduped_and_capped() -> None:
    fps = [f"{index:016x}" for index in range(MAX_HINTS_PER_RESULT + 3)]

    hints = token_hints([*fps, fps[0]])

    assert hints.count("JIUWEN_SECRET::") == MAX_HINTS_PER_RESULT


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def test_config_defaults_to_observe() -> None:
    config = load_secret_shield_config({})

    assert config.tool_result_mode == "observe"
    assert config.exec_gate_mode == "observe"
    assert config.output_mode == "observe"


def test_config_rejects_invalid_modes_and_keeps_positive_bounds() -> None:
    config = load_secret_shield_config(
        {
            "tool_result_mode": "enforce",
            "output_mode": "bogus",
            "vault_max_entries": 0,
        }
    )

    assert config.tool_result_mode == "enforce"
    assert config.output_mode == "observe"
    assert config.vault_max_entries == 64


def test_config_section_tolerates_non_mapping() -> None:
    assert load_secret_shield_config(None) == SecretShieldConfig()
