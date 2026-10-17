"""Portable production capture geometry; native pixels require a Windows host."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[4] / "jiuwenswarm/cua_engine"
TESTS = Path(__file__).resolve().parent


@unittest.skipUnless(os.environ.get('COMPUTER_USE_WINDOWS_TEST_DOTNET'), 'standalone .NET SDK not configured')
class WindowsCaptureGeometryTests(unittest.TestCase):
    def test_physical_coordinates_clipping_and_allocation_bounds(self):
        sdk = Path(os.environ['COMPUTER_USE_WINDOWS_TEST_DOTNET'])
        compilers = sorted((sdk / 'sdk').glob('*/Roslyn/bincore/csc.dll'))
        refs = sorted((sdk / 'packs/Microsoft.NETCore.App.Ref').glob('*/ref/net8.0'))
        runtimes = sorted((sdk / 'shared/Microsoft.NETCore.App').glob('8.*'))
        self.assertTrue(compilers and refs and runtimes)
        with tempfile.TemporaryDirectory(prefix='windows-v5-capture-probe-') as temporary:
            out = Path(temporary)
            env = dict(os.environ, DOTNET_ROOT=str(sdk), DOTNET_CLI_HOME=str(out / 'home'),
                       DOTNET_CLI_TELEMETRY_OPTOUT='1', DOTNET_SKIP_FIRST_TIME_EXPERIENCE='1')
            command = [str(sdk / 'dotnet'), 'exec', str(compilers[-1]), '/nologo', '/noconfig', '/nostdlib+',
                       # This geometry-only assembly also compiles Wire.Request,
                       # whose desktop execution fields are deliberately unused.
                       '/langversion:5', '/target:exe', '/warnaserror+', '/nowarn:0649', f'/out:{out / "probe.dll"}']
            command += ['/reference:' + str(p) for p in sorted(refs[-1].glob('*.dll'))]
            command += [str(TESTS / p.removeprefix('Tests/')) if p.startswith('Tests/') else str(ROOT / 'backends/windows' / p) for p in (
                'Wire.cs', 'NativeCaptureGeometry.cs', 'Tests/NativeCaptureGeometryTests.cs')]
            compiled = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            (out / 'probe.runtimeconfig.json').write_text(json.dumps(dict(runtimeOptions=dict(
                tfm='net8.0', framework=dict(name='Microsoft.NETCore.App', version=runtimes[-1].name)))))
            result = subprocess.run([str(sdk / 'dotnet'), str(out / 'probe.dll')],
                                    capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report['assertions'], 17)
            self.assertEqual(report['coordinates']['toNative'], [1.875, 0, 0, 1.875, -1920, -240])
            self.assertEqual(report['clipped']['x'], 0)


if __name__ == '__main__':
    unittest.main()
