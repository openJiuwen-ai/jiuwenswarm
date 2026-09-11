"""A2A migration unit tests using stdlib SQLite and isolated dependency doubles."""

import builtins
import importlib.util
import re
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace

import pytest


class DBAPIError(Exception):
    pass


def _dialect(name):
    return SimpleNamespace(name=name, identifier_preparer=SimpleNamespace(quote=lambda value: value))


class _Definition:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.__dict__.update(kwargs)
        if args:
            self.name = args[0]


@pytest.fixture
def migration():
    # Execute the real source with per-module imports. No global sys.modules stubs
    # and no optional runtime/SQLAlchemy imports leak into other tests.
    root = Path(__file__).resolve().parents[3] / "jiuwenswarm/gateway/config/enterprise/tables"
    dependencies = {
        "openjiuwen_runtime.foundation.db.table_def": SimpleNamespace(
            ColumnDefinition=_Definition, IndexDefinition=_Definition, TableDefinition=_Definition,
        ),
        "sqlalchemy": SimpleNamespace(inspect=lambda connection: connection, text=lambda sql: sql),
        "sqlalchemy.exc": SimpleNamespace(DBAPIError=DBAPIError),
        "sqlalchemy.ext.asyncio": SimpleNamespace(AsyncEngine=object),
    }

    def import_dependency(name, globals=None, locals=None, fromlist=(), level=0):
        if name in dependencies:
            return dependencies[name]
        if name.startswith(("sqlalchemy", "openjiuwen_runtime")):
            raise AssertionError(f"Unexpected optional dependency: {name}")
        return builtins.__import__(name, globals, locals, fromlist, level)

    def load(name):
        spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        module.__dict__["__builtins__"] = {**vars(builtins), "__import__": import_dependency}
        spec.loader.exec_module(module)
        return module

    dependencies["a2a_models"] = load("a2a_models")
    return load("a2a_migration")


class _SQLiteConnection:
    dialect = _dialect("sqlite")

    def __init__(self, db):
        self.db = db

    async def run_sync(self, callback):
        return callback(self)

    def has_table(self, name):
        return self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
        ).fetchone() is not None

    def get_columns(self, name):
        return [
            {"name": row[1], "type": SimpleNamespace(
                length=int(re.search(r"\d+", row[2]).group()) if re.search(r"\d+", row[2]) else None,
            )}
            for row in self.db.execute(f"PRAGMA table_info({name})")
        ]

    def execute(self, statement):
        return self.db.execute(statement)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_length", [None, 64, 256])
async def test_sqlite_migration_preserves_history_and_long_ids(migration, owner_length):
    with closing(sqlite3.connect(":memory:")) as db:
        owner_sql = "" if owner_length is None else f", source_user_id VARCHAR({owner_length}) NULL"
        db.execute(f"CREATE TABLE a2a_outbound_dispatch (dispatch_id TEXT{owner_sql})")
        db.execute("INSERT INTO a2a_outbound_dispatch (dispatch_id) VALUES ('legacy')")
        connection = _SQLiteConnection(db)

        @asynccontextmanager
        async def begin():
            with db:
                yield connection

        engine = SimpleNamespace(begin=begin, connect=begin)
        await migration.ensure_dispatch_user_column(engine)
        await migration.ensure_dispatch_user_column(engine)
        assert db.execute("SELECT source_user_id FROM a2a_outbound_dispatch").fetchall() == [(None,)]
        assert next(
            col for col in migration.A2A_OUTBOUND_DISPATCH_TABLE_DEF.columns
            if col.name == "source_user_id"
        ).length == 256
        db.execute(
            "INSERT INTO a2a_outbound_dispatch VALUES (?, ?)", ("long-id", "u" * 256),
        )
        assert db.execute(
            "SELECT dispatch_id FROM a2a_outbound_dispatch WHERE source_user_id=?", ("u" * 256,),
        ).fetchall() == [("long-id",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect_name,length,expected", [
    ("postgresql", 64, "ALTER COLUMN source_user_id TYPE VARCHAR(256)"),
    ("mysql", 64, "MODIFY COLUMN source_user_id VARCHAR(256) NULL"),
    ("postgresql", 256, None), ("mysql", 512, None), ("sqlite", 64, None),
])
async def test_existing_owner_column_widening(monkeypatch, migration, dialect_name, length, expected):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    a2a_migration = migration

    dialect = _dialect(dialect_name)
    owner = {"name": "source_user_id", "type": SimpleNamespace(length=length)}
    statements = []
    inspector = SimpleNamespace(has_table=lambda name: True, get_columns=lambda name: [owner])
    monkeypatch.setattr(a2a_migration, "inspect", lambda connection: inspector)

    class Connection:
        async def run_sync(self, callback):
            return callback(self)

        def execute(self, statement):
            statements.append(str(statement))
            owner["type"] = SimpleNamespace(length=256)

    connection = Connection()
    connection.dialect = dialect

    @asynccontextmanager
    async def begin():
        yield connection

    engine = SimpleNamespace(begin=begin)
    await a2a_migration.ensure_dispatch_user_column(engine)
    await a2a_migration.ensure_dispatch_user_column(engine)
    assert statements == ([] if expected is None else [f"ALTER TABLE a2a_outbound_dispatch {expected}"])


@pytest.mark.asyncio
@pytest.mark.parametrize("final_length", [64, 256])
async def test_failed_widening_requires_verified_final_length(monkeypatch, migration, final_length):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    a2a_migration = migration

    owner = {"name": "source_user_id", "type": SimpleNamespace(length=64)}
    events = []
    failure = DBAPIError("ALTER TABLE", {}, Exception("DDL failed"))
    inspector = SimpleNamespace(has_table=lambda name: True, get_columns=lambda name: [owner])
    monkeypatch.setattr(a2a_migration, "inspect", lambda connection: inspector)

    class Connection:
        dialect = _dialect("postgresql")

        async def run_sync(self, callback):
            return callback(self)

        def execute(self, statement):
            raise failure

    @asynccontextmanager
    async def begin():
        try:
            yield Connection()
        finally:
            events.append("rollback")
            owner["type"] = SimpleNamespace(length=final_length)

    @asynccontextmanager
    async def connect():
        events.append("verify")
        yield Connection()

    engine = SimpleNamespace(begin=begin, connect=connect)
    if final_length == 256:
        await a2a_migration.ensure_dispatch_user_column(engine)
    else:
        with pytest.raises(DBAPIError) as caught:
            await a2a_migration.ensure_dispatch_user_column(engine)
        assert caught.value is failure
    assert events == ["rollback", "verify"]
