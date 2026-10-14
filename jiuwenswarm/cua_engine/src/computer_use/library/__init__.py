from pathlib import Path
import os
import sys
from urllib.parse import urlsplit, unquote
from urllib.request import url2pathname


def resolveLibraryRoot(configured=None, home=None):
    home=Path.home() if home is None else Path(home)
    configured=os.environ.get("COMPUTER_USE_PACKAGES_DIR") if configured is None else configured
    directory=configured or str(home/".computer-use/packages")
    if directory.startswith("~/") or sys.platform=="win32" and directory.startswith("~\\"):directory=str(home/directory[2:])
    if not os.path.isabs(directory) or "\0" in directory:
        raise ValueError("COMPUTER_USE_PACKAGES_DIR must be an absolute filesystem path (or start with ~/). No package directory was selected; correct the MCP server environment.")
    return Path(os.path.abspath(directory)).as_uri().rstrip("/")+"/"


def fileURLToPath(url):
    parts=urlsplit(url)
    if parts.scheme!="file" or parts.netloc not in ("","localhost") or parts.query or parts.fragment:
        raise ValueError("Expected a local file URL without query or fragment")
    value=unquote(parts.path,encoding="utf-8",errors="strict")
    if "\0" in value:raise ValueError("File URL contains NUL")
    # file:///C:/... has a URL-leading slash, not a Windows drive root.
    # Decode the original escaped path exactly once using the OS converter.
    return Path(url2pathname(parts.path))


def pathToFileURL(path):return Path(path).absolute().as_uri()
