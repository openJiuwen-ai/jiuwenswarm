# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Use the ProcessRuntime filesystem model and bubblewrap mount ordering."""

from pathlib import Path

from jiuwenbox.models.policy import BindMount, FilesystemPolicy, ProcessPolicy, SecurityPolicy
from jiuwenbox.supervisor.bwrap import BwrapConfig
from jiuwenbox.supervisor.filesystem import materialize_directories, materialize_files

from ._policy import RUNTIME_LINKS, under, validate_filesystem


def _unique_binds(mounts: list[BindMount]) -> list[BindMount]:
    """Keep the last exact occurrence without reordering distinct overrides."""
    seen = set()
    result = []
    for mount in reversed(mounts):
        key = (mount.host_path, mount.sandbox_path, mount.mode)
        if key not in seen:
            seen.add(key)
            result.append(mount)
    return list(reversed(result))


def build_mounts(config: dict, plan: dict) -> BwrapConfig:
    filesystem = FilesystemPolicy.model_validate_json(plan["filesystem_json"])
    filesystem.bind_mounts[:0] = [
        BindMount(host_path=row["source"], sandbox_path=row["target"], mode="ro") for row in config["runtime"]
    ]
    filesystem.bind_mounts = _unique_binds(filesystem.bind_mounts)
    uid, gid = plan["identity"]
    policy = SecurityPolicy(
        name="lite", filesystem_policy=filesystem,
        process=ProcessPolicy(run_as_user=str(uid), run_as_group=str(gid)),
    )
    bwrap = BwrapConfig.from_policy(policy, [])
    # A bulk mount at / can expand into /jiuwenbox even though its declared
    # target was valid. Check actual targets before adding our trusted bind.
    validate_filesystem(FilesystemPolicy(
        read_only=[target for _, target in (*bwrap.ro_binds, *bwrap.rw_binds)],
    ))
    bwrap.uid, bwrap.gid = uid, gid
    bwrap.unshare_net = plan["mode"] != "host"
    bwrap.unshare_cgroup = False
    bwrap.cap_drop = ["ALL"]
    bwrap.as_pid_1 = True

    # Deployments provide the interpreter and its libraries. Policy mounts
    # retain the same later-overrides-earlier semantics as ProcessRuntime.
    bwrap.ro_binds.append((str(Path(config["package_root"]) / "jiuwenbox"), "/jiuwenbox"))
    if filesystem.directories or filesystem.files:
        root = Path(config["backing_root"])
        directory_binds = materialize_directories(
            filesystem.directories, root / "directories", policy, config["uid"], config["gid"],
        )
        file_binds = materialize_files(
            filesystem.files, root / "files", policy, config["uid"], config["gid"],
        )
        bwrap.rw_binds.extend(
            (row["host_path"], row["sandbox_path"]) for row in (*directory_binds, *file_binds)
        )

    # These paths belong to the fresh temporary root unless policy mounts
    # supply a persistent directory at the same destination.
    for path, permissions in (("/tmp", "01777"), ("/dev/shm", "01777"), ("/run", None)):
        bwrap.add_dir_mount(path, permissions)
    targets = [target for _, target in (*bwrap.ro_binds, *bwrap.rw_binds)]
    bwrap.symlinks = [
        (source, target) for target, source in RUNTIME_LINKS.items()
        if "/" not in targets and target not in targets and any(under("/" + source, mounted) for mounted in targets)
    ]
    bwrap.extra_args = ["--new-session", "--clearenv"]
    bwrap.env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    return bwrap
