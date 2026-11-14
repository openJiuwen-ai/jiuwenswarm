# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Offline application installation preserves executable entrypoints."""
import importlib.util
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

ROOT = Path(__file__).resolve().parents[3]
INSTALL_ENTRY = (ROOT / "deploy/yuanrong/deploy.sh").read_text().rsplit('main "$@"', 1)[0]
INSTALL_ENTRY = INSTALL_ENTRY.replace('cd "$(dirname "${BASH_SOURCE[0]}")"', 'cd ' + shlex.quote(str(ROOT / 'deploy/yuanrong')))


@unittest.skipUnless(sys.platform.startswith("linux"), "Requires Linux Bash 4+")
class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.key = self.root / "tenant-key"
        self.key.write_text("test-only-fixture")
        self.settings = {
            "python": "/business/bin/python", "ca_file": str(self.root / "trust.pem"),
            "token_file": str(self.key), "tenant": "test", "namespace": "test",
            "ingress_url": "https://adx.example:8443", "node_ingress_url": "https://node.example:8443",
            "workspace_root": str(self.root / "users"), "shared_workspace_sqlite": False,
            "gateway": {"home": str(self.root / "home")},
            "runtime": {"user": pwd.getpwuid(os.getuid()).pw_name,
                        "agent_image": "agent:v1", "tool_image": "tool:v1"},
        }
        (self.root / "users").mkdir()


    def test_swarm_offline_install_preserves_service_entrypoints(self):
        def wheel(directory, distribution, version, module, requirements=(), commands=()):
            normalized = distribution.replace("-", "_")
            info = f"{normalized}-{version}.dist-info"
            files = {
                f"{module}/__init__.py": b'def main(): print("installed-entrypoint")\n',
                f"{info}/METADATA": (f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n"
                                     + "".join(f"Requires-Dist: {r}\n" for r in requirements)).encode(),
                f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            }
            if commands:
                files[f"{info}/entry_points.txt"] = ("[console_scripts]\n" + "".join(
                    f"{command} = {module}:main\n" for command in commands)).encode()
            record = io.StringIO()
            writer = csv.writer(record)
            for name, data in files.items():
                digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                writer.writerow([name, "sha256=" + digest, len(data)])
            writer.writerow([f"{info}/RECORD", "", ""])
            files[f"{info}/RECORD"] = record.getvalue().encode()
            with zipfile.ZipFile(directory / f"{normalized}-{version}-py3-none-any.whl", "w") as archive:
                for name, data in files.items():
                    archive.writestr(name, data)

        commands = ("jiuwenswarm-init", "jiuwenswarm-agentserver", "jiuwenswarm-gateway", "jiuwenswarm-web")
        for name in ("jiuwenswarm", "workswarm"):
            with self.subTest(distribution=name):
                package = self.root / name
                package.mkdir()
                environment = self.root / (name + "-venv")
                subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, capture_output=True)
                python = str(environment / "bin/python")
                wheel(package, "openjiuwen", "0.1.20", "openjiuwen")
                wheel(package, name, "0.2.8", "jiuwenswarm", commands=commands,
                      requirements=("openjiuwen @ git+https://gitcode.com/robbluo/agent-core.git@" + "a" * 40,))
                self.install_from_module(package, python)
                check = subprocess.run([python, "-c", "import jiuwenswarm,openjiuwen; "
                                        "from importlib.metadata import version; print(version(" + repr(name) + "))"],
                                       check=True, capture_output=True, text=True)
                self.assertEqual(check.stdout.strip(), "0.2.8")
                for command in commands:
                    result = subprocess.run([str(environment / "bin" / command)], check=True,
                                            capture_output=True, text=True)
                    self.assertEqual(result.stdout.strip(), "installed-entrypoint")


    def install_from_module(self, package, python):
        runner = self.root / "install-python"
        runner.write_text('#!/bin/sh\nif [ "$2" = components ]; then exit 0; fi\nexec ' + shlex.quote(str(python)) + ' "$@"\n')
        runner.chmod(0o755)
        script = INSTALL_ENTRY + '\nerror() { echo "$*" >&2; exit 1; }; process_install'
        subprocess.run(["bash", "-c", script], check=True,
                       env=dict(os.environ, MODULE=str(ROOT / "deploy/yuanrong/deploy.sh"), DEPLOY=str(ROOT / "deploy/yuanrong"),
                                SWARM_PACKAGE_DIR=str(package),
                                SWARM_PYTHON=str(runner)))


    def test_missing_core_fails_before_install(self):
        (self.root / "workswarm-0.2-py3-none-any.whl").touch()
        with self.assertRaises(subprocess.CalledProcessError):
            self.install_from_module(self.root, "/must-not-run/python")


    def test_failed_pip_is_not_masked_by_a_successful_component_check(self):
        for name in ("workswarm-0.2-py3-none-any.whl", "openjiuwen-0.1-py3-none-any.whl"):
            (self.root / name).touch()
        fake = self.root / "python"
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n[ "$1" != -m ]\n')
        fake.chmod(0o755)
        calls = self.root / "calls"
        script = INSTALL_ENTRY + '\nerror() { exit 1; }; if process_install; then exit 99; fi'
        result = subprocess.run(["bash", "-c", script], env=dict(os.environ,
            MODULE=str(ROOT / "deploy/yuanrong/deploy.sh"), DEPLOY=str(ROOT / "deploy/yuanrong"), SWARM_PACKAGE_DIR=str(self.root), SWARM_PYTHON=str(fake), CALLS=str(calls)))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls.read_text().splitlines()), 1)
