# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import base64
import json

import pytest

from jiuwenswarm.common.auth import session_store as store_mod


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "auth_dir", lambda: tmp_path)
    return tmp_path


def test_envelope_carries_version_alg_and_kid():
    raw = store_mod.encrypt_json({"hello": "世界"})
    envelope = json.loads(raw)
    assert envelope["version"] == 1
    assert envelope["alg"] == "A256GCM"
    assert envelope["kid"] and len(envelope["kid"]) == 8
    assert set(envelope) == {"version", "alg", "kid", "iv", "tag", "data"}
    assert store_mod.decrypt_json(raw) == {"hello": "世界"}


def test_envelope_does_not_leak_plaintext():
    raw = store_mod.encrypt_json({"secret": "SK-LEAK"})
    assert "SK-LEAK" not in raw


def test_kid_is_not_the_key(_isolated):
    key = store_mod._install_key(create=True)
    envelope = json.dumps(json.loads(store_mod.encrypt_json({"a": 1})))
    assert base64.urlsafe_b64encode(key).decode() not in envelope
    assert key.hex() not in envelope


def test_another_machines_key_cannot_decrypt(_isolated, tmp_path):
    ciphertext = store_mod.encrypt_json({"from": "machine-A"})
    other = tmp_path / "machine-b"
    other.mkdir()
    store_mod.auth_dir = lambda: other  # 由 _isolated 的 monkeypatch 负责还原
    with pytest.raises(ValueError, match="无法解密"):
        store_mod.decrypt_json(ciphertext)


def test_garbage_ciphertext_fails_loudly(_isolated):
    for bad in ("", "not-json", json.dumps({"version": 99, "alg": "A256GCM"})):
        with pytest.raises(ValueError):
            store_mod.decrypt_json(bad)


def test_unreadable_key_file_is_never_overwritten(_isolated, monkeypatch):
    from pathlib import Path

    key = store_mod._install_key(create=True)
    key_path = _isolated / store_mod._KEY_FILE_NAME
    original_text = key_path.read_text(encoding="utf-8")

    real_read_text = Path.read_text

    def _locked(self, *args, **kwargs):
        if self == key_path:
            raise PermissionError("file is being used by another process")
        return real_read_text(self, *args, **kwargs)

    with monkeypatch.context() as locked:
        locked.setattr(Path, "read_text", _locked)
        with pytest.raises(OSError):
            store_mod._keyring()
    assert key_path.read_text(encoding="utf-8") == original_text
    assert store_mod._install_key(create=False) == key


def test_corrupt_key_file_is_kept_aside_and_regenerated(_isolated, monkeypatch):
    key_path = _isolated / store_mod._KEY_FILE_NAME
    key_path.write_text("not-a-key", encoding="utf-8")
    errors = []
    monkeypatch.setattr(store_mod.logger, "error", lambda message, *args: errors.append(message % args))

    assert store_mod._install_key(create=False) is None, "只读时不动它"
    key = store_mod._install_key(create=True)

    assert len(key) == 32 and store_mod._install_key(create=False) == key
    (backup,) = _isolated.glob(store_mod._KEY_FILE_NAME + ".corrupt-*")
    assert backup.read_text(encoding="utf-8") == "not-a-key", "损坏的文件留档，方便排查"
    assert len(errors) == 1 and "已损坏" in errors[0], "会话从此解不开，要按 ERROR 记下来"


def test_key_created_by_another_process_first_wins(_isolated, monkeypatch):
    theirs = bytes(range(32))
    key_path = _isolated / store_mod._KEY_FILE_NAME
    real_publish = store_mod._publish_new_file

    def _other_process_got_there_first(tmp, target):
        target.write_text(base64.urlsafe_b64encode(theirs).decode("ascii"), encoding="utf-8")
        return real_publish(tmp, target)

    monkeypatch.setattr(store_mod, "_publish_new_file", _other_process_got_there_first)
    assert store_mod._install_key(create=True) == theirs
    assert store_mod._install_key(create=False) == theirs
    assert [p.name for p in _isolated.iterdir()] == [store_mod._KEY_FILE_NAME], "临时文件要清掉"
    assert key_path.exists()


def test_derived_secrets_are_stable_and_separated_by_label(_isolated):
    first = store_mod.derive_local_secret(b"purpose-a:")
    assert store_mod.derive_local_secret(b"purpose-a:") == first
    assert store_mod.derive_local_secret(b"purpose-b:") != first
    primary, _ = store_mod._keyring()
    assert primary not in (first,), "不能把加密密钥本身交出去"
