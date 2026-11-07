//go:build test

package proxy

import (
	"net/http"

	"credential_router/internal/platform"
)

// Test-support accessors for the external white-box test suite in
// tests/unit/proxy. The //go:build test constraint excludes this file from
// production builds (`go build ./...`) while leaving the symbols available
// during `go test ./tests/unit/...` — there is no production binary impact.

// ClientMessageForTesting returns the client-facing message for an error code.
func ClientMessageForTesting(code platform.Code) string { return clientMessage(code) }

// ClientIPForTesting extracts the source IP from an incoming request.
func ClientIPForTesting(r *http.Request) string { return clientIP(r) }

// IsTimeoutForTesting reports whether err is a network timeout.
func IsTimeoutForTesting(err error) bool { return isTimeout(err) }

// StripHopByHopHeadersForTesting removes hop-by-hop headers from h in place.
func StripHopByHopHeadersForTesting(h http.Header) { stripHopByHopHeaders(h) }

// CloneHeaderForTesting deep-copies an http.Header.
func CloneHeaderForTesting(h http.Header) http.Header { return cloneHeader(h) }

// CopyHeaderForTesting copies all values of src into dst.
func CopyHeaderForTesting(dst, src http.Header) { copyHeader(dst, src) }

// RedactProxyKeyForTesting returns the redacted form of proxyKey used in log lines.
func RedactProxyKeyForTesting(proxyKey string) string { return redactProxyKey(proxyKey) }
