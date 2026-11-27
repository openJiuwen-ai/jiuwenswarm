from jiuwenswarm.common.model_config_validation import is_placeholder_api_base


def test_is_placeholder_api_base_detects_documentation_domains():
    assert is_placeholder_api_base("https://example.com/compatible-mode/v1")
    assert is_placeholder_api_base("https://api.example.com/v1")
    assert is_placeholder_api_base("https://example.org")
    assert is_placeholder_api_base("https://docs.example.net/v1")


def test_is_placeholder_api_base_allows_real_domains_and_empty_values():
    assert not is_placeholder_api_base("https://real.provider.test/v1")
    assert not is_placeholder_api_base("https://example.test/v1")
    assert not is_placeholder_api_base("")


def test_is_placeholder_api_base_normalizes_hostname_and_requires_domain_boundary():
    assert is_placeholder_api_base("  https://API.Example.COM/v1  ")
    assert not is_placeholder_api_base("https://notexample.com/v1")
    assert not is_placeholder_api_base("https://example.com.invalid/v1")
