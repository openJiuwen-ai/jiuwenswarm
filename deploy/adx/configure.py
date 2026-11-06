#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Configure a Swarm application against an existing ADX cluster."""
import argparse
import copy
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def write_yaml(path, data):
    import yaml
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(prefix=".adx-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            if owner is not None:
                os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_yaml(path):
    import yaml
    with Path(path).open() as stream:
        data = yaml.safe_load(stream)
    if not isinstance(data, dict):
        raise ValueError("configuration must be a YAML mapping")
    return data



def expand_defaults(value, variables):
    from string import Template
    if isinstance(value, dict):
        return {key: expand_defaults(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_defaults(item, variables) for item in value]
    return Template(value).substitute(variables) if isinstance(value, str) else value



def read_environment(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("invalid deployment environment name")
        decoded = json.loads(value)
        if not isinstance(decoded, str) or any(c in decoded for c in "\r\n\x00"):
            raise ValueError("invalid deployment environment value")
        values[key] = decoded
    return values



# These names belong to the retired YuanRong installer, or duplicate values
# now supplied by the ADX deployment's service launcher.
_OBSOLETE_APPLICATION_ENV = (
    "YR_PYTHON_VERSION", "YR_FUNC_CODE_DIR", "FUNC_SVC_NAME", "FUNCTION_ID", "SANDBOX_TYPE",
    "MGR_CPU", "MGR_MEMORY", "MGR_MIN_INSTANCE", "MGR_MAX_INSTANCE", "MGR_CONCURRENT_NUM",
    "MASTER_NODE_IP", "FRONTEND_HOST", "FRONTEND_PORT", "ETCD_ENDPOINTS", "WEB_ENABLED",
    "GATEWAY_CONCURRENCY", "GATEWAY_INVOKE_TIMEOUT", "GATEWAY_SESSION_MAP_SCOPE",
    "TOOL_SANDBOX_TYPE", "TOOL_SANDBOX_ENABLE", "TOOL_SANDBOX_IMAGE", "TOOL_SANDBOX_EXECUTOR",
    "TOOL_SANDBOX_USER", "TOOL_SANDBOX_GROUP", "BIND_IP",
    "GATEWAY_HOST", "GATEWAY_PORT", "WEB_HOST", "WEB_PORT", "WEB_STATIC_HOST", "WEB_STATIC_PORT",
    "ADX_GATEWAY_TOKEN",
)
_TOOL_ENV_SETTINGS = (
    ("TOOL_SANDBOX_CPU", "cpu", "cpu_limit"),
    ("TOOL_SANDBOX_MEMORY", "memory", "mem_limit"),
    ("TOOL_SANDBOX_IDLE_TIMEOUT", "idle_timeout", None),
)


def migrate_tool_environment(config, path):
    """Preserve effective legacy resource overrides in YAML before removing them."""
    from dotenv import dotenv_values

    if not Path(path).is_file():
        return config
    values = dotenv_values(path, interpolate=False)
    sandbox = config["sandbox"]
    for name, key, limit in _TOOL_ENV_SETTINGS:
        raw = values.get(name)
        if raw is None or not raw.strip():
            continue
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"{name} must be an integer before migration") from None
        if value < 0 or (key != "idle_timeout" and value == 0):
            raise ValueError(f"{name} is outside the supported ADX range")
        sandbox[key] = value
        if limit and sandbox.get(limit):
            sandbox[limit] = max(sandbox[limit], value)
    return config


def gateway_config(config, settings):
    config = copy.deepcopy(config)
    runtime = settings["runtime"]
    gateway = config.setdefault("gateway", {})
    gateway["agent_client"] = {"type": "adx_router", "agent_namespace": settings["namespace"]}
    adx_options = gateway.get("adx") or {}
    gateway["adx"] = {key: copy.deepcopy(adx_options[key]) for key in
                      ("connect_timeout_seconds", "ssh_user", "ssh_known_hosts", "restart_policy")
                      if key in adx_options}
    gateway["adx"].setdefault("connect_timeout_seconds", 180)
    gateway["adx"].update({
        "ingress_url": settings["ingress_url"], "tenant": settings["tenant"],
        "token_env": "ADX_TOKEN", "builtin_image": runtime["agent_image"],
        "runtime_class": runtime.get("agent_runtime", "supervisor"), "builtin_process_from_image": True,
        "workspace_target": "/home/agentos",
    })
    gateway.setdefault("agentos", {})["workspace_root"] = settings["workspace_root"]
    account = pwd.getpwnam(runtime["user"]) if runtime.get("user") else pwd.getpwuid(os.getuid())
    from jiuwenswarm.common.adx_config import managed_adx_sandbox
    defaults = {
        "enabled": True, "type": "adx-environment",
        "runtime": runtime.get("tool_runtime", "conch"), "image": runtime["tool_image"],
        "cpu": 2000, "memory": 4096, "cpu_limit": 2000, "mem_limit": 4096,
        "idle_timeout": 60, "user": str(account.pw_uid), "group": str(account.pw_gid),
        "connection": {"verify_tls": True},
    }
    if settings.get("profile", "standalone") != "agentos":
        defaults["url"] = "${JIUWENSWARM_ADX_NODE_INGRESS_URL}"
    config["sandbox"] = managed_adx_sandbox(config.get("sandbox"), defaults)
    ssh = gateway.get("agentos", {}).get("ssh")
    if isinstance(ssh, dict):
        ssh.pop("user_template", None)
    for section, key in (("checkpoint", "sqlite_journal_mode"), ("workspace_sqlite", "journal_mode")):
        if isinstance(config.get(section), dict):
            config[section].pop(key, None)
            if not config[section]:
                config.pop(section)
    if settings.get("shared_workspace_sqlite", False):
        for team in config.get("modes", {}).get("team", {}).values():
            if isinstance(team, dict):
                storage = team.setdefault("storage", {"type": "sqlite"})
                if storage.get("type", "sqlite") == "sqlite":
                    storage.setdefault("params", {}).update(db_enable_wal=False, mmap_size_mb=0)
    return config



def check_components():
    from adx_environment import Environment  # noqa: F401
    from openjiuwen.extensions.sys_operation.sandbox.providers.adx_environment import _AdxEnvironmentBackend  # noqa: F401
    from jiuwenswarm.extensions.adx.config import AdxRouterConfig
    if "builtin_process_from_image" not in AdxRouterConfig.__dataclass_fields__:
        raise ValueError("Swarm needs the builtin_process_from_image compatibility patch")



def write_private(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = path.stat() if path.exists() else None
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".agentos-")
    try:
        with os.fdopen(fd, "w") as stream:
            if owner:
                os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def ready(settings, timeout=60):
    """Check authenticated ADX API access, not a legacy frontend TCP socket."""
    url = settings["ingress_url"].rstrip("/")
    if not url.startswith("https://"):
        raise ValueError("ADX ingress must use HTTPS")
    token = Path(settings["token_file"]).read_text().strip()
    context = ssl.create_default_context(cafile=settings["ca_file"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), NoRedirect())
    request = urllib.request.Request(url + "/api/instances?pageSize=1", headers={"Authorization": "Bearer " + token})
    deadline = time.monotonic() + timeout
    last = "unavailable"
    while True:
        service = "ADX"
        try:
            with opener.open(request, timeout=5) as response:
                payload = json.load(response)
                if response.status == 200 and isinstance(payload, dict) and isinstance(payload.get("items"), list):
                    return
                last = "invalid inventory response"
        except urllib.error.HTTPError as exc:
            last = "HTTP " + str(exc.code)
            if exc.code in (401, 403):
                raise RuntimeError(service + " readiness authentication failed: " + last) from None
        except (OSError, ValueError) as exc:
            last = type(exc).__name__
        if time.monotonic() >= deadline:
            raise RuntimeError(service + " API not ready: " + last)
        time.sleep(1)


def configure(settings):
    check_business(settings)
    workspace = Path(settings["workspace_root"])
    if not workspace.is_dir():
        raise ValueError("workspace_root is unavailable; prepare the shared workspace directory first")
    options = settings.get("gateway", {})
    home = effective_home(settings)
    data = data_directory(settings)
    config = data / "config/config.yaml"
    environment = dict(os.environ, JIUWENSWARM_HOME=str(home))
    if options.get("instance_name"):
        environment["JIUWENSWARM_DATA_DIR"] = str(data)
    fresh = not config.exists()
    if fresh:
        subprocess.run([settings["python"], "-m", "jiuwenswarm.init_workspace"],
                       env=environment, stdin=subprocess.DEVNULL, check=True)
        if options.get("base_config"):
            write_yaml(config, read_yaml(options["base_config"]))
    # Validate every historical configuration before changing existing files.
    user_updates = []
    for user in workspace.iterdir() if settings.get("update_user_configs", False) else ():
        user_config = user / ".jiuwenswarm/config/config.yaml"
        if user_config.is_file():
            try:
                updated = migrate_tool_environment(
                    gateway_config(read_yaml(user_config), settings), user_config.with_name(".env"))
            except ValueError as exc:
                raise ValueError(f"Cannot migrate {user_config}: {exc}") from None
            user_updates.append((user_config, updated))
    env_file = data / "config/.env"
    custom = Path(settings.get("business_env_file") or Path(__file__).with_name(".env.custom"))
    value = migrate_tool_environment(gateway_config(read_yaml(config), settings),
                                     env_file if env_file.exists() else custom)
    agentos = value["gateway"].setdefault("agentos", {})
    if settings.get("profile", "standalone") == "standalone":
        agentos.update(auth_enabled=False, auth_service_url="", registry={"endpoint": ""})
    for key, setting in options.get("agentos", {}).items():
        agentos[key] = setting
    if fresh:
        value["gateway"]["session_map_scope"] = "per_chat_bot_user"
    write_yaml(config, value)
    settings.setdefault("gateway", {})["agentos"] = value["gateway"]["agentos"]
    if fresh or not env_file.exists():
        write_private(env_file, custom.read_text() if custom.exists() else "")
    update_environment(env_file, settings, service=True)
    # Existing users retain model/IAM/session configuration and all data files.
    for user_config, updated in user_updates:
        write_yaml(user_config, updated)
        update_environment(user_config.with_name(".env"), settings, user_config.stat())
    return env_file


def update_environment(path, settings, owner=None, *, service=False):
    from dotenv import dotenv_values, set_key, unset_key
    if not path.exists():
        write_private(path, "")
        if owner:
            os.chown(path, owner.st_uid, owner.st_gid)
    owner = path.stat()
    token = Path(settings["token_file"]).read_text().strip()
    values = {"ADX_TOKEN": token, "SSL_CERT_FILE": settings["ca_file"]}
    node_ingress_key = "JIUWENSWARM_ADX_NODE_INGRESS_URL"
    if (settings.get("profile", "standalone") != "agentos"
            and (service or settings.get("persist_user_node_ingress_url", True))):
        values[node_ingress_key] = settings["node_ingress_url"]
    elif node_ingress_key in dotenv_values(path, interpolate=False):
        unset_key(str(path), node_ingress_key)
    if service:
        values.update({
            "AGENTOS_BUILTIN_AGENT_CPU": str(settings["runtime"].get("cpu", 2000)),
            "AGENTOS_BUILTIN_AGENT_MEMORY": str(settings["runtime"].get("memory", 4096)),
        })
    for key, setting in values.items():
        set_key(str(path), key, str(setting))
    existing = dotenv_values(path, interpolate=False)
    for key in (*_OBSOLETE_APPLICATION_ENV, *(item[0] for item in _TOOL_ENV_SETTINGS)):
        if key in existing:
            unset_key(str(path), key)
    os.chown(path, owner.st_uid, owner.st_gid)
    path.chmod(0o600)


def shell_gateway_variables(settings):
    options = settings["gateway"]
    values = {"HOME": str(effective_home(settings)), "RUNTIME_PROFILE": settings.get("profile", "standalone"), "JIUWENSWARM_INSTANCE_NAME": options.get("instance_name", ""), "GATEWAY_HOST": options["host"], "GATEWAY_PORT": options["port"],
              "WEB_HOST": options["web_host"], "WEB_PORT": options["web_port"],
              "WEB_STATIC_HOST": options["host"], "WEB_STATIC_PORT": options["static_port"],
              "APP_ENV_FILE": str(data_directory(settings) / "config/.env")}
    for key, value in values.items():
        print("DEPLOY_VARS[" + shlex.quote(key) + "]=" + shlex.quote(str(value)))



def effective_home(settings):
    options = settings["gateway"]
    home = Path(options["home"]).resolve()
    name = options.get("instance_name")
    return home / ".jiuwenswarm-instances" / name if name else home


def data_directory(settings):
    return effective_home(settings) / ".jiuwenswarm"


def load_identity(path, bindings=None):
    """Resolve service identity without loading startup policy or credentials."""
    from string import Template

    if Path(path).is_file():
        gateway = read_yaml(path).get("gateway") or {}
    elif "SWARM_INSTANCE_NAME" in os.environ:
        gateway = {}
    else:
        raise ValueError("Cannot determine service identity: settings file is missing")
    identity = {
        "home": os.environ.get("SWARM_HOME", gateway.get("home", str(Path.home()))),
        "instance_name": os.environ.get("SWARM_INSTANCE_NAME", gateway.get("instance_name", "")),
        "static_port": gateway.get("static_port", 5173),
    }
    variables = dict(os.environ)
    variables.setdefault("HOME", str(Path.home()))
    required = set()
    for value in identity.values():
        if isinstance(value, str):
            required.update(Template(value).get_identifiers())
    if required - variables.keys() and bindings and Path(bindings).is_file():
        variables.update(read_environment(bindings))
    try:
        identity = expand_defaults(identity, variables)
    except KeyError as exc:
        raise ValueError("Cannot resolve service identity variable: " + str(exc)) from None
    name = identity["instance_name"]
    if not isinstance(name, str) or (name and not re.fullmatch(r"[a-zA-Z0-9_-]+", name)):
        raise ValueError("invalid instance_name")
    home = identity["home"]
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise ValueError("service identity home must be an absolute path")
    port = int(identity["static_port"])
    if not 0 < port < 65536:
        raise ValueError("invalid static_port")
    identity["static_port"] = port
    return {"gateway": identity}


def load_settings(path, bindings=None):
    variables = dict(os.environ)
    if bindings:
        variables.update(read_environment(bindings))
    settings = expand_defaults(read_yaml(path), variables)
    options = settings.setdefault("gateway", {})
    options.setdefault("home", str(Path.home()))
    options.setdefault("host", "127.0.0.1")
    options.setdefault("port", 19001)
    options.setdefault("web_host", "127.0.0.1")
    options.setdefault("web_port", 19000)
    options.setdefault("static_port", 5173)
    name = options.get("instance_name", "")
    if name and not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("invalid instance_name")
    if settings.get("profile", "standalone") not in ("standalone", "agentos"):
        raise ValueError("profile must be standalone or agentos")
    if settings.get("profile", "standalone") == "standalone":
        if options["host"] not in ("127.0.0.1", "::1", "localhost") or options["web_host"] not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("standalone deployment requires loopback listeners")
    return settings


def check_business(settings):
    for key in ("token_file", "ca_file"):
        if not Path(settings[key]).is_file():
            raise ValueError(key + " is missing")
    check_components()


def shell_identity(settings):
    options = settings["gateway"]
    for key, value in {"HOME": str(effective_home(settings)), "JIUWENSWARM_INSTANCE_NAME": options.get("instance_name", ""),
                       "WEB_STATIC_PORT": options.get("static_port", 5173)}.items():
        print("DEPLOY_VARS[" + shlex.quote(key) + "]=" + shlex.quote(str(value)))


def main():
    parser = argparse.ArgumentParser(description="Configure Swarm on an existing ADX cluster")
    parser.add_argument("action", choices=("configure", "variables", "identity", "ready", "components"))
    parser.add_argument("--settings", type=Path, default=Path(__file__).with_name("settings.yaml"))
    parser.add_argument("--bindings", type=Path)
    args = parser.parse_args()
    if args.action == "components":
        check_components()
        return
    if args.action == "identity":
        shell_identity(load_identity(args.settings, args.bindings))
        return
    settings = load_settings(args.settings, args.bindings)
    if args.action == "configure":
        configure(settings)
    elif args.action == "variables":
        shell_gateway_variables(settings)
    else:
        ready(settings)


if __name__ == "__main__":
    main()
