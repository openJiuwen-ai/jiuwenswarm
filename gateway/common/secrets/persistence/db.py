"""DB medium adapter stub (enterprise integration pending)."""

from __future__ import annotations

from jiuwenswarm.common.secrets.registry import StorageLocation


class DbMediumAdapter:
    def read_raw(self, loc: StorageLocation) -> str:
        raise NotImplementedError(
            f"medium=db is not available yet (path={loc.path!r}); integrate PersistenceGateway db adapter"
        )

    def write_raw(self, loc: StorageLocation, raw: str) -> None:
        raise NotImplementedError(
            f"medium=db is not available yet (path={loc.path!r}); integrate PersistenceGateway db adapter"
        )

    def delete_raw(self, loc: StorageLocation) -> None:
        raise NotImplementedError(
            f"medium=db is not available yet (path={loc.path!r}); integrate PersistenceGateway db adapter"
        )
