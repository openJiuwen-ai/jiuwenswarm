# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the reused Shell lifecycle without changing host services."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy/yuanrong"


@unittest.skipUnless(sys.platform.startswith("linux"), "Release shell requires Linux Bash 4+")
class ShellLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trace = self.root / "trace"

    def run_script(self, body):
        entry = (DEPLOY / "deploy.sh").read_text().rsplit('main "$@"', 1)[0]
        entry = entry.replace('cd "$(dirname "${BASH_SOURCE[0]}")"', 'cd ' + shlex.quote(str(DEPLOY)))
        return subprocess.run(["bash", "-c", entry + "\n" + body], text=True, capture_output=True,
                              env=dict(os.environ, TRACE=str(self.trace), OUTPUT=str(self.root)))

    def test_up_and_reverse_down_use_handlers(self):
        result = self.run_script('''
gateway_load_config() { echo config >> "$TRACE"; }
swarm_service() { :; }
gateway_is_master_node() { return 0; }
gateway_holds_vip() { return 0; }
check_gateway_up_dependency() { :; }
check_web_up_dependency() { :; }
gateway_deploy_process() { echo gateway-up >> "$TRACE"; }
web_deploy_process() { echo web-up >> "$TRACE"; }
gateway_undeploy_process() { echo gateway-down >> "$TRACE"; }
web_undeploy_process() { echo web-down >> "$TRACE"; }
parse_args up
process_up
process_down
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text().splitlines(), ["config", "gateway-up", "web-up", "web-down", "gateway-down"])

    def test_repeated_up_keeps_running_gateway_and_web(self):
        result = self.run_script('''
gateway_is_master_node() { return 0; }
gateway_holds_vip() { return 0; }
get_local_ip() { echo 127.0.0.1; }
gateway_has_systemd() { return 0; }
web_has_systemd() { return 0; }
exec_on_host() { echo "$*" >> "$TRACE"; case "$*" in *is-active*) return 0;; *) exit 82;; esac; }
gateway_deploy_process
web_deploy_process
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.trace.read_text().splitlines()), 2)
        self.assertNotIn("restart", self.trace.read_text())

    def test_absent_units_are_skipped_and_stop_failures_propagate(self):
        absent = self.run_script('''
exec_on_host() { echo "$*" >> "$TRACE"; case "$*" in *LoadState*) echo not-found;; *) exit 83;; esac; }
gateway_stop_systemd 127.0.0.1
web_stop_systemd 127.0.0.1
''')
        self.assertEqual(absent.returncode, 0, absent.stderr)
        self.assertNotIn("systemctl stop", self.trace.read_text())
        for command in ("gateway_stop_systemd", "web_stop_systemd"):
            failure = self.run_script('''
exec_on_host() { case "$*" in *LoadState*) echo loaded;; *"systemctl stop"*) return 7;; *) return 0;; esac; }
''' + command + ' 127.0.0.1')
            self.assertEqual(failure.returncode, 7, failure.stderr)

    def test_systemd_handlers_use_private_environment_and_expected_ports(self):
        result = self.run_script('''
DEPLOY_VARS[APP_ENV_FILE]="$OUTPUT/service.env"
SWARM_SERVICE_PREFLIGHT="/bin/true"
copy_to_host() { cp "$2" "$OUTPUT/$(basename "$3")"; }
exec_on_host() {
  echo "$*" >> "$TRACE"
  case "$*" in
    *"command -v jiuwenswarm-gateway"*) echo /business/bin/jiuwenswarm-gateway;;
    *"command -v jiuwenswarm-web"*) echo /business/bin/jiuwenswarm-web;;
    *dirname*) echo /business/bin;;
    *'echo $PATH'*) echo /usr/bin:/bin;;
  esac
  return 0
}
port_is_listening() { echo "port=$2" >> "$TRACE"; }
sleep() { :; }
gateway_start_systemd 127.0.0.1 1
web_start_systemd 127.0.0.1
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for service in ("jiuwenswarm-gateway", "jiuwenswarm-web"):
            unit = (self.root / (service + ".service")).read_text()
            self.assertNotIn("EnvironmentFile=", unit)
            if service == "jiuwenswarm-gateway":
                self.assertIn("--require-adx-router", unit)
            self.assertIn('--dotenv "' + str(self.root / "service.env") + '"', unit)
            self.assertIn("ExecStartPre=", unit)
        trace = self.trace.read_text()
        for port in (19001, 19000, 5173):
            self.assertIn("port=" + str(port), trace)
        self.assertNotIn("8888", trace)


    def test_cleanup_actions_do_not_require_startup_bindings(self):
        settings = self.root / "settings.yaml"
        settings.write_text("business_env_file: ${ADX_BUSINESS_ENV_FILE}\ngateway: {}\n")
        runner = self.root / "python-wrapper"
        runner.write_text('#!/bin/sh\nif [ "$1" = -m ] && [ "$2" = pip ]; then echo pip-uninstall >> "$TRACE"; exit 0; fi\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        runner.chmod(0o755)
        result = self.run_script('''
BUSINESS_PYTHON="$OUTPUT/python-wrapper"
SWARM_SETTINGS="$OUTPUT/settings.yaml"
SWARM_BINDINGS="$OUTPUT/missing.env"
export SWARM_HOME="$OUTPUT" SWARM_INSTANCE_NAME=cleanup-fixture
get_local_ip() { echo 127.0.0.1; }
gateway_has_systemd() { return 0; }
web_has_systemd() { return 0; }
exec_on_host() {
    case "$*" in
        *LoadState*) echo loaded;;
        *"systemctl stop"*) echo "$*" >> "$TRACE";;
        *) return 90;;
    esac
}
pgrep() { return 1; }
swarm_disable_systemd() { echo "disable=$(gateway_service_name)" >> "$TRACE"; }
parse_args down
process_down
process_status
process_uninstall
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        trace = self.trace.read_text()
        self.assertIn("systemctl stop jiuwenswarm-web-cleanup-fixture", trace)
        self.assertIn("systemctl stop jiuwenswarm-gateway-cleanup-fixture", trace)
        self.assertIn("disable=jiuwenswarm-gateway-cleanup-fixture", trace)
        self.assertIn("pip-uninstall", trace)


    def test_gateway_listener_and_web_proxy_targets_for_both_launchers(self):
        for host, target in (("0.0.0.0", "127.0.0.1"), ("::", "[::1]"),
                             ("127.0.0.1", "127.0.0.1"), ("192.0.2.10", "192.0.2.10")):
            with self.subTest(host=host):
                self.trace.write_text("")
                result = self.run_script('''
DEPLOY_VARS[APP_ENV_FILE]="$OUTPUT/service.env"
DEPLOY_VARS[WEB_HOST]=''' + shlex.quote(host) + '''
DEPLOY_VARS[WEB_PORT]=29000
copy_to_host() { cp "$2" "$OUTPUT/$(basename "$(dirname "$3")")--$(basename "$3")"; }
exec_on_host() {
    echo "$*" >> "$TRACE"
    case "$*" in
        *"command -v jiuwenswarm-gateway"*) echo /business/bin/jiuwenswarm-gateway;;
        *"command -v jiuwenswarm-web"*) echo /business/bin/jiuwenswarm-web;;
        *dirname*) echo /business/bin;;
        *'echo $PATH'*) echo /usr/bin:/bin;;
    esac
    return 0
}
port_is_listening() { :; }
sleep() { :; }
gateway_start_systemd 127.0.0.1
web_start_systemd 127.0.0.1
gateway_start_nohup 127.0.0.1
web_start_nohup 127.0.0.1
''')
                self.assertEqual(result.returncode, 0, result.stderr)
                gateway = (self.root / "jiuwenswarm-gateway.service.d--env.conf").read_text()
                self.assertIn("Environment=WEB_HOST=" + host + "\n", gateway)
                self.assertIn("Environment=WEB_PORT=29000", gateway)
                web = (self.root / "system--jiuwenswarm-web.service").read_text()
                proxy = "--proxy-target http://" + target + ":29000"
                self.assertIn(proxy, web)
                trace = self.trace.read_text()
                self.assertIn("WEB_HOST=" + host + " WEB_PORT=29000", trace)
                self.assertIn(proxy, trace)


if __name__ == "__main__":
    unittest.main()
