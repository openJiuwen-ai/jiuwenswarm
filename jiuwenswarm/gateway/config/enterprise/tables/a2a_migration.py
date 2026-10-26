"""Upgrade A2A user ownership; legacy rows remain unowned."""

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from .a2a_models import A2A_OUTBOUND_DISPATCH_TABLE_DEF, A2A_OUTBOUND_USER_STATE_TABLE_DEF


async def ensure_user_state_identity(engine: AsyncEngine) -> None:
    """Replace the template-only unique index with a per-user identity."""
    table_name = A2A_OUTBOUND_USER_STATE_TABLE_DEF.table_name
    index_name = f"ix_{table_name}_template_id_user_id"

    def upgrade(connection):
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return
        quote = connection.dialect.identifier_preparer.quote
        table = quote(table_name)
        if not any(col["name"] == "user_id" for col in inspector.get_columns(table_name)):
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN user_id VARCHAR(256) NULL"))
        indexes = inspector.get_indexes(table_name)
        # Create the replacement before removing the old uniqueness constraint.
        if not any(
            idx["unique"] and idx["column_names"] == ["template_id", "user_id"]
            for idx in indexes
        ):
            connection.execute(text(
                f"CREATE UNIQUE INDEX {quote(index_name)} ON {table} (template_id, user_id)"
            ))
        for idx in indexes:
            if idx["unique"] and idx["column_names"] == ["template_id"]:
                suffix = f" ON {table}" if connection.dialect.name == "mysql" else ""
                connection.execute(text(f"DROP INDEX {quote(idx['name'])}{suffix}"))

    def ready(connection):
        inspector = inspect(connection)
        indexes = inspector.get_indexes(table_name)
        return (
            any(col["name"] == "user_id" for col in inspector.get_columns(table_name))
            and any(idx["unique"] and idx["column_names"] == ["template_id", "user_id"] for idx in indexes)
            and not any(idx["unique"] and idx["column_names"] == ["template_id"] for idx in indexes)
        )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
    except DBAPIError as exc:
        # Only tolerate concurrent upgrades after verifying the complete schema.
        async with engine.connect() as connection:
            if not await connection.run_sync(ready):
                raise exc


async def ensure_dispatch_user_column(engine: AsyncEngine) -> None:
    """Add or widen the nullable owner without assigning legacy ownership."""
    table_name = A2A_OUTBOUND_DISPATCH_TABLE_DEF.table_name
    column = next(
        col
        for col in A2A_OUTBOUND_DISPATCH_TABLE_DEF.columns
        if col.name == "source_user_id"
    )

    def owner_column(connection):
        inspector = inspect(connection)
        if not inspector.has_table(table_name):
            return None
        return next(
            (col for col in inspector.get_columns(table_name) if col["name"] == column.name),
            None,
        )

    def needs_widening(connection, owner):
        # SQLite does not enforce VARCHAR length; no table rebuild is needed.
        length = getattr(owner["type"], "length", None)
        return (
            connection.dialect.name in {"mysql", "postgresql"}
            and length is not None and length < column.length
        )

    def upgrade(connection):
        if not inspect(connection).has_table(table_name):
            return
        owner = owner_column(connection)
        quote = connection.dialect.identifier_preparer.quote
        prefix = f"ALTER TABLE {quote(table_name)}"
        field = f"{quote(column.name)} VARCHAR({column.length})"
        if owner is None:
            statement = f"{prefix} ADD COLUMN {field} NULL"
        elif not needs_widening(connection, owner):
            return
        elif connection.dialect.name == "mysql":
            statement = f"{prefix} MODIFY COLUMN {field} NULL"
        else:
            statement = (
                f"{prefix} ALTER COLUMN {quote(column.name)} TYPE VARCHAR({column.length})"
            )
        connection.execute(text(statement))

    def owner_ready(connection):
        owner = owner_column(connection)
        return owner is not None and not needs_widening(connection, owner)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
    except DBAPIError as exc:
        # Another Gateway may have added or widened it concurrently. Verify after rollback;
        # all other failures must prevent startup with a partially upgraded table.
        async with engine.connect() as connection:
            if not await connection.run_sync(owner_ready):
                raise exc
