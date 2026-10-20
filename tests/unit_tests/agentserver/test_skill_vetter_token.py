from jiuwenswarm.server.runtime.skill.skill_vetter.store import (
    consume_vet_token,
    invalidate_vet_tokens,
    issue_vet_token,
    remove_skill_hash,
)


def test_issue_then_consume_is_single_use():
    state = {}
    token = issue_vet_token(state, "s", "h1")
    assert token
    assert consume_vet_token(state, "s", "h1", token) is True
    assert consume_vet_token(state, "s", "h1", token) is False


def test_issue_replaces_prior_token():
    state = {}
    first = issue_vet_token(state, "s", "h1")
    second = issue_vet_token(state, "s", "h1")
    assert first != second
    assert consume_vet_token(state, "s", "h1", first) is False
    assert consume_vet_token(state, "s", "h1", second) is True


def test_wrong_token_hash_or_name_fails_closed():
    state = {}
    token = issue_vet_token(state, "s", "h1")
    assert consume_vet_token(state, "s", "h1", "nope") is False
    assert consume_vet_token(state, "s", "h2", token) is False
    assert consume_vet_token(state, "other", "h1", token) is False
    # None of the failures consumed the live token.
    assert consume_vet_token(state, "s", "h1", token) is True


def test_missing_entry_fails_closed():
    assert consume_vet_token({}, "s", "h1", "tok") is False
    assert (
        consume_vet_token({"skill_vet": {"tokens": "nope"}}, "s", "h1", "tok") is False
    )
    assert consume_vet_token({}, "s", "", "") is False


def test_invalidate_removes_token_and_is_defensive():
    state = {}
    token = issue_vet_token(state, "s", "h1")
    invalidate_vet_tokens(state, "s")
    assert consume_vet_token(state, "s", "h1", token) is False
    invalidate_vet_tokens(state, "s")
    invalidate_vet_tokens({"skill_vet": "nope"}, "s")
    invalidate_vet_tokens({}, "")


def test_remove_skill_hash_drops_token_and_baseline():
    state = {"skill_vet": {"skill_hashes": {"s": "h1"}}}
    token = issue_vet_token(state, "s", "h1")
    assert remove_skill_hash(state, "s") is True
    assert "s" not in state["skill_vet"]["skill_hashes"]
    assert consume_vet_token(state, "s", "h1", token) is False
