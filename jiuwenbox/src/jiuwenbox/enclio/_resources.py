# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-run resources owned by the supervisor and its caller."""

import os
import select
import time
from pathlib import Path

from jiuwenbox.models.policy import CgroupPolicy
from jiuwenbox.supervisor import cgroup

from ._linux import kill_pidfd, pidfd_alive


def possible_groups(run_id: str, limits: CgroupPolicy) -> list[Path]:
    return cgroup.possible_group_paths(run_id, limits)


def _verify_limits(handle: cgroup.CgroupHandle, policy: CgroupPolicy) -> None:
    def value(filename: str) -> str:
        for path in handle.paths:
            field = path / filename
            if field.is_file():
                return field.read_text().strip()
        raise OSError("missing_controller")

    v2 = handle.backend is cgroup.CgroupBackend.V2
    if policy.memory_max is not None:
        actual = int(value("memory.max" if v2 else "memory.limit_in_bytes"))
        if not 0 < actual or abs(actual - policy.memory_max) >= os.sysconf("SC_PAGESIZE"):
            raise ValueError("memory_limit_mismatch")
    if policy.cpu_max is not None:
        actual = (tuple(map(int, value("cpu.max").split())) if v2 else
                  (int(value("cpu.cfs_quota_us")), int(value("cpu.cfs_period_us"))))
        if actual != policy.cpu_max:
            raise ValueError("cpu_limit_mismatch")
    if policy.pids_max is not None and int(value("pids.max")) != policy.pids_max:
        raise ValueError("pid_limit_mismatch")


def cleanup(paths: list[Path], tracked: list[int], seconds: float, lifetime: int | None) -> bool:
    end = time.monotonic() + seconds
    for fd in reversed(tracked):
        kill_pidfd(fd)
    while True:
        clean = True
        for path in paths:
            try:
                events = path / "cgroup.events"
                if events.is_file():
                    if "populated 0" not in events.read_text().splitlines():
                        (path / "cgroup.kill").write_text("1")
                        clean = False
                        continue
                elif (path / "cgroup.procs").read_text().strip():
                    # v1 PIDs are only inspected here. Numeric PID reuse is
                    # avoided by killing registered trees via their pidfds.
                    clean = False
                    continue
                path.rmdir()
            except FileNotFoundError:
                clean &= not path.exists()
            except OSError:
                clean = False
        # 判断 lifetime fd 是否可读，写端对应的是supervisor的proxy_writer，只有在两种情况下变可读：
        # 一种是有新数据到，但在代码上不会使用这个管道发送数据，所以这种情况不会发生；
        # 一种是写端已关闭，supervisor -> proxy -> worker，supervisor创建后会主动关闭，
        # 而proxy和worker会持有写端，只有在proxy退出、worker全部退出的情况下，管道进入关闭状态
        proxy_gone = lifetime is None or bool(select.select([lifetime], [], [], 0)[0])
        # 由于 tracked 只登记了进程 bwrap/proxy/沙箱init，并没有登记 proxy worker，
        # 所以需要通过 lifetime 管道的可读来判断 proxy worker 的状态
        if clean and proxy_gone and not any(pidfd_alive(fd) for fd in tracked):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


class RunResources:
    """Keep cleanup handles until both execution and cleanup have completed."""

    def __init__(self, run_id: str, limits: CgroupPolicy):
        self.candidates = possible_groups(run_id, limits) if not limits.is_empty() else []
        self.paths: list[Path] = []
        # tracked 登记了三个进程，分别是 bwrap/proxy/沙箱init，并没有登记 proxy worker
        self.tracked: list[int] = []
        self.lifetime: int | None = None

    def prepare_groups(self) -> bool:
        if any(path.exists() for path in self.candidates):
            return False
        self.paths = list(self.candidates)
        return True

    def cleanup(self, seconds: float) -> bool:
        return cleanup(self.paths, self.tracked, seconds, self.lifetime)

    def close(self) -> None:
        for fd in self.tracked:
            os.close(fd)
        if self.lifetime is not None:
            os.close(self.lifetime)
