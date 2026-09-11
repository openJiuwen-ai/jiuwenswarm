"""Upgrade only A2A history ownership; legacy rows remain unowned."""

from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from .a2a_models import A2A_OUTBOUND_DISPATCH_TABLE_DEF


async def ensure_dispatch_user_column(engine: AsyncEngine) -> None:
    """Add the nullable owner before init_table creates its declared index."""
    table_name = A2A_OUTBOUND_DISPATCH_TABLE_DEF.table_name
    column = next(
        col
        for col in A2A_OUTBOUND_DISPATCH_TABLE_DEF.columns
        if col.name == "source_user_id"
    )

    def has_owner(connection):
        inspector = inspect(connection)
        return inspector.has_table(table_name) and any(
            col["name"] == column.name for col in inspector.get_columns(table_name)
        )

    def upgrade(connection):
        if not inspect(connection).has_table(table_name) or has_owner(connection):
            return
        quote = connection.dialect.identifier_preparer.quote
        connection.execute(
            text(
                f"ALTER TABLE {quote(table_name)} ADD COLUMN "
                f"{quote(column.name)} VARCHAR({column.length}) NULL"
            )
        )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
    except DBAPIError:
        # Another Gateway may have added it concurrently. Verify after rollback;
        # all other failures must prevent startup with a partially upgraded table.
        async with engine.connect() as connection:
            if not await connection.run_sync(has_owner):
                raise
