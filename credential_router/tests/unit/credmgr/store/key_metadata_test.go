//go:build cgo

package store_test

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/platform"
	"credential_router/internal/credmgr/store"
)

func openTempStore(t *testing.T) *store.Store {
	t.Helper()
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "test.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func sampleKeyMetadata() *store.KeyMetadata {
	return &store.KeyMetadata{
		ActiveKekVersion:   1,
		PendingKekVersion:  0,
		ActiveDekVersion:   1,
		PendingDekVersion:  0,
		CryptoMode:         crypto.ModeAES,
		ActiveConfigShard:  []byte("shard-001"),
		PendingConfigShard: []byte{},
		FileShardVersion:   1,
		FileShardRotatedAt: 1000,
		LastRotateAt:       1000,
		WrappedDEK:         make([]byte, 44),
		PendingWrappedDEK:  []byte{},
		DekRotatedAt:       0,
	}
}

func TestInsertKeyMetadataSuccess(t *testing.T) {
	s := openTempStore(t)
	km := sampleKeyMetadata()
	if err := s.InsertKeyMetadata(context.Background(), km); err != nil {
		t.Fatalf("Insert: %v", err)
	}
	got, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got.CryptoMode != crypto.ModeAES {
		t.Error("crypto_mode mismatch")
	}
	if got.ActiveKekVersion != 1 {
		t.Error("active_kek_version mismatch")
	}
}

func TestInsertKeyMetadataDuplicate(t *testing.T) {
	s := openTempStore(t)
	if err := s.InsertKeyMetadata(context.Background(), sampleKeyMetadata()); err != nil {
		t.Fatal(err)
	}
	err := s.InsertKeyMetadata(context.Background(), sampleKeyMetadata())
	if !errors.Is(err, platform.ErrConflict) {
		t.Errorf("got %v, want platform.ErrConflict", err)
	}
}

func TestGetKeyMetadataEmpty(t *testing.T) {
	s := openTempStore(t)
	_, err := s.GetKeyMetadata(context.Background())
	if !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("got %v, want platform.ErrNotFound", err)
	}
}

func TestUpdateKeyMetadataSuccess(t *testing.T) {
	s := openTempStore(t)
	km := sampleKeyMetadata()
	if err := s.InsertKeyMetadata(context.Background(), km); err != nil {
		t.Fatal(err)
	}
	km.CryptoMode = crypto.ModeSM4
	if err := s.UpdateKeyMetadata(context.Background(), km); err != nil {
		t.Fatal(err)
	}
	got, _ := s.GetKeyMetadata(context.Background())
	if got.CryptoMode != crypto.ModeSM4 {
		t.Error("crypto_mode not updated")
	}
}

func TestUpdateKeyMetadataNotFound(t *testing.T) {
	s := openTempStore(t)
	err := s.UpdateKeyMetadata(context.Background(), sampleKeyMetadata())
	if !errors.Is(err, platform.ErrNotFound) {
		t.Errorf("got %v, want platform.ErrNotFound", err)
	}
}

func TestBulkUpdateKekVersion(t *testing.T) {
	s := openTempStore(t)
	ctx := context.Background()

	// Insert 3 credentials with different kek_version
	for i, kev := range []int64{1, 1, 2} {
		c := &store.Credential{
			UserID:       fmt.Sprintf("u%d", i),
			APIBase:      "https://api.example.com",
			KeyTag:       "default",
			APIKeyCipher: []byte("c"),
			AuthType:     "openai",
			KekVersion:   kev,
			DekVersion:   1,
		}
		if err := s.InsertCredential(ctx, c); err != nil {
			t.Fatalf("Insert %d: %v", i, err)
		}
	}

	n, err := s.BulkUpdateKekVersion(ctx, 5)
	if err != nil {
		t.Fatal(err)
	}
	if n != 3 {
		t.Errorf("updated = %d, want 3 (all rows have kek_version<5)", n)
	}

	// Verify all rows now have kek_version=5
	creds, _ := s.ListCredentials(ctx, 0, 0)
	for _, c := range creds {
		if c.KekVersion != 5 {
			t.Errorf("c%d: kek_version=%d, want 5", c.ID, c.KekVersion)
		}
	}
}

func TestUpdateKeyMetadataNil(t *testing.T) {
	s := openTempStore(t)
	if err := s.UpdateKeyMetadata(context.Background(), nil); err == nil {
		t.Error("expected error for nil")
	}
}

// ---------------------------------------------------------------------------
// Gap 8: ReencryptDekVersionBatch — batch limit, TX atomicity, decrypt failure
// ---------------------------------------------------------------------------

// encryptCredentialWithDEK encrypts plaintext with the given DEK (ModeAES).
func encryptCredentialWithDEK(t *testing.T, dek, plaintext []byte) []byte {
	t.Helper()
	ct, err := crypto.EncryptCredential(crypto.ModeAES, dek, plaintext)
	if err != nil {
		t.Fatalf("EncryptCredential: %v", err)
	}
	return ct
}

func testDEK(seed byte) []byte {
	dek := make([]byte, 16)
	for i := range dek {
		dek[i] = seed + byte(i)
	}
	return dek
}

func TestReencryptDekVersionBatchBatchesByLimit(t *testing.T) {
	s := openTempStore(t)
	ctx := context.Background()

	oldDEK := testDEK(0x10)
	newDEK := testDEK(0x50)

	const total = 25
	const limit = 10
	for i := 0; i < total; i++ {
		ct := encryptCredentialWithDEK(t, oldDEK, []byte(fmt.Sprintf("plain-%02d", i)))
		c := &store.Credential{
			UserID:       fmt.Sprintf("u%02d", i),
			APIBase:      "https://api.example.com",
			KeyTag:       "default",
			APIKeyCipher: ct,
			AuthType:     "openai",
			KekVersion:   1,
			DekVersion:   1,
		}
		if err := s.InsertCredential(ctx, c); err != nil {
			t.Fatalf("insert %d: %v", i, err)
		}
	}

	n, err := s.ReencryptDekVersionBatch(ctx, oldDEK, newDEK, crypto.ModeAES, 2, limit)
	if err != nil {
		t.Fatalf("ReencryptDekVersionBatch: %v", err)
	}
	if n != limit {
		t.Fatalf("reencrypted = %d, want %d (one batch at the limit)", n, limit)
	}

	creds, err := s.ListCredentials(ctx, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(creds) != total {
		t.Fatalf("listed %d rows, want %d", len(creds), total)
	}
	// ListCredentials orders by id, which equals insertion order here.
	for i, c := range creds {
		wantDEK, wantVersion := oldDEK, int64(1)
		if i < limit {
			wantDEK, wantVersion = newDEK, 2
		}
		if c.DekVersion != wantVersion {
			t.Errorf("row %d (id=%d): dek_version = %d, want %d", i, c.ID, c.DekVersion, wantVersion)
			continue
		}
		pt, err := crypto.DecryptCredential(crypto.ModeAES, wantDEK, c.APIKeyCipher)
		if err != nil {
			t.Errorf("row %d (id=%d): decrypt under batch DEK: %v", i, c.ID, err)
			continue
		}
		if want := fmt.Sprintf("plain-%02d", i); string(pt) != want {
			t.Errorf("row %d (id=%d): plaintext = %q, want %q", i, c.ID, pt, want)
		}
	}
}

func TestReencryptDekVersionBatchDecryptFailRollsBack(t *testing.T) {
	s := openTempStore(t)
	ctx := context.Background()

	oldDEK := testDEK(0x10)
	newDEK := testDEK(0x50)

	const total = 5
	for i := 0; i < total; i++ {
		ct := encryptCredentialWithDEK(t, oldDEK, []byte(fmt.Sprintf("plain-%02d", i)))
		c := &store.Credential{
			UserID:       fmt.Sprintf("u%02d", i),
			APIBase:      "https://api.example.com",
			KeyTag:       "default",
			APIKeyCipher: ct,
			AuthType:     "openai",
			KekVersion:   1,
			DekVersion:   1,
		}
		if err := s.InsertCredential(ctx, c); err != nil {
			t.Fatalf("insert %d: %v", i, err)
		}
	}

	// Corrupt the ciphertext of the lowest-id row, which comes first in the
	// batch (ORDER BY id); AEAD auth must fail during Phase A decrypt.
	if _, err := s.AdminDB().ExecContext(ctx,
		`UPDATE credentials SET api_key_cipher = ? WHERE user_id = ?`,
		bytes.Repeat([]byte{0xFF}, 64), "u00"); err != nil {
		t.Fatalf("corrupt row ciphertext: %v", err)
	}

	_, err := s.ReencryptDekVersionBatch(ctx, oldDEK, newDEK, crypto.ModeAES, 2, 10)
	if !errors.Is(err, store.ErrPhaseADecryptFail) {
		t.Fatalf("got %v, want store.ErrPhaseADecryptFail", err)
	}

	creds, err := s.ListCredentials(ctx, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(creds) != total {
		t.Fatalf("listed %d rows, want %d", len(creds), total)
	}
	for i, c := range creds {
		if c.DekVersion != 1 {
			t.Errorf("row %d (id=%d): dek_version = %d, want 1 (rollback)", i, c.ID, c.DekVersion)
		}
	}
	// Uncorrupted rows must still decrypt with the old DEK.
	pt, err := crypto.DecryptCredential(crypto.ModeAES, oldDEK, creds[1].APIKeyCipher)
	if err != nil {
		t.Fatalf("row 1 ciphertext damaged by failed batch: %v", err)
	}
	if string(pt) != "plain-01" {
		t.Errorf("row 1 plaintext = %q, want %q", pt, "plain-01")
	}
}

// TestReencryptDekVersionBatchConcurrentAdminUpdate pins the Phase A
// row_version guard: a competing admin UPDATE on the same row between
// Phase A's SELECT and UPDATE must keep its ciphertext, and Phase A's
// UPDATE must not advance the row to dek_version=2.
//
// The competing UPDATE runs on a fresh sqlite connection so it can
// interleave with the in-flight Phase A transaction — the same path a
// real admin request takes when AdminMaxConns > 1. Phase A's UPDATE
// either lands 0 rows (row_version guard) or its row is already at
// dek_version=2 — both states are acceptable; what matters is that the
// admin ciphertext survives.
func TestReencryptDekVersionBatchConcurrentAdminUpdate(t *testing.T) {
	s := openTempStore(t)
	ctx := context.Background()

	oldDEK := testDEK(0x10)
	newDEK := testDEK(0x60)

	ct := encryptCredentialWithDEK(t, oldDEK, []byte("plain-00"))
	seed := &store.Credential{
		UserID:       "u00",
		APIBase:      "https://api.example.com",
		KeyTag:       "default",
		APIKeyCipher: ct,
		AuthType:     "openai",
		KekVersion:   1,
		DekVersion:   1,
	}
	if err := s.InsertCredential(ctx, seed); err != nil {
		t.Fatalf("insert seed: %v", err)
	}
	rowID := seed.ID
	rowVer := seed.RowVersion

	adminCipher := encryptCredentialWithDEK(t, oldDEK, []byte("admin-wins"))

	phaseASelected := make(chan struct{})
	adminDone := make(chan struct{})
	restore := store.SetReencryptPhaseHookForTesting(func(_ context.Context) {
		close(phaseASelected)
		<-adminDone
	})
	defer restore()

	type result struct {
		n   int64
		err error
	}
	phaseAResult := make(chan result, 1)
	go func() {
		n, err := s.ReencryptDekVersionBatch(ctx, oldDEK, newDEK, crypto.ModeAES, 2, 10)
		phaseAResult <- result{n: n, err: err}
	}()

	<-phaseASelected

	adminConn, err := sql.Open("sqlite3", s.Path()+"?_busy_timeout=5000")
	if err != nil {
		close(adminDone)
		t.Fatalf("open admin conn: %v", err)
	}
	defer adminConn.Close()
	updRes, err := adminConn.ExecContext(ctx,
		`UPDATE credentials
		   SET api_key_cipher=?, auth_type=?, row_version=row_version+1, updated_at=?
		 WHERE id=? AND row_version=?`,
		adminCipher, "openai", time.Now().Unix(), rowID, rowVer,
	)
	if err != nil {
		close(adminDone)
		t.Fatalf("admin concurrent UPDATE: %v", err)
	}
	aff, err := updRes.RowsAffected()
	if err != nil {
		close(adminDone)
		t.Fatalf("admin RowsAffected: %v", err)
	}
	if aff != 1 {
		close(adminDone)
		t.Fatalf("admin concurrent UPDATE: rows affected = %d, want 1", aff)
	}
	close(adminDone)

	// Phase A's UPDATE may either succeed (row_version guard hits 0 rows
	// silently) or report a transient database lock if SQLite writer
	// ordering is unlucky. What matters is end-state: admin ciphertext
	// preserved, dek_version not advanced by Phase A.
	<-phaseAResult

	got, err := s.GetCredentialByID(ctx, rowID)
	if err != nil {
		t.Fatalf("GetCredentialByID: %v", err)
	}
	if !bytes.Equal(got.APIKeyCipher, adminCipher) {
		t.Errorf("row ciphertext overwritten by Phase A: got %x, want admin write %x",
			got.APIKeyCipher, adminCipher)
	}
	if got.DekVersion != 1 {
		t.Errorf("dek_version = %d, want 1 (Phase A must not advance; admin write was the winner)", got.DekVersion)
	}
	if got.RowVersion != rowVer+1 {
		t.Errorf("row_version = %d, want %d (only the admin UPDATE should advance it)",
			got.RowVersion, rowVer+1)
	}
}
