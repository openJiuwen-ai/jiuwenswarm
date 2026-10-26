# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""企业专属表 Repository 注入入口。

企业版启动时由 ``set_enterprise_record_repositories`` 注入；
EE Manager 写路径经 ``require_enterprise_repository``，未注入则 fail-fast。
"""

from __future__ import annotations

from jiuwenswarm.gateway.config.enterprise.repository import EnterpriseRecordRepository

_repos: dict[str, EnterpriseRecordRepository] = {}


def set_enterprise_record_repository(
    store_name: str,
    repo: EnterpriseRecordRepository | None,
) -> None:
    if repo is None:
        _repos.pop(store_name, None)
    else:
        _repos[store_name] = repo


def set_enterprise_record_repositories(
    repos: dict[str, EnterpriseRecordRepository] | None,
) -> None:
    _repos.clear()
    if repos:
        _repos.update(repos)


def get_enterprise_record_repository(store_name: str) -> EnterpriseRecordRepository | None:
    return _repos.get(store_name)


def clear_enterprise_record_repositories() -> None:
    _repos.clear()


def list_injected_store_names() -> tuple[str, ...]:
    return tuple(sorted(_repos))


__all__ = [
    "clear_enterprise_record_repositories",
    "get_enterprise_record_repository",
    "list_injected_store_names",
    "set_enterprise_record_repository",
    "set_enterprise_record_repositories",
]
