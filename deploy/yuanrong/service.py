#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Read prepared Gateway/Web service settings without changing workspaces."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex


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


def shell_identity(settings):
    options = settings["gateway"]
    for key, value in {"HOME": str(effective_home(settings)), "JIUWENSWARM_INSTANCE_NAME": options.get("instance_name", ""),
                       "WEB_STATIC_PORT": options.get("static_port", 5173)}.items():
        print("DEPLOY_VARS[" + shlex.quote(key) + "]=" + shlex.quote(str(value)))


def shell_gateway_variables(settings):
    options = settings["gateway"]
    values = {"HOME": str(effective_home(settings)), "RUNTIME_PROFILE": settings.get("profile", "standalone"), "JIUWENSWARM_INSTANCE_NAME": options.get("instance_name", ""), "GATEWAY_HOST": options["host"], "GATEWAY_PORT": options["port"],
              "WEB_HOST": options["web_host"], "WEB_PORT": options["web_port"],
              "WEB_STATIC_HOST": options["host"], "WEB_STATIC_PORT": options["static_port"],
              "APP_ENV_FILE": str(data_directory(settings) / "config/.env")}
    for key, value in values.items():
        print("DEPLOY_VARS[" + shlex.quote(key) + "]=" + shlex.quote(str(value)))


def load_settings(path, bindings=None):
    source = read_yaml(path)
    gateway = source.get("gateway") or {}
    settings = {"profile": source.get("profile", "standalone"), "gateway": {
        key: gateway[key] for key in ("host", "port", "web_host", "web_port") if key in gateway
    }}
    variables = dict(os.environ)
    if bindings:
        variables.update(read_environment(bindings))
    settings = expand_defaults(settings, variables)
    settings["gateway"].update(load_identity(path, bindings)["gateway"])
    options = settings["gateway"]
    options.setdefault("host", "127.0.0.1")
    options.setdefault("port", 19001)
    options.setdefault("web_host", "127.0.0.1")
    options.setdefault("web_port", 19000)
    if settings["profile"] not in ("standalone", "agentos"):
        raise ValueError("profile must be standalone or agentos")
    if settings["profile"] == "standalone":
        if options["host"] not in ("127.0.0.1", "::1", "localhost") or options["web_host"] not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("standalone deployment requires loopback listeners")
    return settings


def main():
    parser = argparse.ArgumentParser(description="Read Gateway/Web service settings")
    parser.add_argument("action", choices=("variables", "identity"))
    parser.add_argument("--settings", type=Path, default=Path(__file__).with_name("settings.yaml"))
    parser.add_argument("--bindings", type=Path)
    args = parser.parse_args()
    if args.action == "identity":
        shell_identity(load_identity(args.settings, args.bindings))
    else:
        shell_gateway_variables(load_settings(args.settings, args.bindings))


if __name__ == "__main__":
    main()
