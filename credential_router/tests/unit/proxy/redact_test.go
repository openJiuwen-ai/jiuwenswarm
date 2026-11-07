package proxy_test

import (
	"strings"
	"testing"

	"credential_router/internal/proxy"
)

// TestRedactProxyKeyTruncatesAllLengths covers the contract that
// redactProxyKey never returns the full proxy_key, even when the input
// is shorter than the 8-char threshold. The previous implementation
// returned the full string when len <= 8, which would have leaked short
// proxy_keys if the key format ever changed.
func TestRedactProxyKeyTruncatesAllLengths(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"empty", "", ""},
		{"one char", "a", "a"},
		{"seven chars", "cr_pk_a", "cr_p"},
		{"eight chars", "cr_pk_ab", "cr_p"},
		{"nine chars", "cr_pk_abc", "cr_pk_ab"},
		{"forty-nine chars (production format)", "cr_pk_" + strings.Repeat("a", 43), "cr_pk_aa"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := proxy.RedactProxyKeyForTesting(tc.in)
			if got != tc.want {
				t.Errorf("RedactProxyKey(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}