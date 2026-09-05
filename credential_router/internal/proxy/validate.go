// Package validate provides field-level validation for credential_router.
//
// All validation functions follow the same contract: they return nil when
// valid, or an *platform.Error with CodeBadRequest describing the first violation
// found. Charsets are hardcoded by design:
//
//   - user_id:   [A-Za-z0-9._-]+   (URL-safe subset, no '~')
//   - api_base:  [A-Za-z0-9._:/?&=%-]+  plus control-char rejection plus url.Parse + scheme + host
//   - key_tag:   ^[a-z0-9-]+$   (lowercase only; tied to SQL UNIQUE)
//   - auth_type: ∈ {"openai", "anthropic", "google"}   (exact enumeration)
//
// Max lengths are config-driven (cfg.Admin.Validation.*).
package proxy

import (
	"fmt"
	"net/url"
	"strings"
	"unicode"

	"credential_router/internal/platform"
	"credential_router/internal/proxy/ssrf"
)

// hardcodedUnitSeparator is the former b64key delimiter that must never
// appear in any credential field. The rejection is retained so no field
// can smuggle the separator into any downstream key derivation that still
// expects "no 0x1F in input".
const hardcodedUnitSeparator = 0x1F

// isUserIDChar reports whether r is allowed in user_id (URL-safe subset).
// Allowed: A-Z, a-z, 0-9, '.', '_', '-'.
func isUserIDChar(r rune) bool {
	if r >= 'A' && r <= 'Z' {
		return true
	}
	if r >= 'a' && r <= 'z' {
		return true
	}
	if r >= '0' && r <= '9' {
		return true
	}
	return r == '.' || r == '_' || r == '-'
}

// isKeyTagChar reports whether r is allowed in key_tag (lowercase only).
// Allowed: a-z, 0-9, '-'.
func isKeyTagChar(r rune) bool {
	if r >= 'a' && r <= 'z' {
		return true
	}
	if r >= '0' && r <= '9' {
		return true
	}
	return r == '-'
}

// isAPIBaseChar reports whether r is in the URL-safe subset [A-Za-z0-9._:/?&=%-].
// APIBase additionally rejects ASCII control chars and runs url.Parse.
func isAPIBaseChar(r rune) bool {
	if r >= 'A' && r <= 'Z' {
		return true
	}
	if r >= 'a' && r <= 'z' {
		return true
	}
	if r >= '0' && r <= '9' {
		return true
	}
	return r == '.' || r == '_' || r == ':' || r == '/' || r == '?' ||
		r == '&' || r == '=' || r == '%' || r == '-'
}

// isControlChar reports whether r is an ASCII control character (0x00-0x1F).
func isControlChar(r rune) bool {
	return r >= 0x00 && r <= 0x1F
}

// rejectUnitSeparator checks for the former b64key delimiter and returns a
// specific error if found. This is called BEFORE generic charset checks so the
// error message tells the caller exactly why.
func rejectUnitSeparator(s string, name string) error {
	for _, r := range s {
		if r == hardcodedUnitSeparator {
			return platform.New(platform.CodeBadRequest, "validate",
				fmt.Sprintf("%s: contains forbidden 0x%02X (unit separator)", name, hardcodedUnitSeparator))
		}
	}
	return nil
}

// UserID validates a user identifier string. Charset [A-Za-z0-9._-]+.
func UserID(s string, maxLen int) error {
	if len(s) == 0 {
		return platform.New(platform.CodeBadRequest, "validate", "user_id: empty")
	}
	if len(s) > maxLen {
		return platform.New(platform.CodeBadRequest, "validate", fmt.Sprintf("user_id: exceeds max length %d", maxLen))
	}
	if err := rejectUnitSeparator(s, "user_id"); err != nil {
		return err
	}
	for _, r := range s {
		if !isUserIDChar(r) {
			return platform.New(platform.CodeBadRequest, "validate",
				fmt.Sprintf("user_id: contains invalid character 0x%02X (allowed: A-Za-z0-9._-)", r))
		}
	}
	return nil
}

// KeyTag validates a key tag identifier string. Charset ^[a-z0-9-]+$.
func KeyTag(s string, maxLen int) error {
	if len(s) == 0 {
		return platform.New(platform.CodeBadRequest, "validate", "key_tag: empty")
	}
	if len(s) > maxLen {
		return platform.New(platform.CodeBadRequest, "validate", fmt.Sprintf("key_tag: exceeds max length %d", maxLen))
	}
	if err := rejectUnitSeparator(s, "key_tag"); err != nil {
		return err
	}
	for _, r := range s {
		if !isKeyTagChar(r) {
			return platform.New(platform.CodeBadRequest, "validate",
				fmt.Sprintf("key_tag: contains invalid character 0x%02X (allowed: a-z0-9-)", r))
		}
	}
	return nil
}

// AuthType validates an authentication type identifier. Must be exactly
// one of the values in authTypeMap (currently "openai", "anthropic",
// "google") — the same set the proxy auth injector recognises.
func AuthType(s string, maxLen int) error {
	if len(s) == 0 {
		return platform.New(platform.CodeBadRequest, "validate", "auth_type: empty")
	}
	if len(s) > maxLen {
		return platform.New(platform.CodeBadRequest, "validate", fmt.Sprintf("auth_type: exceeds max length %d", maxLen))
	}
	switch s {
	case "openai", "anthropic", "google":
		return nil
	default:
		return platform.New(platform.CodeBadRequest, "validate",
			fmt.Sprintf("auth_type: must be one of {openai, anthropic, google}, got %q", s))
	}
}

// APIKey validates an opaque upstream API key string. The field carries
// no syntactic structure (providers issue keys in arbitrary charsets), so
// only length is bounded by maxLen (admin.validation.api_key_max_len).
// An empty key is rejected; callers that allow optional updates must
// skip the call instead of passing "".
func APIKey(s string, maxLen int) error {
	if len(s) == 0 {
		return platform.New(platform.CodeBadRequest, "validate", "api_key: empty")
	}
	if len(s) > maxLen {
		return platform.New(platform.CodeBadRequest, "validate", fmt.Sprintf("api_key: exceeds max length %d", maxLen))
	}
	return nil
}

// APIBase validates a real upstream base URL string. Charset
// [A-Za-z0-9._:/?&=%-]+ plus control-char rejection plus url.Parse + scheme +
// non-empty host.
//
// Use APIBaseWithPolicy when the caller (admin write paths such as
// createCredential / getCredential) needs internal hosts blocked.
func APIBase(s string, maxLen int) error {
	return APIBaseWithPolicy(s, maxLen, nil)
}

// APIBaseWithPolicy runs every check APIBase does, plus an SSRF gate against
// the supplied *ssrf.URLPolicy. A nil policy disables the SSRF check (every
// host passes), preserving APIBase's old behavior.
func APIBaseWithPolicy(s string, maxLen int, p *ssrf.URLPolicy) error {
	if len(s) == 0 {
		return platform.New(platform.CodeBadRequest, "validate", "api_base: empty")
	}
	if len(s) > maxLen {
		return platform.New(platform.CodeBadRequest, "validate", fmt.Sprintf("api_base: exceeds max length %d", maxLen))
	}
	if err := rejectUnitSeparator(s, "api_base"); err != nil {
		return err
	}
	for _, r := range s {
		if isControlChar(r) {
			return platform.New(platform.CodeBadRequest, "validate",
				fmt.Sprintf("api_base: contains forbidden control character 0x%02X", r))
		}
		if !isAPIBaseChar(r) {
			return platform.New(platform.CodeBadRequest, "validate",
				fmt.Sprintf("api_base: contains invalid character 0x%02X (allowed: A-Za-z0-9._:/?&=%% plus -)", r))
		}
	}
	u, err := url.Parse(s)
	if err != nil {
		return platform.New(platform.CodeBadRequest, "validate", fmt.Sprintf("api_base: %s", err.Error()))
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return platform.New(platform.CodeBadRequest, "validate", "api_base: must have http or https scheme")
	}
	if u.Host == "" {
		return platform.New(platform.CodeBadRequest, "validate", "api_base: empty host")
	}
	return p.CheckHost(u.Host)
}

// NormalizeAPIBase canonicalises an upstream base URL for cache-key
// uniqueness — the same logical upstream ("https://api.openai.com" and
// "https://api.openai.com/" must hash to the same key) must collapse to
// one form. Trim whitespace; strip trailing slashes (idempotent).
func NormalizeAPIBase(s string) string {
	s = strings.TrimSpace(s)
	// Strip trailing slashes AND the whitespace they expose, in a single pass.
	// Loop-based stripping ("/ " → "" → "") is non-idempotent because the
	// intermediate whitespace becomes load-bearing on subsequent passes. Match
	// TrimSpace's whitespace definition so Unicode whitespace (NBSP, NEL, …)
	// exposed by a trailing slash is also stripped.
	return strings.TrimRightFunc(s, func(r rune) bool {
		return r == '/' || unicode.IsSpace(r)
	})
}
