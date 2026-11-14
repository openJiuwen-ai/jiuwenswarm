"""Network presets preserve control paths without opening internal services."""
import ipaddress

import pytest

from jiuwenswarm.extensions.adx.network import agent_policy, managed_tool_policy, tool_policy


def decision(policy, target, port=443):
    """Evaluate IPv4 rules using the runtime's higher-priority-first contract."""
    address = ipaddress.IPv4Address(target)
    traffic = policy['traffic']
    for rule in sorted(traffic['rules'], key=lambda item: item['priority'], reverse=True):
        if rule.get('direction') not in ('egress', 'both') or rule.get('domain'):
            continue
        if rule.get('cidr') and address not in ipaddress.IPv4Network(rule['cidr']):
            continue
        selected = rule.get('port_range')
        if selected and not selected['first'] <= port <= selected['last']:
            continue
        return rule['action']
    return traffic['egress_default_action']


def test_default_n2_allows_public_but_denies_internal_and_metadata():
    policy = tool_policy([])
    assert decision(policy, '1.1.1.1') == 'allow'
    for target in ('127.0.0.1', '10.88.4.20', '192.168.77.10', '169.254.169.254', '100.64.1.1'):
        assert decision(policy, target) == 'deny'
    assert policy['traffic']['ingress_default_action'] == 'deny'
    assert policy['dns']['default_action'] == 'allow'


@pytest.mark.parametrize('profile,public,granted', [('N0', 'deny', 'deny'), ('N1', 'deny', 'allow'), ('N2', 'allow', 'allow')])
def test_tool_profile_and_exact_legacy_origin(profile, public, granted):
    policy = tool_policy(['http://192.168.77.10:18144'], profile=profile)
    assert decision(policy, '1.1.1.1') == public
    assert decision(policy, '192.168.77.10', 18144) == granted
    assert decision(policy, '192.168.77.10', 6379) == 'deny'


def test_publicly_addressed_cluster_node_is_still_denied():
    policy = tool_policy([], blocked_cidrs=['8.8.8.8/32'])
    assert decision(policy, '8.8.8.8') == 'deny'
    assert decision(policy, '1.1.1.1') == 'allow'


def test_n0_agent_keeps_node_ingress_but_never_grants_the_model():
    policy = agent_policy({'ingress_url': 'http://10.88.4.1:8080'},
                          ['http://192.168.77.10:18144'], ['http://192.168.77.10:18144'], profile='N0')
    assert decision(policy, '10.88.4.1', 8080) == 'allow'
    assert decision(policy, '192.168.77.10', 18144) == 'deny'


@pytest.mark.parametrize('profile', ['N1', 'N2'])
def test_agent_internal_model_requires_existing_explicit_authorization(profile):
    with pytest.raises(ValueError, match='explicit allowed origin'):
        agent_policy({'ingress_url': 'http://10.88.4.1:8080'}, ['http://192.168.77.10:18144'], [], profile=profile)
    policy = agent_policy({'ingress_url': 'http://10.88.4.1:8080'},
                          ['http://192.168.77.10:18144'], ['http://192.168.77.10:18144'], profile=profile)
    assert decision(policy, '192.168.77.10', 18144) == 'allow'
    assert decision(policy, '192.168.77.10', 6379) == 'deny'


@pytest.mark.parametrize('profile', ['N3', '', None, True, 2])
def test_unknown_profile_is_not_silently_downgraded(profile):
    with pytest.raises(ValueError, match='profile'):
        tool_policy([], profile=profile)


def test_native_n2_roundtrips_through_managed_tool_overlay():
    policy = tool_policy([])
    assert managed_tool_policy(policy) == policy


def test_legacy_explicit_deny_is_not_upgraded_to_n2():
    assert managed_tool_policy({'block_network': True})['traffic']['egress_default_action'] == 'deny'
