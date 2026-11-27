"""box-supervisor main entry point."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path

import yaml

from jiuwenbox.models.policy import NetworkMode, SecurityPolicy
from jiuwenbox.supervisor.bwrap import BwrapConfig, BwrapProcess
from jiuwenbox.supervisor.landlock import encode_landlock_payload
from jiuwenbox.supervisor.seccomp import open_seccomp_filter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Supervisor:
    """Manages a single sandboxed process with full isolation."""

    def __init__(
        self,
        policy: SecurityPolicy,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.policy = policy
        self.command = command
        self.workdir = workdir
        self.env = env or {}
        self._bwrap: BwrapProcess | None = None
        self._seccomp_fd: int | None = None
        self._landlock_launcher_path: Path | None = None
        self._shutting_down = False

    @classmethod
    def from_policy_file(cls, path: str | Path, command: list[str], **kwargs) -> Supervisor:
        """Create a Supervisor from a policy YAML file."""
        policy_path = Path(path)
        with open(policy_path) as f:
            data = yaml.safe_load(f)
        policy = SecurityPolicy.model_validate(data)
        return cls(policy=policy, command=command, **kwargs)

    def _setup_seccomp(self, config: BwrapConfig) -> None:
        """Generate seccomp filter and configure bwrap to use it."""
        fd = open_seccomp_filter(self.policy.syscall)
        self._seccomp_fd = fd
        config.seccomp_fd = fd

    def _close_seccomp_fd(self) -> None:
        if self._seccomp_fd is None:
            return
        try:
            os.close(self._seccomp_fd)
        except OSError:
            logger.warning("Failed to close seccomp fd", exc_info=True)
        finally:
            self._seccomp_fd = None

    def _setup_network_namespace(self, config: BwrapConfig) -> None:
        """Reuse the pre-configured runtime namespace when one is provided."""
        if self.policy.network.mode != NetworkMode.ISOLATED:
            return

        if os.environ.get("JIUWENBOX_NETNS_READY") == "1":
            config.unshare_net = False

    @staticmethod
    def _setup_policy_directory_mounts(config: BwrapConfig) -> None:
        """Attach server-created lifecycle directories to their sandbox paths."""
        raw_binds = os.environ.get("JIUWENBOX_DIRECTORY_BINDS")
        if not raw_binds:
            return

        for entry in json.loads(raw_binds):
            config.rw_binds.append((entry["host_path"], entry["sandbox_path"]))

    def _setup_landlock_launcher(self, config: BwrapConfig, temp_dir: Path) -> None:
        """Run a fixed in-sandbox launcher that applies Landlock before exec."""
        if self.policy.landlock.compatibility == "disabled":
            return

        launcher_path = Path(__file__).with_name("landlock_launcher.py")
        temp_launcher_path = temp_dir / "jiuwenbox-landlock-launcher.py"
        temp_launcher_path.write_bytes(launcher_path.read_bytes())
        os.chmod(temp_launcher_path, 0o644)
        self._landlock_launcher_path = temp_launcher_path

        sandbox_launcher_path = "/run/jiuwenbox-landlock-launcher.py"
        config.add_dir_mount("/run")
        config.ro_binds.append((str(self._landlock_launcher_path), sandbox_launcher_path))
        config.command = [
            "/usr/bin/python3",
            sandbox_launcher_path,
            encode_landlock_payload(self.policy),
            "--",
            *config.command,
        ]

    async def start(self) -> int:
        """Start the sandbox and return the exit code of the target process."""
        with tempfile.TemporaryDirectory(prefix="jiuwenbox-landlock-") as temp_dir:
            try:
                # 1. Build bwrap config from policy
                config = BwrapConfig.from_policy(self.policy, self.command)
                config.env.update(self.env)
                if self.workdir:
                    config.workdir = self.workdir
                self._setup_network_namespace(config)
                self._setup_policy_directory_mounts(config)
                self._setup_landlock_launcher(config, Path(temp_dir))

                # 2. Setup seccomp
                try:
                    self._setup_seccomp(config)
                except Exception:
                    logger.warning("Failed to setup seccomp, continuing without it", exc_info=True)

                # 3. Register signal handlers
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._shutdown(s)))

                # 4. Start bwrap process
                self._bwrap = BwrapProcess(config)
                self._bwrap.start()
                self._close_seccomp_fd()

                # 5. Wait for process to exit
                return await asyncio.get_running_loop().run_in_executor(
                    None, self._bwrap.wait
                )
            finally:
                self._close_seccomp_fd()
                await self._cleanup()

    async def _shutdown(self, sig: signal.Signals) -> None:
        """Graceful shutdown on signal."""
        if self._shutting_down:
            return
        self._shutting_down = True

        logger.info("Received signal %s, shutting down", sig.name)
        if self._bwrap:
            self._bwrap.stop()
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Clean up resources."""
        self._close_seccomp_fd()
        if self._landlock_launcher_path and self._landlock_launcher_path.exists():
            self._landlock_launcher_path.unlink(missing_ok=True)


async def run_supervisor(
    policy_path: str,
    command: list[str],
    workdir: str | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """High-level entry point: load policy and run the sandbox."""
    supervisor = Supervisor.from_policy_file(policy_path, command, workdir=workdir, env=env)
    return await supervisor.start()


def main() -> int:
    """CLI entry point for box-supervisor (used by box-server to spawn)."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    )

    if len(sys.argv) < 3:
        logger.error("Usage: %s <policy.yaml> <command> [args...]", sys.argv[0])
        return 1

    policy_path = sys.argv[1]
    command = sys.argv[2:]

    return asyncio.run(run_supervisor(policy_path, command))


if __name__ == "__main__":
    raise SystemExit(main())
