# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Callbacks for channel-specific cron behavior."""

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from jiuwenswarm.runtime.cron.models import CronJob
else:
    CronJob = Any


class CronHooks(Protocol):
    def mutation(
        self, job: CronJob, *, action: str, request_channel_id: str
    ) -> None: ...

    def run_metadata(self, job: CronJob) -> dict[str, Any]: ...
