# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run from jiuwenbox: PYTHONPATH=src python tests/enclio/example_full_policy.py /tmp/enclio-demo

Requires Linux, Bubblewrap, libseccomp, public HTTPS, and an isolated writable
cgroup management view. Install Python dependencies with: pip install -e .
The caller-owned root retains workspace/backing files after sandbox cleanup.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

from jiuwenbox.enclio import CommandRequest, Deployment, Endpoint, LiteSandboxEngine, Mount, Network, SandboxPolicy
from jiuwenbox.models.policy import (
    ArchitectureSyscallPolicy, BindMount, BindRootEntries, CgroupPolicy, DeviceMount,
    DirectoryMount, FileMount, FilesystemPolicy, ProcessPolicy, SyscallPolicy,
)

TOOL_PROGRAM = """
import ctypes,errno,json,os,platform,stat,sys,urllib.request
from pathlib import Path

def check(condition, message):
    if not condition: raise RuntimeError(message)

check(Path.cwd()==Path('/workspace'), 'cwd')
check(os.environ['HOME']=='/home/agent' and os.environ['APP_MODE']=='request', 'environment')
check(Path('/inputs/instructions.txt').read_text()=='approved input', 'bind mount')
check(json.loads(Path('/assets/options.json').read_text())=={'enabled':True}, 'root entries')
check(stat.S_IMODE(Path('/cache').stat().st_mode)==0o700, 'directory permissions')
check(stat.S_IMODE(Path('/state/settings.json').stat().st_mode)==0o600, 'file permissions')
with open('/dev/example-zero','rb') as stream:
    check(stream.read(8)==bytes(8), 'device')
libc=ctypes.CDLL(None,use_errno=True)
nr=173 if platform.machine()=='aarch64' else 110
check(libc.syscall(nr)==-1 and ctypes.get_errno()==errno.EPERM, 'seccomp')
with urllib.request.urlopen('https://example.com/',timeout=10) as response:
    check(response.status==200 and b'example domain' in response.read(65536).lower(), 'HTTPS')
message=sys.stdin.buffer.read().decode()
Path('result.txt').write_text(message)
Path('/cache/last-request').write_text(os.environ['REQUEST_ID'])
Path('/state/settings.json').write_text(json.dumps({'last_request':os.environ['REQUEST_ID']}))
Path.home().joinpath('ready').write_text('ok')
print(json.dumps({'message':message,'request_id':os.environ['REQUEST_ID'],
    'app_mode':os.environ['APP_MODE'],'cwd':str(Path.cwd()),'home':str(Path.home()),'https_ok':True}))
"""


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


async def main(root: Path) -> None:
    workspace, assets, backing = root / "workspace", root / "assets", root / "backing"
    workspace.mkdir(parents=True, exist_ok=True)
    assets.mkdir(exist_ok=True)
    instructions = root / "instructions.txt"
    instructions.write_text("approved input")
    (assets / "options.json").write_text('{"enabled": true}')
    engine = LiteSandboxEngine(Deployment(
        runtime_mounts=(Mount("/usr", "/usr", mode="ro"), Mount("/etc/ssl/certs", "/etc/ssl/certs", mode="ro")),
        python=sys.executable, bwrap=os.environ.get("JIUWENBOX_LITE_BWRAP", "/usr/bin/bwrap"),
        sandbox_python="/usr/bin/python3", service_uid=os.getuid(), service_gid=os.getgid(), backing_root=str(backing),
    ))
    blocked = ["getppid", "unshare", "setns", "mount", "umount2"]
    policy = SandboxPolicy(
        mounts=(Mount(str(workspace), "/workspace"),), default_cwd="/workspace",
        network=Network("isolated", (Endpoint("dns", "example.com", 443),)),
        syscall=SyscallPolicy(
            arm64=ArchitectureSyscallPolicy(blocked=blocked), x86_64=ArchitectureSyscallPolicy(blocked=blocked),
        ),
        limits=CgroupPolicy(memory_max=256 * 1024 * 1024, cpu_max=(100_000, 100_000), pids_max=64),
        version="example-all-members-v1",  # Caller metadata, not an enforced version.
        filesystem_policy=FilesystemPolicy(
            directories=[DirectoryMount(path="/cache", permissions="0700"),
                         DirectoryMount(path="/home/agent", permissions="0700")],
            files=[FileMount(path="/state/settings.json", permissions="0600")],
            read_only=["/usr", "/inputs", "/assets"],
            read_write=["/workspace", "/tmp", "/cache", "/home/agent", "/state/settings.json"],
            bind_mounts=[BindMount(host_path=str(instructions), sandbox_path="/inputs/instructions.txt", mode="ro")],
            bind_root_entries=[BindRootEntries(host_root=str(assets), sandbox_path="/assets", mode="ro")],
            device=[DeviceMount(host_path="/dev/zero", sandbox_path="/dev/example-zero")],
        ),
        environment={"HOME": "/home/agent", "APP_MODE": "policy"},
        process=ProcessPolicy(run_as_user=str(os.getuid()), run_as_group=str(os.getgid())),
    )
    command = CommandRequest(
        argv=("/usr/bin/python3", "-c", TOOL_PROGRAM), cwd=None,
        env={"APP_MODE": "request", "REQUEST_ID": "request-001"}, stdin=b"hello from LiteRuntime", timeout_seconds=30,
    )
    cancel = asyncio.Event()  # Set this event to request cancellation.
    result = await engine.run(policy, command, cancel=cancel)
    check(result.status == "exited" and result.exit_code == 0, repr(result))
    check(result.command_started is True and result.cleanup_complete is True, repr(result))
    check(result.stderr == b"", result.stderr.decode(errors="replace"))
    check(json.loads(result.stdout) == {
        "message": "hello from LiteRuntime", "request_id": "request-001", "app_mode": "request",
        "cwd": "/workspace", "home": "/home/agent", "https_ok": True,
    }, "unexpected output")
    check((workspace / "result.txt").read_text() == "hello from LiteRuntime", "workspace persistence")
    check(instructions.read_text() == "approved input", "read-only input changed")

    def backing_name(path: str) -> str:
        return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")

    cache = backing / "directories" / backing_name("/cache")
    home = backing / "directories" / backing_name("/home/agent")
    settings = backing / "files" / backing_name("/state/settings.json")
    check((cache / "last-request").read_text() == "request-001", "cache persistence")
    check((home / "ready").read_text() == "ok", "home persistence")
    check(json.loads(settings.read_text()) == {"last_request": "request-001"}, "settings persistence")
    check(not (Path("/sys/fs/cgroup/jiuwenbox") / result.run_id).exists(), "task cgroup remains")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="caller-owned directory for example input and output")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("this example requires Linux")
    asyncio.run(main(args.root.resolve()))
