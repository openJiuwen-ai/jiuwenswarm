"""Checkpoint persistence honors the deployment's SQLite journal mode."""
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.runtime import checkpointer


@pytest.mark.asyncio
@pytest.mark.parametrize("config", [
    {"checkpoint": {"sqlite_journal_mode": "DELETE"}},
    {"workspace_sqlite": {"journal_mode": "DELETE"}},
])
async def test_checkpoint_journal_mode_reaches_core_provider(monkeypatch, tmp_path, config):
    monkeypatch.setattr(checkpointer, "_PERSISTENT_CHECKPOINTER_READY", False)
    monkeypatch.setattr(checkpointer, "get_checkpoint_dir", lambda: tmp_path)
    monkeypatch.setattr(checkpointer, "get_config", lambda: config, raising=False)
    monkeypatch.setattr(checkpointer, "PersistenceCheckpointerProvider", lambda: None)
    create = AsyncMock(return_value=object())
    monkeypatch.setattr(checkpointer.CheckpointerFactory, "create", create)
    monkeypatch.setattr(checkpointer.CheckpointerFactory, "set_default_checkpointer", lambda _: None)

    await checkpointer.ensure_persistent_checkpointer()

    assert create.await_args.args[0].conf["db_journal_mode"] == "DELETE"


@pytest.mark.asyncio
async def test_checkpoint_rejects_unknown_sqlite_journal_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(checkpointer, "_PERSISTENT_CHECKPOINTER_READY", False)
    monkeypatch.setattr(checkpointer, "get_checkpoint_dir", lambda: tmp_path)
    monkeypatch.setattr(checkpointer, "get_config", lambda: {
        "checkpoint": {"sqlite_journal_mode": "MEMORY"},
    }, raising=False)
    monkeypatch.setattr(checkpointer, "PersistenceCheckpointerProvider", lambda: None)
    with pytest.raises(RuntimeError, match="persistent checkpointer initialization failed"):
        await checkpointer.ensure_persistent_checkpointer()
