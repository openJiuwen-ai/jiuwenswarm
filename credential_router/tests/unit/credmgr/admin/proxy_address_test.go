//go:build cgo

package admin_test

import (
	"testing"

	"credential_router/internal/credmgr/admin"
	"credential_router/internal/platform"
	"credential_router/internal/proxy"
)

// proxyAddress is a pure function of ServerConfig (it does not touch the
// DB, cache, or manager), so it can be unit-tested in isolation by
// constructing a Server with only the serverCfg field set. The struct has
// many other fields the function does not depend on; zero values are fine.

func TestProxyAddressLoopbackBind(t *testing.T) {
	s := admin.NewTestServerForTesting(platform.ServerConfig{BindAddress: "127.0.0.1:8080"})
	got := s.ProxyAddressForTesting("https://api.example.com/v1")
	want := "http://127.0.0.1:8080/proxy/" + proxy.EncodeBase64URL("https://api.example.com/v1")
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestProxyAddressSpecificBind(t *testing.T) {
	s := admin.NewTestServerForTesting(platform.ServerConfig{BindAddress: "10.0.0.5:9090"})
	got := s.ProxyAddressForTesting("https://api.example.com/v1")
	want := "http://10.0.0.5:9090/proxy/" + proxy.EncodeBase64URL("https://api.example.com/v1")
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestProxyAddressWildcardBindUsesExternalAddress(t *testing.T) {
	s := admin.NewTestServerForTesting(platform.ServerConfig{
		BindAddress:     "0.0.0.0:8080",
		ExternalAddress: "http://router.example.com:8080",
	})
	got := s.ProxyAddressForTesting("https://api.example.com/v1")
	want := "http://router.example.com:8080/proxy/" + proxy.EncodeBase64URL("https://api.example.com/v1")
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestProxyAddressWildcardBindExternalTrailingSlash(t *testing.T) {
	s := admin.NewTestServerForTesting(platform.ServerConfig{
		BindAddress:     "0.0.0.0:8080",
		ExternalAddress: "http://router.example.com:8080/",
	})
	got := s.ProxyAddressForTesting("https://api.example.com/v1")
	want := "http://router.example.com:8080/proxy/" + proxy.EncodeBase64URL("https://api.example.com/v1")
	if got != want {
		t.Errorf("trailing slash not trimmed: got %q, want %q", got, want)
	}
}

func TestProxyAddressWildcardBindExternalIPv6(t *testing.T) {
	s := admin.NewTestServerForTesting(platform.ServerConfig{
		BindAddress:     "[::]:8080",
		ExternalAddress: "https://router.example.com",
	})
	got := s.ProxyAddressForTesting("https://api.example.com/v1")
	want := "https://router.example.com/proxy/" + proxy.EncodeBase64URL("https://api.example.com/v1")
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestProxyAddressWildcardBindEmptyExternalReturnsEmpty(t *testing.T) {
	s := admin.NewTestServerForTesting(platform.ServerConfig{BindAddress: "0.0.0.0:8080"})
	if got := s.ProxyAddressForTesting("https://api.example.com/v1"); got != "" {
		t.Errorf("expected empty when wildcard+no external (defensive bypass), got %q", got)
	}
}
