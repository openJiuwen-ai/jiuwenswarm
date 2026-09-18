//go:build cgo

package store_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"credential_router/internal/platform"
	"credential_router/internal/credmgr/store"
)

func ctxb() context.Context {
	return context.Background()
}

func newTestCred(userID, apiBase, keyTag string) *store.Credential {
	return &store.Credential{
		UserID:       userID,
		APIBase:      apiBase,
		KeyTag:       keyTag,
		APIKeyCipher: []byte("test-cipher-" + keyTag),
		AuthType:     "openai",
	}
}

// ---------------------------------------------------------------------------
// 1. TestInsertCredential — insert, returns ID, row_version=1, created_at set
// ---------------------------------------------------------------------------
func TestInsertCredential(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	if c.ID == 0 {
		t.Fatal("expected non-zero ID after insert")
	}
	if c.RowVersion != 1 {
		t.Fatalf("expected row_version=1, got %d", c.RowVersion)
	}
	if c.CreatedAt == 0 {
		t.Fatal("expected created_at to be set")
	}
	if c.UpdatedAt == 0 {
		t.Fatal("expected updated_at to be set")
	}
}

// ---------------------------------------------------------------------------
// 2. TestInsertCredentialDuplicate — platform.ErrConflict on duplicate
// ---------------------------------------------------------------------------
func TestInsertCredentialDuplicate(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c1 := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c1); err != nil {
		t.Fatalf("first insert: %v", err)
	}
	c2 := newTestCred("u1", "https://example.com", "default")
	err := s.InsertCredential(ctxb(), c2)
	if err == nil {
		t.Fatal("expected error on duplicate insert")
	}
	if !errors.Is(err, platform.ErrConflict) {
		t.Fatalf("expected platform.ErrConflict, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 3. TestInsertCredentialNil — error on nil
// ---------------------------------------------------------------------------
func TestInsertCredentialNil(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	err := s.InsertCredential(ctxb(), nil)
	if err == nil {
		t.Fatal("expected error for nil credential")
	}
}

// ---------------------------------------------------------------------------
// 4. TestGetCredentialByUserURLTagFound — returns inserted row
// ---------------------------------------------------------------------------
func TestGetCredentialByUserURLTagFound(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	orig := newTestCred("u1", "https://example.com", "default")
	orig.APIKeyCipher = []byte("secret-cipher")
	orig.KekVersion = 3
	orig.DekVersion = 5
	if err := s.InsertCredential(ctxb(), orig); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}

	got, err := s.GetCredentialByUserURLTag(ctxb(), "u1", "https://example.com", "default")
	if err != nil {
		t.Fatalf("GetCredentialByUserURLTag: %v", err)
	}
	if got.ID != orig.ID {
		t.Fatalf("ID: expected %d, got %d", orig.ID, got.ID)
	}
	if got.UserID != "u1" {
		t.Fatalf("UserID: expected u1, got %q", got.UserID)
	}
	if got.APIBase != "https://example.com" {
		t.Fatalf("APIBase: expected https://example.com, got %q", got.APIBase)
	}
	if got.KeyTag != "default" {
		t.Fatalf("KeyTag: expected default, got %q", got.KeyTag)
	}
	if string(got.APIKeyCipher) != "secret-cipher" {
		t.Fatalf("APIKeyCipher: expected secret-cipher, got %q", string(got.APIKeyCipher))
	}
	if got.AuthType != "openai" {
		t.Fatalf("AuthType: expected openai, got %q", got.AuthType)
	}
	if got.RowVersion != 1 {
		t.Fatalf("RowVersion: expected 1, got %d", got.RowVersion)
	}
	if got.KekVersion != 3 {
		t.Fatalf("KekVersion: expected 3, got %d", got.KekVersion)
	}
	if got.DekVersion != 5 {
		t.Fatalf("DekVersion: expected 5, got %d", got.DekVersion)
	}
	if got.CreatedAt != orig.CreatedAt {
		t.Fatalf("CreatedAt: expected %d, got %d", orig.CreatedAt, got.CreatedAt)
	}
	if got.UpdatedAt != orig.UpdatedAt {
		t.Fatalf("UpdatedAt: expected %d, got %d", orig.UpdatedAt, got.UpdatedAt)
	}
}

// ---------------------------------------------------------------------------
// 5. TestGetCredentialByUserURLTagNotFound — platform.ErrNotFound
// ---------------------------------------------------------------------------
func TestGetCredentialByUserURLTagNotFound(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	_, err := s.GetCredentialByUserURLTag(ctxb(), "nonexistent", "https://x.com", "tag")
	if err == nil {
		t.Fatal("expected error for missing credential")
	}
	if !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("expected platform.ErrNotFound, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 6. TestListCredentialsEmpty — empty slice on empty DB
// ---------------------------------------------------------------------------
func TestListCredentialsEmpty(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	creds, err := s.ListCredentials(ctxb(), 0, 0)
	if err != nil {
		t.Fatalf("ListCredentials: %v", err)
	}
	if len(creds) != 0 {
		t.Fatalf("expected empty slice, got %d items", len(creds))
	}
}

// ---------------------------------------------------------------------------
// 7. TestListCredentialsMultiple — 3 rows, all returned in order
// ---------------------------------------------------------------------------
func TestListCredentialsMultiple(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	for i := 0; i < 3; i++ {
		c := newTestCred("u1", "https://example.com", "k"+itos(i))
		if err := s.InsertCredential(ctxb(), c); err != nil {
			t.Fatalf("insert %d: %v", i, err)
		}
	}

	creds, err := s.ListCredentials(ctxb(), 0, 0)
	if err != nil {
		t.Fatalf("ListCredentials: %v", err)
	}
	if len(creds) != 3 {
		t.Fatalf("expected 3 credentials, got %d", len(creds))
	}
	// Verify order by key_tag
	expectedTags := []string{"k0", "k1", "k2"}
	for i, c := range creds {
		if c.KeyTag != expectedTags[i] {
			t.Fatalf("cred[%d].KeyTag: expected %q, got %q", i, expectedTags[i], c.KeyTag)
		}
	}
}

// itos converts an int to a string (simple helper to avoid fmt import in tests).
func itos(i int) string {
	if i == 0 {
		return "0"
	}
	s := ""
	for i > 0 {
		s = string(rune('0'+i%10)) + s
		i /= 10
	}
	return s
}

// ---------------------------------------------------------------------------
// 8. TestListCredentialsLimit — limit works
// ---------------------------------------------------------------------------
func TestListCredentialsLimit(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	for i := 0; i < 3; i++ {
		c := newTestCred("u1", "https://example.com", "k"+itos(i))
		if err := s.InsertCredential(ctxb(), c); err != nil {
			t.Fatalf("insert %d: %v", i, err)
		}
	}

	creds, err := s.ListCredentials(ctxb(), 2, 0)
	if err != nil {
		t.Fatalf("ListCredentials: %v", err)
	}
	if len(creds) != 2 {
		t.Fatalf("expected 2 credentials, got %d", len(creds))
	}
}

// ---------------------------------------------------------------------------
// 9. TestListCredentialsOffset — offset works
// ---------------------------------------------------------------------------
func TestListCredentialsOffset(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	for i := 0; i < 3; i++ {
		c := newTestCred("u1", "https://example.com", "k"+itos(i))
		if err := s.InsertCredential(ctxb(), c); err != nil {
			t.Fatalf("insert %d: %v", i, err)
		}
	}

	creds, err := s.ListCredentials(ctxb(), 0, 1)
	if err != nil {
		t.Fatalf("ListCredentials: %v", err)
	}
	if len(creds) != 2 {
		t.Fatalf("expected 2 credentials, got %d", len(creds))
	}
	if creds[0].KeyTag != "k1" {
		t.Fatalf("first result KeyTag: expected k1, got %q", creds[0].KeyTag)
	}
}

// ---------------------------------------------------------------------------
// 10. TestListCredentialsLimitAndOffset — limit + offset combined
// ---------------------------------------------------------------------------
func TestListCredentialsLimitAndOffset(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	for i := 0; i < 3; i++ {
		c := newTestCred("u1", "https://example.com", "k"+itos(i))
		if err := s.InsertCredential(ctxb(), c); err != nil {
			t.Fatalf("insert %d: %v", i, err)
		}
	}

	creds, err := s.ListCredentials(ctxb(), 1, 1)
	if err != nil {
		t.Fatalf("ListCredentials: %v", err)
	}
	if len(creds) != 1 {
		t.Fatalf("expected 1 credential, got %d", len(creds))
	}
	if creds[0].KeyTag != "k1" {
		t.Fatalf("KeyTag: expected k1, got %q", creds[0].KeyTag)
	}
}

// ---------------------------------------------------------------------------
// 11. TestListCredentialsNegativeLimit — error on negative limit
// ---------------------------------------------------------------------------
func TestListCredentialsNegativeLimit(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	_, err := s.ListCredentials(ctxb(), -1, 0)
	if err == nil {
		t.Fatal("expected error for negative limit")
	}
}

// ---------------------------------------------------------------------------
// 12. TestListCredentialsNegativeOffset — error on negative offset
// ---------------------------------------------------------------------------
func TestListCredentialsNegativeOffset(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	_, err := s.ListCredentials(ctxb(), 0, -1)
	if err == nil {
		t.Fatal("expected error for negative offset")
	}
}

// ---------------------------------------------------------------------------
// 13. TestInsertCredentialSetsKekDekDefaults — 0 value defaults to 1
// ---------------------------------------------------------------------------
func TestInsertCredentialSetsKekDekDefaults(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	c.KekVersion = 0
	c.DekVersion = 0
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	if c.KekVersion != 1 {
		t.Fatalf("KekVersion: expected 1 (default), got %d", c.KekVersion)
	}
	if c.DekVersion != 1 {
		t.Fatalf("DekVersion: expected 1 (default), got %d", c.DekVersion)
	}
}

// ---------------------------------------------------------------------------
// 14. TestInsertCredentialPreservesKekDek — explicit values preserved
// ---------------------------------------------------------------------------
func TestInsertCredentialPreservesKekDek(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	c.KekVersion = 5
	c.DekVersion = 7
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	if c.KekVersion != 5 {
		t.Fatalf("KekVersion: expected 5, got %d", c.KekVersion)
	}
	if c.DekVersion != 7 {
		t.Fatalf("DekVersion: expected 7, got %d", c.DekVersion)
	}

	got, err := s.GetCredentialByUserURLTag(ctxb(), "u1", "https://example.com", "default")
	if err != nil {
		t.Fatalf("GetCredentialByUserURLTag: %v", err)
	}
	if got.KekVersion != 5 {
		t.Fatalf("KekVersion from DB: expected 5, got %d", got.KekVersion)
	}
	if got.DekVersion != 7 {
		t.Fatalf("DekVersion from DB: expected 7, got %d", got.DekVersion)
	}
}

// ---------------------------------------------------------------------------
// 15. TestUpdateCredentialSuccess — update with correct row_version
// ---------------------------------------------------------------------------
func TestUpdateCredentialSuccess(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	origID := c.ID
	origRV := c.RowVersion // should be 1

	c.APIKeyCipher = []byte("updated-cipher")
	c.AuthType = "anthropic"
	if err := s.UpdateCredential(ctxb(), origID, c); err != nil {
		t.Fatalf("UpdateCredential: %v", err)
	}
	if c.RowVersion != origRV+1 {
		t.Fatalf("RowVersion: expected %d, got %d", origRV+1, c.RowVersion)
	}
	if c.ID != origID {
		t.Fatalf("ID changed: expected %d, got %d", origID, c.ID)
	}
	if c.UpdatedAt == 0 {
		t.Fatal("UpdatedAt should be set after update")
	}

	// Verify via Get
	got, err := s.GetCredentialByUserURLTag(ctxb(), "u1", "https://example.com", "default")
	if err != nil {
		t.Fatalf("Get after update: %v", err)
	}
	if string(got.APIKeyCipher) != "updated-cipher" {
		t.Fatalf("APIKeyCipher: expected updated-cipher, got %q", string(got.APIKeyCipher))
	}
	if got.AuthType != "anthropic" {
		t.Fatalf("AuthType: expected anthropic, got %q", got.AuthType)
	}
	if got.RowVersion != origRV+1 {
		t.Fatalf("RowVersion in DB: expected %d, got %d", origRV+1, got.RowVersion)
	}
}

// ---------------------------------------------------------------------------
// 16. TestUpdateCredentialNotFound — non-existent id → platform.ErrNotFound
// ---------------------------------------------------------------------------
func TestUpdateCredentialNotFound(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	err := s.UpdateCredential(ctxb(), 999, c)
	if err == nil {
		t.Fatal("expected error for non-existent id")
	}
	if !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("expected platform.ErrNotFound, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 17. TestUpdateCredentialVersionConflict — wrong row_version → platform.ErrConflict
// ---------------------------------------------------------------------------
func TestUpdateCredentialVersionConflict(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	// Use a stale row_version
	c.RowVersion = 42
	c.APIKeyCipher = []byte("stale-update")
	err := s.UpdateCredential(ctxb(), c.ID, c)
	if err == nil {
		t.Fatal("expected error for version conflict")
	}
	if !errors.Is(err, platform.ErrConflict) {
		t.Fatalf("expected platform.ErrConflict, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 18. TestUpdateCredentialSetsUpdatedAt — updated_at is set to recent time
// ---------------------------------------------------------------------------
func TestUpdateCredentialSetsUpdatedAt(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	before := time.Now().Unix()
	c.APIKeyCipher = []byte("new-cipher")
	if err := s.UpdateCredential(ctxb(), c.ID, c); err != nil {
		t.Fatalf("UpdateCredential: %v", err)
	}
	if c.UpdatedAt < before {
		t.Fatalf("UpdatedAt (%d) should be >= before (%d)", c.UpdatedAt, before)
	}
}

// ---------------------------------------------------------------------------
// 19. TestUpdateCredentialPreservesOtherFields — only specified fields change
// ---------------------------------------------------------------------------
func TestUpdateCredentialPreservesOtherFields(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	c.KekVersion = 3
	c.DekVersion = 5
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	origID := c.ID
	origUserID := c.UserID
	origAPIBase := c.APIBase
	origKeyTag := c.KeyTag
	origKek := c.KekVersion
	origDek := c.DekVersion

	// Update only api_key_cipher
	c.APIKeyCipher = []byte("only-cipher-changed")
	if err := s.UpdateCredential(ctxb(), origID, c); err != nil {
		t.Fatalf("UpdateCredential: %v", err)
	}

	got, err := s.GetCredentialByUserURLTag(ctxb(), origUserID, origAPIBase, origKeyTag)
	if err != nil {
		t.Fatalf("Get after update: %v", err)
	}
	if string(got.APIKeyCipher) != "only-cipher-changed" {
		t.Fatalf("APIKeyCipher: expected only-cipher-changed, got %q", string(got.APIKeyCipher))
	}
	if got.UserID != origUserID {
		t.Fatalf("UserID changed: expected %q, got %q", origUserID, got.UserID)
	}
	if got.APIBase != origAPIBase {
		t.Fatalf("APIBase changed: expected %q, got %q", origAPIBase, got.APIBase)
	}
	if got.KeyTag != origKeyTag {
		t.Fatalf("KeyTag changed: expected %q, got %q", origKeyTag, got.KeyTag)
	}
	if got.KekVersion != origKek {
		t.Fatalf("KekVersion changed: expected %d, got %d", origKek, got.KekVersion)
	}
	if got.DekVersion != origDek {
		t.Fatalf("DekVersion changed: expected %d, got %d", origDek, got.DekVersion)
	}
}

// ---------------------------------------------------------------------------
// 19a. TestUpdateCredentialPartialKeepsAPIKey — APIKeyCipher=nil means
//
//	"keep existing api_key_cipher"; only auth_type changes.
//
// ---------------------------------------------------------------------------
func TestUpdateCredentialPartialKeepsAPIKey(t *testing.T) {
	s := openMem(t)
	defer s.Close()
	ctx := ctxb()

	orig := newTestCred("u1", "https://api.example.com", "default")
	orig.APIKeyCipher = []byte("orig-cipher")
	orig.AuthType = "openai"
	if err := s.InsertCredential(ctx, orig); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}

	updated := &store.Credential{
		ID: orig.ID, UserID: orig.UserID, APIBase: orig.APIBase, KeyTag: orig.KeyTag,
		KekVersion: orig.KekVersion, DekVersion: orig.DekVersion,
		APIKeyCipher: nil, // partial: keep existing
		AuthType:     "anthropic",
		RowVersion:   orig.RowVersion,
	}
	if err := s.UpdateCredential(ctx, orig.ID, updated); err != nil {
		t.Fatalf("UpdateCredential: %v", err)
	}

	got, err := s.GetCredentialByUserURLTag(ctx, "u1", "https://api.example.com", "default")
	if err != nil {
		t.Fatalf("Get after update: %v", err)
	}
	if string(got.APIKeyCipher) != "orig-cipher" {
		t.Fatalf("APIKeyCipher=%q, want orig-cipher (preserved)", got.APIKeyCipher)
	}
	if got.AuthType != "anthropic" {
		t.Fatalf("AuthType=%q, want anthropic (updated)", got.AuthType)
	}
	if got.RowVersion != orig.RowVersion+1 {
		t.Fatalf("RowVersion=%d, want %d", got.RowVersion, orig.RowVersion+1)
	}
}

// ---------------------------------------------------------------------------
// 19b. TestUpdateCredentialPartialKeepsAuthType — AuthType="" means
//
//	"keep existing auth_type"; only api_key_cipher changes.
//
// ---------------------------------------------------------------------------
func TestUpdateCredentialPartialKeepsAuthType(t *testing.T) {
	s := openMem(t)
	defer s.Close()
	ctx := ctxb()

	orig := newTestCred("u1", "https://api.example.com", "default")
	orig.APIKeyCipher = []byte("orig-cipher")
	orig.AuthType = "openai"
	if err := s.InsertCredential(ctx, orig); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}

	updated := &store.Credential{
		ID: orig.ID, UserID: orig.UserID, APIBase: orig.APIBase, KeyTag: orig.KeyTag,
		KekVersion: orig.KekVersion, DekVersion: orig.DekVersion,
		APIKeyCipher: []byte("new-cipher"),
		AuthType:     "", // partial: keep existing
		RowVersion:   orig.RowVersion,
	}
	if err := s.UpdateCredential(ctx, orig.ID, updated); err != nil {
		t.Fatalf("UpdateCredential: %v", err)
	}

	got, err := s.GetCredentialByUserURLTag(ctx, "u1", "https://api.example.com", "default")
	if err != nil {
		t.Fatalf("Get after update: %v", err)
	}
	if string(got.APIKeyCipher) != "new-cipher" {
		t.Fatalf("APIKeyCipher=%q, want new-cipher (updated)", got.APIKeyCipher)
	}
	if got.AuthType != "openai" {
		t.Fatalf("AuthType=%q, want openai (preserved)", got.AuthType)
	}
}

// ---------------------------------------------------------------------------
// 20. TestDeleteCredentialSuccess — delete with correct row_version
// ---------------------------------------------------------------------------
func TestDeleteCredentialSuccess(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	if err := s.DeleteCredential(ctxb(), c.ID, c.RowVersion); err != nil {
		t.Fatalf("DeleteCredential: %v", err)
	}
	// Verify deletion
	_, err := s.GetCredentialByUserURLTag(ctxb(), "u1", "https://example.com", "default")
	if err == nil {
		t.Fatal("expected error after deletion")
	}
	if !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("expected platform.ErrNotFound, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 21. TestDeleteCredentialVersionConflict — wrong row_version → platform.ErrConflict
// ---------------------------------------------------------------------------
func TestDeleteCredentialVersionConflict(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	err := s.DeleteCredential(ctxb(), c.ID, 42)
	if err == nil {
		t.Fatal("expected error for version conflict")
	}
	if !errors.Is(err, platform.ErrConflict) {
		t.Fatalf("expected platform.ErrConflict, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 22. TestDeleteCredentialIdempotency — second delete with stale version fails
// ---------------------------------------------------------------------------
func TestDeleteCredentialIdempotency(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	// First delete succeeds
	if err := s.DeleteCredential(ctxb(), c.ID, c.RowVersion); err != nil {
		t.Fatalf("first DeleteCredential: %v", err)
	}
	// Second delete with same version fails (row gone)
	err := s.DeleteCredential(ctxb(), c.ID, c.RowVersion)
	if err == nil {
		t.Fatal("expected error for second delete")
	}
	if !errors.Is(err, platform.ErrConflict) {
		t.Fatalf("expected platform.ErrConflict, got %v", err)
	}
}

// ---------------------------------------------------------------------------
// 23. TestConcurrentUpdateSameRow — one succeeds, one gets ErrConflict
// ---------------------------------------------------------------------------
func TestConcurrentUpdateSameRow(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
	id := c.ID

	// Two goroutines both try to update with the same initial row_version
	errsCh := make(chan error, 2)
	for i := 0; i < 2; i++ {
		go func(n int) {
			cp := &store.Credential{
				ID:           id,
				UserID:       "u1",
				APIBase:      "https://example.com",
				KeyTag:       "default",
				APIKeyCipher: []byte("concurrent-" + itos(n)),
				AuthType:     "openai",
				RowVersion:   1, // both start with the same version
			}
			errsCh <- s.UpdateCredential(ctxb(), id, cp)
		}(i)
	}

	successCount := 0
	conflictCount := 0
	for i := 0; i < 2; i++ {
		err := <-errsCh
		if err == nil {
			successCount++
		} else if errors.Is(err, platform.ErrConflict) {
			conflictCount++
		} else {
			t.Errorf("unexpected error: %v", err)
		}
	}
	if successCount != 1 {
		t.Fatalf("expected exactly 1 success, got %d", successCount)
	}
	if conflictCount != 1 {
		t.Fatalf("expected exactly 1 conflict, got %d", conflictCount)
	}
}

// ---------------------------------------------------------------------------
// 24. TestUpdateCredentialNil — nil credential returns error
// ---------------------------------------------------------------------------
func TestUpdateCredentialNil(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	err := s.UpdateCredential(ctxb(), 1, nil)
	if err == nil {
		t.Fatal("expected error for nil credential")
	}
}

// ---------------------------------------------------------------------------
// 25. TestCountStragglersByDekVersion — counts rows below the target version
// ---------------------------------------------------------------------------
func TestCountStragglersByDekVersion(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	for i, dv := range []int64{1, 1, 2} {
		c := newTestCred("u"+itos(i), "https://example.com", "default")
		c.DekVersion = dv
		if err := s.InsertCredential(ctxb(), c); err != nil {
			t.Fatalf("insert %d: %v", i, err)
		}
	}

	n, err := s.CountStragglersByDekVersion(ctxb(), 2)
	if err != nil {
		t.Fatalf("CountStragglersByDekVersion(2): %v", err)
	}
	if n != 2 {
		t.Fatalf("count(2) = %d, want 2 (rows with dek_version < 2)", n)
	}

	n, err = s.CountStragglersByDekVersion(ctxb(), 1)
	if err != nil {
		t.Fatalf("CountStragglersByDekVersion(1): %v", err)
	}
	if n != 0 {
		t.Fatalf("count(1) = %d, want 0 (no row below the lowest version)", n)
	}

	// Once every row is at the target version the count drops to 0.
	if _, err := s.AdminDB().ExecContext(ctxb(), `UPDATE credentials SET dek_version = 3`); err != nil {
		t.Fatalf("bump dek_version to 3: %v", err)
	}
	n, err = s.CountStragglersByDekVersion(ctxb(), 3)
	if err != nil {
		t.Fatalf("CountStragglersByDekVersion(3): %v", err)
	}
	if n != 0 {
		t.Fatalf("count(3) after full migration = %d, want 0", n)
	}
}

// ---------------------------------------------------------------------------
// 26. TestGetCredentialByIDFound — read back by numeric ID
// ---------------------------------------------------------------------------
func TestGetCredentialByIDFound(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	orig := newTestCred("u1", "https://example.com", "default")
	orig.APIKeyCipher = []byte("secret-cipher")
	orig.KekVersion = 3
	orig.DekVersion = 5
	if err := s.InsertCredential(ctxb(), orig); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}

	got, err := s.GetCredentialByID(ctxb(), orig.ID)
	if err != nil {
		t.Fatalf("GetCredentialByID: %v", err)
	}
	if got.ID != orig.ID {
		t.Fatalf("ID: expected %d, got %d", orig.ID, got.ID)
	}
	if got.UserID != "u1" {
		t.Fatalf("UserID: expected u1, got %q", got.UserID)
	}
	if got.APIBase != "https://example.com" {
		t.Fatalf("APIBase: expected https://example.com, got %q", got.APIBase)
	}
	if got.KeyTag != "default" {
		t.Fatalf("KeyTag: expected default, got %q", got.KeyTag)
	}
	if string(got.APIKeyCipher) != "secret-cipher" {
		t.Fatalf("APIKeyCipher: expected secret-cipher, got %q", string(got.APIKeyCipher))
	}
	if got.AuthType != "openai" {
		t.Fatalf("AuthType: expected openai, got %q", got.AuthType)
	}
	if got.RowVersion != 1 {
		t.Fatalf("RowVersion: expected 1, got %d", got.RowVersion)
	}
	if got.KekVersion != 3 {
		t.Fatalf("KekVersion: expected 3, got %d", got.KekVersion)
	}
	if got.DekVersion != 5 {
		t.Fatalf("DekVersion: expected 5, got %d", got.DekVersion)
	}
	if got.CreatedAt != orig.CreatedAt {
		t.Fatalf("CreatedAt: expected %d, got %d", orig.CreatedAt, got.CreatedAt)
	}
	if got.UpdatedAt != orig.UpdatedAt {
		t.Fatalf("UpdatedAt: expected %d, got %d", orig.UpdatedAt, got.UpdatedAt)
	}
}

// ---------------------------------------------------------------------------
// 27. TestGetCredentialByIDNotFound — missing id → platform.ErrNotFound
// ---------------------------------------------------------------------------
func TestGetCredentialByIDNotFound(t *testing.T) {
	s := openMem(t)
	defer s.Close()

	c := newTestCred("u1", "https://example.com", "default")
	if err := s.InsertCredential(ctxb(), c); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}

	_, err := s.GetCredentialByID(ctxb(), c.ID+999)
	if err == nil {
		t.Fatal("expected error for missing id")
	}
	if !errors.Is(err, platform.ErrNotFound) {
		t.Fatalf("expected platform.ErrNotFound, got %v", err)
	}
}
