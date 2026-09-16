//go:build cgo

// Fuzz tests for internal/credmgr.
//
// Quick:  go test -run=^$ -fuzz=^Fuzz -fuzztime=10s ./internal/vault/...
// Full:   go test -run=^$ -fuzz=^Fuzz -fuzztime=60s ./internal/vault/...

package vault_test

import (
	"strings"
	"testing"

	"credential_router/internal/credmgr/crypto"
)

// FuzzParseMode — crypto-mode string parsing. Exactly "aes" and "sm" are
// accepted and must map to the non-zero ModeAES/ModeSM4; anything else must
// error without a value. Never panics, including on long/unicode/control input.
func FuzzParseMode(f *testing.F) {
	f.Add("")
	f.Add("aes")
	f.Add("sm")
	f.Add("AES")
	f.Add(" aes ")
	f.Add("aes\x00")
	f.Add(strings.Repeat("a", 4096))
	f.Add("中文")
	f.Add("\xff\xfe\xfd")
	f.Fuzz(func(t *testing.T, s string) {
		mode, err := crypto.ParseMode(s)
		switch s {
		case "aes":
			if err != nil {
				t.Fatalf("aes: unexpected error %v", err)
			}
			if mode != crypto.ModeAES {
				t.Fatalf("aes: mode=%v, want ModeAES(0x01)", mode)
			}
		case "sm":
			if err != nil {
				t.Fatalf("sm: unexpected error %v", err)
			}
			if mode != crypto.ModeSM4 {
				t.Fatalf("sm: mode=%v, want ModeSM4(0x02)", mode)
			}
		default:
			if err == nil {
				t.Fatalf("ParseMode(%q) succeeded, want error", s)
			}
			if mode != 0 {
				t.Fatalf("ParseMode(%q) returned mode=%v with error, want zero", s, mode)
			}
		}
	})
}
