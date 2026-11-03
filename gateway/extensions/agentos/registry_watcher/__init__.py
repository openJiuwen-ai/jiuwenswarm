# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Watch the registry's 3rd-agent images in etcd for Gateway northbound ports.

The management plane registers a 3rd-agent image; the registry mirrors each
image under one etcd key ``{namespace}/images/image_<sid>`` (no leading slash,
one key per ``name``+``version``). The Gateway northbound port to listen on is
the ``data.access_mode`` row ``name=web_svc_port``. This package watches that
prefix and reports the desired ``agent_type -> port`` map so the Gateway
binds/listens without allocating a port itself.

Do not conflate the two rows: ``web`` is the *container* web port used by the
southbound ``?port=`` query; ``web_svc_port`` is the Gateway northbound port.

The etcd transport lives in ``gateway.common.etcd``; the Gateway injects the
apply callback and owns this service's process lifecycle.

Trust boundary: this prefix is registry input and decides which ports the
Gateway binds. Its etcd endpoint must be restricted to trusted networks and
protected by deployment-side etcd ACLs; plain HTTP is only appropriate on an
isolated internal network.
"""

from __future__ import annotations

import os

# Namespace under which the registry mirrors images. Overridable via env.
DEFAULT_NAMESPACE = "a2x-registry"
NAMESPACE_ENV = "A2X_REGISTRY_ETCD_NAMESPACE"

# ``access_mode`` row name whose ``port`` is the Gateway northbound listen port.
WEB_SVC_PORT_ROW = "web_svc_port"

# Default northbound port range (inclusive). ``19100`` is A2A_SERVER_PORT and is
# intentionally excluded; reserved fixed ports to avoid: 19000, 19001, 5173,
# 2222, 8888, 18092.
DEFAULT_PORT_START = 19101
DEFAULT_PORT_END = 19200


def registry_namespace() -> str:
    """Namespace for the registry's etcd keys: env override, else default."""
    value = str(os.environ.get(NAMESPACE_ENV) or "").strip().strip("/")
    return value or DEFAULT_NAMESPACE


def registry_prefix(namespace: str | None = None) -> str:
    """The etcd key prefix holding all image records (no leading slash)."""
    ns = str(namespace or "").strip().strip("/") or registry_namespace()
    return f"{ns}/images/"


def in_default_port_range(port: int) -> bool:
    """True when *port* falls inside the default northbound range (inclusive)."""
    try:
        value = int(port)
    except (TypeError, ValueError):
        return False
    return DEFAULT_PORT_START <= value <= DEFAULT_PORT_END


__all__ = [
    "DEFAULT_NAMESPACE",
    "NAMESPACE_ENV",
    "WEB_SVC_PORT_ROW",
    "DEFAULT_PORT_START",
    "DEFAULT_PORT_END",
    "registry_namespace",
    "registry_prefix",
    "in_default_port_range",
]
