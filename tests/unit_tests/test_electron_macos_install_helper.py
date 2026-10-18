"""Run the real replacement script against disposable bundles and fake OS tools."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Uses macOS ditto and PlistBuddy")
HELPER = Path(__file__).resolve().parents[2] / "jiuwenswarm/channels/desktop/electron/install_macos_update.sh"


def _bundle(directory, version, bundle_id="com.example.workswarm"):
    resources = directory / "Contents/Resources/app"
    resources.mkdir(parents=True)
    (resources / "main.cjs").write_text("// fixture")
    (resources / "package.json").write_text('{"name":"jiuwenswarm-electron"}')
    executable = directory / "Contents/MacOS/Electron"
    executable.parent.mkdir()
    executable.write_text(version)
    (directory / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id, "CFBundleExecutable": "Electron",
    }))


def _tool(directory, name, source):
    target = directory / name
    target.write_text("#!/bin/bash\nset -eu\n" + source)
    target.chmod(0o755)


@pytest.mark.parametrize("mode", ["success", "untrusted", "wrong-id", "swap-failure", "launch-failure", "licensed"])
def test_real_helper_preserves_or_replaces_app(tmp_path, mode):
    destination = tmp_path / "installed" / "Work 'Swarm.app"
    incoming = tmp_path / "incoming" / "Work 'Swarm.app"
    _bundle(destination, "old")
    _bundle(incoming, "new", "wrong.id" if mode == "wrong-id" else "com.example.workswarm")
    tools = tmp_path / "tools"
    tools.mkdir()
    installer = tmp_path / "update.dmg"
    installer.touch()
    log = tmp_path / "update.log"
    _tool(tools, "hdiutil", '''
case "$1" in
verify) exit 0 ;;
attach)
  if [ "$TEST_MODE" = licensed ]; then
    read -r agreement
    test "$agreement" = Y
  fi
  while [ "$1" != '-mountpoint' ]; do shift; done
  /usr/bin/ditto "$FIXTURE_APP" "$2/Work 'Swarm.app"
  ;;
detach) /bin/rm -rf "$2/Work 'Swarm.app" ;;
esac
''')
    _tool(tools, "codesign", "exit 0\n")
    _tool(tools, "lipo", "exit 0\n")
    _tool(tools, "spctl", 'test "$TEST_MODE" != untrusted\n')
    _tool(tools, "open", 'test "$TEST_MODE" != launch-failure\ntest -f "$1/Contents/MacOS/Electron"\n')
    _tool(tools, "mv", '''
if [ "$TEST_MODE" = swap-failure ] && [[ "$1" = */new.app ]]; then exit 1; fi
exec /bin/mv "$@"
''')
    env = {**os.environ, "PATH": f"{tools}:/usr/bin:/bin:/usr/sbin:/sbin",
           "FIXTURE_APP": str(incoming), "TEST_MODE": mode}
    result = subprocess.run(
        ["/bin/bash", str(HELPER), str(installer), str(destination), "99999999",
         "", "", str(log), "arm64"],
        env=env, capture_output=True, text=True, timeout=15, check=False,
    )
    contents = (destination / "Contents/MacOS/Electron").read_text()
    if mode == "success":
        assert result.returncode == 0, log.read_text()
        assert contents == "new"
        assert result.stdout == "READY\n"
    else:
        assert result.returncode != 0, log.read_text()
        assert contents == "old"
        if mode not in {"swap-failure", "launch-failure"}:
            assert "READY" not in result.stdout
    assert not list(destination.parent.glob(".electron-update.*"))
