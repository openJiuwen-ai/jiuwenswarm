# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the production cleanup function with Bash, without macOS OS tools."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("mode", ["parent-alive", "parent-exited", "foreign-receipt", "extra-file"])
def test_receipt_cleanup_is_owned_and_waits_for_parent_exit(tmp_path, mode):
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is unavailable")
    source = Path(__file__).resolve().parents[2] / "jiuwenswarm/channels/desktop/electron/install_macos_update.sh"
    text = source.read_text(encoding="utf-8")
    start = text.index("cleanup_update_attempt() {")
    cleanup = text[start:text.index("\ncleanup() {", start)]
    updates = tmp_path / "updates with space"
    attempt = updates / "electron-update-test"
    attempt.mkdir(parents=True)
    helper = attempt / "install.sh"
    helper.write_text("fixture", encoding="utf-8")
    receipt = (tmp_path if mode == "foreign-receipt" else attempt) / "shutdown-fixture.result"
    receipt.write_text("pending\n", encoding="utf-8")
    extra = attempt / "unrelated.txt"
    if mode == "extra-file":
        extra.write_text("keep", encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text('set -eu\nHELPER_PATH="$1"\nSHUTDOWN_RECEIPT="$2"\nINSTALLER="$3"\nPARENT_EXITED="$4"\n'
        + cleanup + "\ncleanup_update_attempt\n", encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env.pop("BASH_ENV", None)
    if git_bash.is_file():
        env["PATH"] = str(git_bash.parents[1] / "usr/bin") + os.pathsep + env["PATH"]
    result = subprocess.run([bash, "--noprofile", "--norc", driver.as_posix(), helper.as_posix(),
        receipt.as_posix(), (updates / "update.dmg").as_posix(), "0" if mode == "parent-alive" else "1"],
        env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    if mode in {"parent-alive", "foreign-receipt"}:
        assert helper.is_file() and receipt.read_text(encoding="utf-8") == "pending\n"
    else:
        assert not helper.exists() and not receipt.exists()
        if mode == "extra-file":
            assert extra.read_text(encoding="utf-8") == "keep"
        else:
            assert not attempt.exists()
