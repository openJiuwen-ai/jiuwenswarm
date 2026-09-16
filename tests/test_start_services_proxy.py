from jiuwenswarm import start_services
import urllib.request


def test_no_proxy_does_not_hide_macos_system_proxy(monkeypatch):
    monkeypatch.setattr(start_services.sys, 'platform', 'darwin')
    monkeypatch.setattr(urllib.request, 'getproxies_macosx_sysconf', lambda: {'http':'http://127.0.0.1:7897','https':'http://127.0.0.1:7897'}, raising=False)
    env={'NO_PROXY':'localhost,127.0.0.1'}
    start_services._inherit_system_proxy(env)
    assert env['HTTPS_PROXY']=='http://127.0.0.1:7897'
    assert env['NO_PROXY']=='localhost,127.0.0.1'


def test_explicit_proxy_is_preserved(monkeypatch):
    monkeypatch.setattr(start_services.sys,'platform','darwin')
    env={'https_proxy':''}
    start_services._inherit_system_proxy(env)
    assert env=={'https_proxy':''}


def test_other_platform_unchanged(monkeypatch):
    monkeypatch.setattr(start_services.sys,'platform','linux')
    env={'NO_PROXY':'localhost'}
    start_services._inherit_system_proxy(env)
    assert env=={'NO_PROXY':'localhost'}
