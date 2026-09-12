//go:build cgo

package admin_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"credential_router/internal/credmgr/store"
)

// FuzzProxyKeyFormat — pure, no harness. Must never panic on any input.
// The proxy_key charset is `cr_pk_` + base64url; fuzz exercises garbage,
// empty, huge, and near-valid inputs against GenerateProxyKey's shape and
// the crate parser assumptions.
func FuzzProxyKeyFormat(f *testing.F) {
	f.Add("")
	f.Add("cr_pk_")
	f.Add(store.GenerateProxyKey())
	f.Add("\x00\x00\x00")
	f.Add(strings.Repeat("A", 1000))
	f.Add("abc-def_ghi.jkl")
	f.Add("cr_pk_" + strings.Repeat("AB", 100))
	f.Add(strings.Repeat("cr_pk_", 10))
	f.Fuzz(func(t *testing.T, s string) {
		_ = proxyKeyShapeValid(s)
	})
}

func proxyKeyShapeValid(s string) bool {
	if !strings.HasPrefix(s, "cr_pk_") {
		return false
	}
	rest := strings.TrimPrefix(s, "cr_pk_")
	if rest == "" {
		return false
	}
	for _, r := range rest {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
			return false
		}
	}
	return true
}

// FuzzDecodeJSON — random bytes against credentialRequest. Must never panic.
// Catches decoder bugs in malformed JSON, huge nesting, weird escapes.
func FuzzDecodeJSON(f *testing.F) {
	f.Add([]byte(`{"user_id":"u","api_base":"https://x","key_tag":"t","api_key":"k","auth_type":"openai"}`))
	f.Add([]byte(``))
	f.Add([]byte(`{`))
	f.Add([]byte(`}`))
	f.Add([]byte(`{"user_id":}`))
	f.Add([]byte(strings.Repeat("[", 1000)))
	f.Add([]byte(`"` + strings.Repeat(`\u0000`, 100) + `"`))
	f.Fuzz(func(t *testing.T, body []byte) {
		var req credentialRequest
		_ = json.Unmarshal(body, &req)
	})
}

// FuzzCreateCredential — handler-level fuzz. Random body POSTed to
// /v1/credentials. The harness is rebuilt per iteration (≈30ms) so we cap
// iterations through the seed corpus. Goal: never panic, never deadlock, no
// infinite loop; status code may be 2xx/4xx/5xx — all are acceptable as long
// as the response is a valid JSON envelope.
func FuzzCreateCredential(f *testing.F) {
	f.Add([]byte(`{"user_id":"u","api_base":"https://api.example.com","key_tag":"default","api_key":"sk","auth_type":"openai"}`))
	f.Add([]byte(`{"user_id":"","api_base":"","key_tag":"","api_key":"","auth_type":""}`))
	f.Add([]byte(`{`))
	f.Add([]byte(``))
	f.Add([]byte(`null`))
	f.Add([]byte(`"` + strings.Repeat("x", 10000) + `"`))
	f.Fuzz(func(t *testing.T, body []byte) {
		h := newAdminHarness(t)
		defer h.cleanup()

		req := httptest.NewRequest(http.MethodPost, "/v1/credentials", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rr := httptest.NewRecorder()
		h.router.ServeHTTP(rr, req)

		// Response body must be parseable JSON envelope; otherwise the handler
		// is silently corrupting its output contract.
		var env map[string]json.RawMessage
		if err := json.Unmarshal(rr.Body.Bytes(), &env); err != nil && rr.Body.Len() > 0 {
			t.Fatalf("non-JSON response (status=%d): %q", rr.Code, rr.Body.String())
		}
	})
}

// FuzzShardRotate — handler-level fuzz. Random body POSTed to
// /v1/keystore/shards. Must never panic; the shard rotation API has multiple
// branches (rotate-s1 now auto-generates S1; rotate-s2 with hex) and we want fuzz to
// exercise the path-decoding/validation edge cases.
func FuzzShardRotate(f *testing.F) {
	f.Add([]byte(`{"action":"rotate-s1"}`))
	f.Add([]byte(`{"action":"rotate-s2","new_s2_hex":"00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"}`))
	f.Add([]byte(`{}`))
	f.Add([]byte(`{"action":"unknown"}`))
	f.Add([]byte(`{"action":"rotate-s1"}`))
	f.Add([]byte(`{"action":"rotate-s1"}`))
	f.Fuzz(func(t *testing.T, body []byte) {
		h := newAdminHarness(t)
		defer h.cleanup()

		req := httptest.NewRequest(http.MethodPost, "/v1/keystore/shards", bytes.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rr := httptest.NewRecorder()
		h.router.ServeHTTP(rr, req)

		// 409 Conflict from "rotation in progress" is acceptable (depends on
		// prior fuzz state in the harness). Anything else must be valid JSON.
		if rr.Code != http.StatusConflict && rr.Body.Len() > 0 {
			var env map[string]json.RawMessage
			if err := json.Unmarshal(rr.Body.Bytes(), &env); err != nil {
				t.Fatalf("non-JSON response (status=%d): %q", rr.Code, rr.Body.String())
			}
		}
	})
}
