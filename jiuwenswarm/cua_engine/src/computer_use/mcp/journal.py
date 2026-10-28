import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .._json import dumps
from ..client.diagnostics import SDK_VERSION


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Journal:

    def __init__(self):
        directory = Path(os.environ.get("COMPUTER_USE_LOG_DIR", str(Path.home() / ".computer-use/logs")))
        self.path = directory / (now().replace(":", "-").replace(".", "-") + "-" + str(uuid.uuid4()) + ".jsonl")
        self._error = None
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            self.record(
                dict(kind="session", sdkVersion=SDK_VERSION, platform=sys.platform, python=sys.version.split()[0],
                     desktopMode=os.environ.get("COMPUTER_USE_DESKTOP", "shared")))
        except Exception as error:
            self._error = str(error)

    def record(self, event):
        if self._error is not None:
            return
        try:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(dumps({"timestamp": now(), **event}) + "\n")
        except Exception as error:
            self._error = str(error)

    def metadata(self):
        return dict(logError=self._error) if self._error is not None else dict(logPath=str(self.path))
