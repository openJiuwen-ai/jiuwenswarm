# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The deployment archive contains one implementation and runnable old entrypoints."""
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]


class DeploymentPackageTests(unittest.TestCase):
    def test_current_and_compatibility_package_entries_ship_the_same_implementation(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            for entry in ("yuanrong", "adx"):
                with self.subTest(entry=entry):
                    archive = work / (entry + " deployment.tar.gz")
                    result = subprocess.run(
                        ["bash", str(ROOT / "deploy" / entry / "package.sh"), str(archive)],
                        cwd=work, capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    with tarfile.open(archive) as tar:
                        files = {item.name for item in tar if item.isfile()}
                        canonical = {
                            "deploy.sh", "service.py", "settings.yaml",
                            "jiuwenswarm_handler.sh", "gateway_handler.sh", "web_handler.sh",
                        }
                        self.assertTrue({"deploy/yuanrong/" + name for name in canonical} <= files)
                        self.assertEqual({name for name in files if name.startswith("deploy/adx/")},
                                         {"deploy/adx/deploy.sh", "deploy/adx/package.sh"})
                        for owned_by_os in ("configure.py", "migration.py", "refresh_user_config.py", "refresh-user-config.sh"):
                            self.assertNotIn("deploy/yuanrong/" + owned_by_os, files)
                        self.assertFalse(any("__pycache__" in name or name.endswith(".pyc") for name in files))
                        if sys.platform.startswith("linux"):
                            extracted = work / entry
                            tar.extractall(extracted)
                            for launch in ("yuanrong", "adx"):
                                help_result = subprocess.run(
                                    ["bash", str(extracted / "deploy" / launch / "deploy.sh"), "--help"],
                                    cwd=work, capture_output=True, text=True,
                                    env=dict(os.environ, SWARM_PYTHON=sys.executable),
                                )
                                self.assertEqual(help_result.returncode, 0, help_result.stderr)
                                self.assertIn("install|up|down|restart|status|uninstall",
                                              help_result.stdout)
