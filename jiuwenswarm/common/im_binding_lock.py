"""Cross-process exclusion between application rebinding and durable IM acceptance."""

from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import tempfile

from filelock import AsyncFileLock

from jiuwenswarm.common.utils import get_config_file


@asynccontextmanager
async def im_binding_lock(platform: str, app_id: str):
    """Lock one configured app across Gateway and AgentServer, without exposing its ID."""
    identity = "\0".join((os.path.normcase(str(Path(get_config_file()).resolve())), platform, app_id))
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    directory = Path(tempfile.gettempdir()) / "workswarm-im-binding-locks"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    async with AsyncFileLock(str(directory / (key + ".lock")), timeout=30):
        yield
