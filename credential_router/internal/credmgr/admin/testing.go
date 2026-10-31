//go:build test

package admin

import (
	"credential_router/internal/platform"
	"credential_router/internal/proxy/ssrf"
)

// Test-support accessors for the external white-box test suite in
// tests/unit/admin. The //go:build test constraint excludes this file from
// production builds (`go build ./...`) while leaving the symbols available
// during `go test ./tests/unit/...` — there is no production binary impact.

// SetPolicyFactoryForTesting installs the URLPolicy factory apiBasePolicy
// uses for SSRF validation. Tests swap the default (DNS-touching) policy for
// proxy.TestPolicy so credential fixtures such as api.example.com are
// accepted without DNS lookups.
func (s *Server) SetPolicyFactoryForTesting(fn func() *ssrf.URLPolicy) {
	s.policyFactory = fn
}

// NewTestServerForTesting constructs a Server with only serverCfg populated,
// which is enough to unit-test pure helpers such as proxyAddress that do not
// touch the DB, cache, or manager. All other Server fields stay at their
// zero values, matching the helper's intent (do not depend on them).
func NewTestServerForTesting(serverCfg platform.ServerConfig) *Server {
	return &Server{serverCfg: serverCfg}
}

// ProxyAddressForTesting exposes the unexported proxyAddress method so the
// external white-box test suite can verify its bind/external_address logic
// without needing a full Server with a live DB, cache, and rotator.
func (s *Server) ProxyAddressForTesting(apiBase string) string {
	return s.proxyAddress(apiBase)
}
