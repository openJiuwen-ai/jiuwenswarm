# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Exercise the reused Shell lifecycle without changing host services."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy/adx"


@unittest.skipUnless(sys.platform.startswith("linux"), "Release shell requires Linux Bash 4+")
class ShellLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trace = self.root / "trace"

    def run_script(self, body, *, managed=True):
        entry = (DEPLOY / "deploy.sh").read_text().rsplit('main "$@"', 1)[0]
        entry = entry.replace('cd "$(dirname "${BASH_SOURCE[0]}")"', 'cd ' + shlex.quote(str(DEPLOY)))
        return subprocess.run(["bash", "-c", entry + "\n" + body], cwd=DEPLOY, text=True, capture_output=True,
                              env=dict(os.environ, TRACE=str(self.trace), OUTPUT=str(self.root),
                                       SWARM_SETTINGS=str(self.root / "settings.yaml") if managed else ""))

    def test_legacy_default_keeps_hosts_and_all_modules(self):
        result = self.run_script('''
gateway_load_config() { exit 91; }
check_jiuwenswarm_up_dependency() { :; }
check_gateway_up_dependency() { :; }
check_web_up_dependency() { :; }
deploy_jiuwenswarm() { echo "install=${DEPLOY_VARS[CLUSTER_HOSTS]}" >> "$TRACE"; }
deploy_gateway() { echo gateway-up >> "$TRACE"; }
deploy_web() { echo web-up >> "$TRACE"; }
parse_args up --hosts 192.0.2.10,192.0.2.11
process_up
''', managed=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text().splitlines(), [
            "install=192.0.2.10,192.0.2.11", "gateway-up", "web-up",
        ])

    def test_gateway_template_configures_the_adx_http_backend(self):
        result = self.run_script('''
DEPLOY_VARS[ADX_INGRESS_URL]=https://adx.example:8443
DEPLOY_VARS[ADX_TENANT]=agentos
DEPLOY_VARS[ADX_TOKEN]=test-token
DEPLOY_VARS[MASTER_NODE_IP]=192.0.2.10
DEPLOY_VARS[INGRESS_VIP]=192.0.2.10
DEPLOY_VARS[REGISTRY_PORT]=19107
DEPLOY_VARS[ETCD_ENDPOINTS]=http://192.0.2.10:32379
DEPLOY_VARS[EXTENSION_DIRS]=/opt/gateway/agent_gateway/extensions
DEPLOY_VARS[JIUWENSWARM_GATEWAY_CONFIG_DIR]=custom
GATEWAY_CONFIG_FILE="$OUTPUT/rendered.yaml"
SHARED_ENV_FILE="$OUTPUT/shared.env"
_ingress_vip() { echo 192.0.2.10; }
check_gateway_up_dependency
gateway_gen_config 127.0.0.1
''', managed=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = yaml.safe_load((self.root / "rendered.yaml").read_text())
        self.assertEqual(config["gateway"]["agent_client"], {
            "type": "agentos_router", "agent_namespace": "default"})
        self.assertEqual(config["gateway"]["adx"]["ingress_url"], "https://adx.example:8443")
        self.assertEqual(config["gateway"]["adx"]["tenant"], "agentos")
        self.assertEqual(config["gateway"]["adx"]["token_env"], "ADX_TOKEN")
        self.assertNotIn("JIUWENSWARM_GATEWAY_CONFIG_DIR=", (self.root / "shared.env").read_text())

    def test_gateway_template_reports_remote_failures_separately(self):
        for status, message in ((1, "Gateway template not found"), (255, "Failed to check Gateway template")):
            with self.subTest(status=status):
                result = self.run_script(f'''
DEPLOY_VARS[GATEWAY_CONFIG_TEMPLATE_FILE]="$OUTPUT/remote-template.yaml"
exec_on_host() {{ return {status}; }}
gateway_get_template_file 192.0.2.11
''', managed=False)
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertIn(message, result.stderr)

    def test_fresh_gateway_deploy_writes_config_gateway_yaml(self):
        result = self.run_script('''
gateway_is_master_node() { :; }
get_local_ip() { echo 127.0.0.1; }
gateway_has_systemd() { return 1; }
gateway_holds_vip() { return 1; }
gateway_gen_config() { :; }
gateway_start_nohup() { :; }
exec_on_host() {
    shift
    case "$*" in
        "test -f"*|"pgrep "*) return 1 ;;
    esac
}
copy_to_host() { echo "$3" >> "$TRACE"; }
gateway_deploy_process
''', managed=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.trace.read_text().splitlines(), [
            "/root/.jiuwenswarm/config/config-gateway.yaml",
            "/root/.jiuwenswarm/config/.env",
        ])

    def test_gateway_ingress_requires_an_https_origin(self):
        for ingress in ("adx.example:8443", "http://adx.example:8443", "https://adx.example:8443/api"):
            with self.subTest(ingress=ingress):
                result = self.run_script(
                    f"DEPLOY_VARS[ADX_INGRESS_URL]={shlex.quote(ingress)}\ncheck_gateway_up_dependency",
                    managed=False,
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("ADX_INGRESS_URL must be an HTTPS origin", result.stdout)

    def test_managed_deploy_reports_missing_enterprise_handler(self):
        result = self.run_script(":", managed=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SWARM_SETTINGS requires the Enterprise deployment package", result.stdout)

@unittest.skipUnless(sys.platform.startswith("linux"), "Release shell requires Linux Bash 4+")
class InstallerInterpreterCompatibilityTests(unittest.TestCase):
    def test_existing_interpreter_is_kept_and_explicit_new_setting_wins(self):
        for settings, expected in (("YR_PYTHON_VERSION=3.12\n", "3.12"),
                                   ("YR_PYTHON_VERSION=3.12\nPYTHON_VERSION=3.11\n", "3.11"),
                                   ("MODEL_NAME=keep\n", "3.11")):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                environment = Path(directory) / '.env.custom'
                environment.write_text(settings)
                entry = (DEPLOY / 'deploy.sh').read_text().rsplit('main "$@"', 1)[0]
                script = entry + '\nCUSTOM_ENV_FILE=' + shlex.quote(str(environment)) + '''
check_dependency() { :; }
process_up() { printf 'selected=%s\\n' "${DEPLOY_VARS[PYTHON_VERSION]}"; }
main up gateway --hosts 127.0.0.1
'''
                result = subprocess.run(['bash', '-c', script], cwd=DEPLOY, capture_output=True, text=True,
                                        env=dict(os.environ, SWARM_SETTINGS=''))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('selected=' + expected, result.stdout)
