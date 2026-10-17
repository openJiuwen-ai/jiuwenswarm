"""L4: persistence gateway."""

from __future__ import annotations

from jiuwenswarm.common.secrets.persistence.db import DbMediumAdapter
from jiuwenswarm.common.secrets.persistence.default_file import DefaultFileStorageBackend
from jiuwenswarm.common.secrets.persistence.env import EnvMediumAdapter
from jiuwenswarm.common.secrets.persistence.file import FileMediumAdapter
from jiuwenswarm.common.secrets.registry import DefaultLocation, StorageLocation, StorageTarget


class PersistenceGateway:
    def __init__(
        self,
        *,
        env_adapter: EnvMediumAdapter,
        file_adapter: FileMediumAdapter,
        db_adapter: DbMediumAdapter | None = None,
        default_backend: DefaultFileStorageBackend,
    ) -> None:
        self._env = env_adapter
        self._file = file_adapter
        self._db = db_adapter or DbMediumAdapter()
        self._default = default_backend

    def read(self, target: StorageTarget) -> str:
        if isinstance(target, DefaultLocation):
            return self._default.read(target.logical_key)
        return self._read_location(target)

    def write(self, target: StorageTarget, raw: str) -> None:
        if isinstance(target, DefaultLocation):
            self._default.write(target.logical_key, raw)
            return
        self._write_location(target, raw)

    def delete(self, target: StorageTarget) -> None:
        if isinstance(target, DefaultLocation):
            self._default.delete(target.logical_key)
            return
        self._delete_location(target)

    def _read_location(self, loc: StorageLocation) -> str:
        if loc.medium == "env":
            return self._env.read_raw(loc)
        if loc.medium == "file":
            return self._file.read_raw(loc)
        if loc.medium == "db":
            return self._db.read_raw(loc)
        raise ValueError(f"unknown medium: {loc.medium}")

    def _write_location(self, loc: StorageLocation, raw: str) -> None:
        if loc.medium == "env":
            self._env.write_raw(loc, raw)
        elif loc.medium == "file":
            self._file.write_raw(loc, raw)
        elif loc.medium == "db":
            self._db.write_raw(loc, raw)
        else:
            raise ValueError(f"unknown medium: {loc.medium}")

    def _delete_location(self, loc: StorageLocation) -> None:
        if loc.medium == "env":
            self._env.delete_raw(loc)
        elif loc.medium == "file":
            self._file.delete_raw(loc)
        elif loc.medium == "db":
            self._db.delete_raw(loc)
        else:
            raise ValueError(f"unknown medium: {loc.medium}")
