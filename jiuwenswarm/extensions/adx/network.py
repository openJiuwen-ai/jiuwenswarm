# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Compile managed network profiles and retained workspace network settings."""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlsplit


# Include non-RFC1918 local, metadata, shared, documentation and multicast
# destinations. Explicit cluster addresses also cover publicly addressed nodes.
_NON_PUBLIC_CIDRS = (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
    '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
    '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
    '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4',
)


def network_profile(value):
    if not isinstance(value, str) or value not in ('N0', 'N1', 'N2'):
        raise ValueError('network profile must be N0, N1 or N2')
    return value


def _empty_policy():
    return {'traffic': {'ingress_default_action': 'deny', 'egress_default_action': 'deny',
                        'rules': [], 'mode': 'stateful'}, 'dns': {'default_action': 'deny', 'rules': []}}


def _origin(value):
    url = urlsplit(value)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise ValueError('egress targets must be HTTP(S) URLs without credentials')
    return url.hostname, url.port or (443 if url.scheme == 'https' else 80)


def _grant(value, *, allow_private=False):
    host, port = _origin(value)
    rule = {'action': 'allow', 'direction': 'egress', 'protocol': 'tcp',
            'port_range': {'first': port, 'last': port}, 'priority': 100}
    try:
        ip = ipaddress.IPv4Address(host)
    except ipaddress.AddressValueError:
        if ':' in host:
            raise ValueError('AgentOS egress policy supports IPv4 and DNS names') from None
        rule['domain'] = host
        if allow_private:
            rule['priority'] = 300
    else:
        if not ip.is_global and not allow_private:
            raise ValueError('internal service origins require an explicit allowed origin')
        rule['cidr'] = f'{ip}/32'
        if allow_private:
            rule['priority'] = 300
    return rule


def tool_policy(targets, *, profile='N2', blocked_cidrs=()):
    """N0 closes business egress; N1 grants origins; N2 permits public egress."""
    profile = network_profile(profile)
    policy = _empty_policy()
    if profile == 'N0':
        return policy
    if profile == 'N2':
        policy['traffic']['egress_default_action'] = 'allow'
        policy['dns']['default_action'] = 'allow'
    if not isinstance(blocked_cidrs, (list, tuple)):
        raise ValueError('network.blocked_cidrs must be a list of IPv4 CIDRs')
    for value in dict.fromkeys((*_NON_PUBLIC_CIDRS, *blocked_cidrs)):
        cidr = str(ipaddress.IPv4Network(value))
        policy['traffic']['rules'].append({'action': 'deny', 'direction': 'egress',
            'protocol': 'any', 'cidr': cidr, 'priority': 200})
    for target in targets:
        rule = _grant(target, allow_private=True)
        policy['traffic']['rules'].append(rule)
        if rule.get('domain'):
            policy['dns']['rules'].append({'action': 'allow', 'pattern': rule['domain']})
    return policy


def agent_policy(context, models, allowed_internal_origins, *, profile='N2', blocked_cidrs=()):
    policy = tool_policy([], profile=profile, blocked_cidrs=blocked_cidrs)
    control = _grant(context['ingress_url'], allow_private=True)
    if context.get('ingress_policy_peer'):
        control.pop('cidr', None)
        control['domain'] = context['ingress_policy_peer']
    policy['traffic']['rules'].insert(0, control)
    if profile == 'N0':
        return policy
    internal = {_origin(value) for value in allowed_internal_origins}
    for value in models:
        rule = _grant(value, allow_private=_origin(value) in internal)
        policy['traffic']['rules'].append(rule)
        if rule.get('domain'):
            policy['dns']['rules'].append({'action': 'allow', 'pattern': rule['domain']})
    return policy



def public_policy(configuration):
    from adx_environment import DNSPolicy, DNSRule, NetworkPolicy, NetworkRule, PortRange, TrafficPolicy
    traffic = dict(configuration["traffic"])
    rules = []
    for original in traffic.pop("rules", []):
        rule = dict(original)
        for key in ("port_range", "environment_port_range"):
            if isinstance(rule.get(key), dict):
                rule[key] = PortRange(**rule[key])
        rules.append(NetworkRule(**rule))
    dns = configuration["dns"]
    return NetworkPolicy(traffic=TrafficPolicy(rules=rules, **traffic),
                         dns=DNSPolicy(default_action=dns["default_action"],
                                       rules=[DNSRule(**r) for r in dns["rules"]])).to_dict()


def managed_tool_policy(configuration):
    import copy
    if not isinstance(configuration, dict) or set(configuration) - {'traffic', 'dns', 'block_network', 'dns_blacklist'}:
        raise ValueError('sandbox.network requires explicit migration to an ADX traffic/DNS policy')
    if 'block_network' in configuration and not isinstance(configuration['block_network'], bool):
        raise ValueError('sandbox.network.block_network must be a boolean')
    if 'dns_blacklist' in configuration and (not isinstance(configuration['dns_blacklist'], (list, tuple))
            or not all(isinstance(p, str) for p in configuration['dns_blacklist'])):
        raise ValueError('sandbox.network.dns_blacklist must be a list of patterns')
    legacy = bool(configuration.get('block_network') or configuration.get('dns_blacklist'))
    if legacy and (configuration.get('traffic') is not None or configuration.get('dns') is not None):
        raise ValueError('sandbox.network cannot mix legacy and traffic/DNS sections')
    policy = _empty_policy()
    if legacy:
        if configuration.get('block_network') and configuration.get('dns_blacklist'):
            raise ValueError('sandbox.network cannot combine block_network and dns_blacklist')
        policy['dns']['rules'] = [{'action': 'deny', 'pattern': p} for p in configuration.get('dns_blacklist', [])]
    else:
        policy.update(copy.deepcopy({k: v for k, v in configuration.items() if k in ('traffic', 'dns') and v is not None}))
        traffic = policy['traffic']
        if (traffic.get('ingress_default_action') not in ('allow', 'deny')
                or traffic.get('egress_default_action') not in ('allow', 'deny')
                or traffic.get('mode', 'stateful') != 'stateful'):
            raise ValueError('managed runsc network requires explicit traffic defaults and stateful mode; migrate the old policy explicitly')
    public_policy(policy)  # Validate with the same public types used by Core.
    return policy


def workspace_model_origins(workspace):
    import yaml
    from dotenv import dotenv_values
    directory = Path(workspace) / ".jiuwenswarm/config"
    configuration = directory / "config.yaml"
    if not configuration.is_file():
        raise ValueError("AgentServer workspace model configuration is missing")
    environment = {k: v for k, v in dotenv_values(directory / ".env", interpolate=False).items() if v is not None}
    def resolve(value):
        return re.sub(r"\$\{([A-Za-z_][A-Za-z_0-9]*)(?::-([^}]*))?\}",
                      lambda m: environment.get(m[1]) or m[2] or "", value)
    origins = set()
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("api_base", "base_url") and isinstance(child, str):
                    url = resolve(child).strip()
                    if url:
                        _origin(url)
                        origins.add(url)
                elif isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    document = yaml.safe_load(configuration.read_text()) or {}
    for key in ("model", "models"):
        visit(document.get(key))
    models = document.get("models") or {}
    if not isinstance(models, dict):
        raise ValueError("models must be a mapping")
    # Match get_default_models' environment fallback without mutating the
    # Gateway process environment shared by concurrent users.
    if not models.get("defaults") and not isinstance(models.get("default"), dict):
        fallback = environment.get("API_BASE", "").strip()
        if fallback:
            _origin(fallback)
            origins.add(fallback)
    return sorted(origins)
