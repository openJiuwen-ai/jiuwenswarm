//go:build cgo

package admin_test

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"credential_router/internal/credmgr/store"
)

func TestHealthOK(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "GET", "/v1/health", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestKeystoreStatus(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "GET", "/v1/keystore/status", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestKeystoreStatusAfterKEKRotate(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/keystore/shards", map[string]any{"action": "rotate-s1"})
	if rec.Code != http.StatusOK {
		t.Fatalf("kek rotate: status=%d body=%s", rec.Code, rec.Body.String())
	}
	rec2 := h.do(t, "GET", "/v1/keystore/status", nil)
	if rec2.Code != http.StatusOK {
		t.Fatalf("status=%d", rec2.Code)
	}
	if !contains(rec2.Body.String(), `"rotation_state":"idle"`) {
		t.Errorf("expected idle state after sync rotation, body=%s", rec2.Body.String())
	}
}

func TestKEKRotateSuccess(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/keystore/shards", map[string]any{"action": "rotate-s1"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if !contains(rec.Body.String(), `"new_kek_version"`) {
		t.Errorf("expected new_kek_version in body, got %s", rec.Body.String())
	}
}

func TestS2RotateSuccess(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/keystore/shards", map[string]string{"action": "rotate-s2"})
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if !contains(rec.Body.String(), `"new_kek_version"`) {
		t.Errorf("expected new_kek_version in body, got %s", rec.Body.String())
	}
}

func TestDEKRotateSuccess(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "POST", "/v1/keystore/rotate-dek", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if !contains(rec.Body.String(), `"new_dek_version"`) {
		t.Errorf("expected new_dek_version in body, got %s", rec.Body.String())
	}
}

func TestStatusIncludesRotationStateFields(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	rec := h.do(t, "GET", "/v1/keystore/status", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d", rec.Code)
	}
	for _, field := range []string{
		`"rotation_state"`,
		`"straggler_count"`,
		`"wrapped_dek_present"`,
		`"file_shard_rotated_at"`,
	} {
		if !contains(rec.Body.String(), field) {
			t.Errorf("expected %s in body, got %s", field, rec.Body.String())
		}
	}
}

// TestCrossTypeRotationRejected409: while a KEK rotation is pending, POST
// /v1/keystore/rotate-dek must return 409 with error code
// "rotation_in_progress"; while a DEK rotation is pending, POST
// /v1/keystore/shards (rotate-s1) must do the same.
func TestCrossTypeRotationRejected409(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	ctx := context.Background()

	// KEK pending → DEK rotation rejected.
	km, err := h.store.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = km.ActiveKekVersion + 1
	if err := h.store.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	rec := h.do(t, "POST", "/v1/keystore/rotate-dek", nil)
	if rec.Code != http.StatusConflict {
		t.Fatalf("rotate-dek with KEK pending: status=%d body=%s, want 409", rec.Code, rec.Body.String())
	}
	if code := envelopeErrorCode(t, rec.Body.Bytes()); code != "rotation_in_progress" {
		t.Errorf("rotate-dek error code=%q, want rotation_in_progress", code)
	}

	// DEK pending → KEK rotation (shards rotate-s1) rejected.
	km, err = h.store.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = 0
	km.PendingDekVersion = km.ActiveDekVersion + 1
	if err := h.store.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	rec = h.do(t, "POST", "/v1/keystore/shards", map[string]any{"action": "rotate-s1"})
	if rec.Code != http.StatusConflict {
		t.Fatalf("shards rotate-s1 with DEK pending: status=%d body=%s, want 409", rec.Code, rec.Body.String())
	}
	if code := envelopeErrorCode(t, rec.Body.Bytes()); code != "rotation_in_progress" {
		t.Errorf("shards error code=%q, want rotation_in_progress", code)
	}
}

// TestStatusReencryptingStateWithStragglers covers the keystoreStatus pending-DEK
// branch: with pending_dek_version > 0 and rows still at the old
// dek_version, status must report rotation_state "reencrypting" with a
// non-null straggler_count equal to the number of rows below the target.
func TestStatusReencryptingStateWithStragglers(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	ctx := context.Background()

	km, err := h.store.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	target := km.ActiveDekVersion + 1
	km.PendingDekVersion = target
	if err := h.store.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	for _, uid := range []string{"u1", "u2", "u3"} {
		cred := &store.Credential{
			UserID: uid, APIBase: "https://api.example.com", KeyTag: "default",
			APIKeyCipher: []byte("enc"), AuthType: "openai",
			KekVersion: km.ActiveKekVersion, DekVersion: km.ActiveDekVersion,
		}
		if err := h.store.InsertCredential(ctx, cred); err != nil {
			t.Fatal(err)
		}
	}

	rec := h.do(t, "GET", "/v1/keystore/status", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !contains(body, `"rotation_state":"reencrypting"`) {
		t.Errorf("want rotation_state reencrypting, body=%s", body)
	}
	if !contains(body, `"straggler_count":3`) {
		t.Errorf("want straggler_count 3, body=%s", body)
	}
}

// TestStatusReadyToCommitWhenDrained verifies that once every row is at the
// target dek_version, status reports "ready_to_commit" with straggler_count 0.
func TestStatusReadyToCommitWhenDrained(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	ctx := context.Background()

	km, err := h.store.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	target := km.ActiveDekVersion + 1
	km.PendingDekVersion = target
	if err := h.store.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	cred := &store.Credential{
		UserID: "u1", APIBase: "https://api.example.com", KeyTag: "default",
		APIKeyCipher: []byte("enc"), AuthType: "openai",
		KekVersion: km.ActiveKekVersion, DekVersion: km.ActiveDekVersion,
	}
	if err := h.store.InsertCredential(ctx, cred); err != nil {
		t.Fatal(err)
	}

	// Simulate Phase A completion: bump all rows to the target dek_version.
	if _, err := h.store.AdminDB().ExecContext(ctx,
		`UPDATE credentials SET dek_version = ?, updated_at = ? WHERE dek_version < ?`,
		target, time.Now().Unix(), target); err != nil {
		t.Fatal(err)
	}

	rec := h.do(t, "GET", "/v1/keystore/status", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !contains(body, `"rotation_state":"ready_to_commit"`) {
		t.Errorf("want rotation_state ready_to_commit, body=%s", body)
	}
	if !contains(body, `"straggler_count":0`) {
		t.Errorf("want straggler_count 0, body=%s", body)
	}
}

// TestStatusSwapPendingForKEK covers the keystoreStatus pending-KEK branch:
// with pending_kek_version > 0 the state must be "swap_pending" and
// straggler_count must stay null (it only applies to DEK reencryption).
func TestStatusSwapPendingForKEK(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	ctx := context.Background()

	km, err := h.store.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = km.ActiveKekVersion + 1
	if err := h.store.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	rec := h.do(t, "GET", "/v1/keystore/status", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !contains(body, `"rotation_state":"swap_pending"`) {
		t.Errorf("want rotation_state swap_pending, body=%s", body)
	}
	if !contains(body, `"straggler_count":null`) {
		t.Errorf("want straggler_count null for KEK swap, body=%s", body)
	}
}

// TestHealthRotatingWhenPending verifies the health endpoint reports
// "rotating" while a KEK rotation is pending.
func TestHealthRotatingWhenPending(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()
	ctx := context.Background()

	km, err := h.store.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = km.ActiveKekVersion + 1
	if err := h.store.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	rec := h.do(t, "GET", "/v1/health", nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", rec.Code, rec.Body.String())
	}
	if !contains(rec.Body.String(), `"status":"rotating"`) {
		t.Errorf("want status rotating, body=%s", rec.Body.String())
	}
}

// TestKEKRotationSurvivesClientCancel verifies that a KEK rotation completes
// even when the client's request context is cancelled before the handler runs.
// beginCtx derives from context.Background() so client disconnect cannot
// interrupt an in-progress rotation (would leave DB in pending state).
func TestKEKRotationSurvivesClientCancel(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()

	body := `{"action":"rotate-s1"}`
	req := httptest.NewRequest("POST", "/v1/keystore/shards", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // simulate client disconnect before request processed
	req = req.WithContext(ctx)

	rec := httptest.NewRecorder()
	h.router.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s (rotation should survive client cancel)", rec.Code, rec.Body.String())
	}

	rec2 := h.do(t, "GET", "/v1/keystore/status", nil)
	if !contains(rec2.Body.String(), `"rotation_state":"idle"`) {
		t.Errorf("expected idle after rotation despite client cancel, body=%s", rec2.Body.String())
	}
}

func TestDEKRotationSurvivesClientCancel(t *testing.T) {
	h := newAdminHarness(t)
	defer h.cleanup()

	req := httptest.NewRequest("POST", "/v1/keystore/rotate-dek", nil)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	req = req.WithContext(ctx)

	rec := httptest.NewRecorder()
	h.router.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s (rotation should survive client cancel)", rec.Code, rec.Body.String())
	}
}



func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

var _ = time.Second
