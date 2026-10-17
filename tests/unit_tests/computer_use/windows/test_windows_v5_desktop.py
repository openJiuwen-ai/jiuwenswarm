"""Desktop-scope contracts through the production C# runtime; fake OS I/O."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from .test_windows_v5_execution import compile_probe


@unittest.skipUnless(os.environ.get('COMPUTER_USE_WINDOWS_TEST_DOTNET'), 'standalone .NET SDK not configured')
class WindowsDesktopRuntimeTests(unittest.TestCase):
    def test_exclusive_desktop_keyboard_pointer_scope_and_partial_input(self):
        with tempfile.TemporaryDirectory(prefix='windows-desktop-') as folder:
            out = Path(folder)
            sdk, env = compile_probe(self, out, 'NativeDesktopTests')
            result = subprocess.run([str(sdk / 'dotnet'), str(out / 'probe.dll')],
                                    capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertGreaterEqual(json.loads(result.stdout)['assertions'], 20)
