// header_transform.go owns the client-to-upstream credential header
// pipeline. Every proxied request runs stripAuthHeaders → InjectRealKey
// in sequence:
//
//  1. stripAuthHeaders removes every credential-auth header
//     (Authorization, X-Api-Key, X-Goog-Api-Key, plus all casing
//     variants) that the client may have set on the inbound request.
//     This is mandatory: a client using the OpenAI SDK with
//     `Authorization: Bearer <proxy_key>`, registered against an
//     anthropic credential, would otherwise forward its proxy_key-
//     bearing Authorization header verbatim to the upstream provider.
//
//  2. InjectRealKey overwrites the auth header for auth_type with the
//     real key retrieved from the credential store. After strip, this
//     becomes a clean delete-then-write against an empty header slot,
//     so the casing-variant removal is straightforward.
//
// The strip-before-inject order matters: stripping first lets inject
// rely on a clean header set instead of having to merge with whatever
// the client sent. Do not call these in the opposite order.
package proxy

import (
	"fmt"
	"net/http"
)

// stripAuthHeaders removes every credential-auth header (and all case
// variants) from h. The list of header families is defined in
// auth_headers.go (authHeaderFamilies).
func stripAuthHeaders(h http.Header) {
	for _, name := range authHeaderFamilies {
		canonical := http.CanonicalHeaderKey(name)
		for k := range h {
			if http.CanonicalHeaderKey(k) == canonical {
				h.Del(k)
			}
		}
	}
}

// InjectRealKey overwrites the auth header for authType with the real
// key. The client's previous header value (if any survived strip) is
// ignored — any fake placeholder the client used is fine, the real
// credential always wins.
func InjectRealKey(headers http.Header, realKey, authType string) error {
	rule, ok := authTypeMap[authType]
	if !ok {
		return fmt.Errorf("unknown auth_type: %s", authType)
	}

	targetValue := rule.Prefix + realKey
	canonical := http.CanonicalHeaderKey(rule.Header)

	// Remove any existing casing variants of the target header, then set once.
	for name := range headers {
		if http.CanonicalHeaderKey(name) == canonical {
			delete(headers, name)
		}
	}
	headers[canonical] = []string{targetValue}
	return nil
}
