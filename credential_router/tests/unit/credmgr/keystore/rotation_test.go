//go:build cgo

package keystore_test

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"credential_router/internal/credmgr/backup"
	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
	_ "github.com/mattn/go-sqlite3"
)

// setupTestRotator builds a full keystore + store + backup manager.
func setupTestRotator(t *testing.T) (*keystore.Rotator, *keystore.Manager, *store.Store, string, func()) {
	t.Helper()

	secretsDir := t.TempDir()
	dataDir := t.TempDir()
	backupDir := filepath.Join(dataDir, "backups")

	var s1 [32]byte
	for i := range s1 {
		s1[i] = byte(i)
	}

	for _, f := range []struct {
		name string
		data []byte
	}{
		{"s1.bin.1", s1[:]},
	} {
		if err := os.WriteFile(filepath.Join(secretsDir, f.name), f.data, 0o600); err != nil {
			t.Fatalf("write %s: %v", f.name, err)
		}
	}
	if err := keystore.WriteCryptoModeFile(filepath.Join(secretsDir, "crypto_mode"), crypto.ModeAES); err != nil {
		t.Fatal(err)
	}

	dbPath := filepath.Join(dataDir, "creds.db")
	s, err := store.OpenForTesting(dbPath)
	if err != nil {
		t.Fatal(err)
	}

	mgr, err := keystore.SelfInit(context.Background(), keystore.SelfInitParams{SecretsDir: secretsDir}, s)
	if err != nil {
		t.Fatal(err)
	}

	bm, err := backup.NewBackupManager(backup.BackupConfig{
		BackupDir: backupDir,
		KeySnapshot: backup.KeySnapshotConfig{
			Keep: 5,
		},
	}, s)
	if err != nil {
		t.Fatal(err)
	}

	rot := keystore.NewRotator(mgr, s, bm, secretsDir)
	return rot, mgr, s, secretsDir, func() { _ = s.Close() }
}


func TestBeginKEKRotationSuccess(t *testing.T) {
	rot, mgr, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	origSnap := mgr.Current()
	origKEKVersion := int64(origSnap.KekVersion)


	ctx := context.Background()
	if err := rot.BeginKEKRotation(ctx); err != nil {
		t.Fatalf("BeginKEKRotation: %v", err)
	}

	newSnap := mgr.Current()
	if int64(newSnap.KekVersion) != origKEKVersion+1 {
		t.Errorf("new KekVersion=%d, want %d", newSnap.KekVersion, origKEKVersion+1)
	}

	prevSnap := mgr.Previous()
	if prevSnap == nil {
		t.Fatal("previous snapshot should be set after swap")
	}
	if int64(prevSnap.KekVersion) != origKEKVersion {
		t.Errorf("previous KekVersion=%d, want %d", prevSnap.KekVersion, origKEKVersion)
	}

	if newSnap.DekVersion != origSnap.DekVersion {
		t.Errorf("DEK version changed: was %d, now %d", origSnap.DekVersion, newSnap.DekVersion)
	}

	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != origKEKVersion+1 {
		t.Errorf("DB PendingKekVersion=%d, want %d", km.PendingKekVersion, origKEKVersion+1)
	}
	if km.FileShardVersion != int64(2) {
		t.Errorf("DB FileShardVersion=%d, want %d", km.FileShardVersion, int64(2))
	}
}

func TestBeginKEKRotationWhileInProgress(t *testing.T) {
	rot, _, _, _, cleanup := setupTestRotator(t)
	defer cleanup()

	unlock := rot.LockRotationForTesting()
	defer unlock()

	err := rot.BeginKEKRotation(context.Background())
	if err != keystore.ErrRotationInProgress {
		t.Errorf("got %v, want ErrRotationInProgress", err)
	}
}

func TestBeginKEKRotationPreservesDEK(t *testing.T) {
	rot, mgr, _, _, cleanup := setupTestRotator(t)
	defer cleanup()

	origSnap := mgr.Current()
	origDEK := origSnap.DEK.Bytes()

	if err := rot.BeginKEKRotation(context.Background()); err != nil {
		t.Fatal(err)
	}

	newSnap := mgr.Current()
	newDEKBytes := newSnap.DEK.Bytes()
	if len(newDEKBytes) != len(origDEK) {
		t.Fatalf("DEK length changed: %d -> %d", len(origDEK), len(newDEKBytes))
	}
	for i, b := range origDEK {
		if newDEKBytes[i] != b {
			t.Errorf("DEK[%d]: was %d, now %d", i, b, newDEKBytes[i])
		}
	}
}
func insertTestCred(t *testing.T, s *store.Store) {
	t.Helper()
	cred := &store.Credential{
		UserID: "u1", APIBase: "https://api.example.com", KeyTag: "default",
		APIKeyCipher: []byte("encrypted"), AuthType: "openai",
		KekVersion: 1, DekVersion: 1,
	}
	if err := s.InsertCredential(context.Background(), cred); err != nil {
		t.Fatalf("InsertCredential: %v", err)
	}
}

func TestCompleteKEKRotationSuccess(t *testing.T) {
	rot, mgr, s, _, cleanup := setupTestRotator(t)
	defer cleanup()
	insertTestCred(t, s)

	ctx := context.Background()
	if err := rot.BeginKEKRotation(ctx); err != nil {
		t.Fatalf("Begin: %v", err)
	}

	if err := rot.CompleteKEKRotation(ctx, 5*time.Second); err != nil {
		t.Fatalf("Complete: %v", err)
	}

	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0", km.PendingKekVersion)
	}
	if km.ActiveKekVersion != 2 {
		t.Errorf("ActiveKekVersion=%d, want 2", km.ActiveKekVersion)
	}

	if mgr.Previous() != nil {
		t.Error("Previous should be nil after Phase B")
	}

	creds, err := s.ListCredentials(ctx, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(creds) == 0 {
		t.Fatal("expected at least 1 credential")
	}
	for _, c := range creds {
		if c.KekVersion != 2 {
			t.Errorf("cred kek_version=%d, want 2", c.KekVersion)
		}
	}
}

func TestCompleteKEKRotationNoPending(t *testing.T) {
	rot, _, _, _, cleanup := setupTestRotator(t)
	defer cleanup()

	err := rot.CompleteKEKRotation(context.Background(), 5*time.Second)
	if err == nil {
		t.Error("expected error when no pending rotation")
	}
}

func TestCompleteKEKRotationWhileInProgress(t *testing.T) {
	rot, _, _, _, cleanup := setupTestRotator(t)
	defer cleanup()

	unlock := rot.LockRotationForTesting()
	defer unlock()

	err := rot.CompleteKEKRotation(context.Background(), 5*time.Second)
	if err != keystore.ErrRotationInProgress {
		t.Errorf("got %v, want ErrRotationInProgress", err)
	}
}

func TestPhaseAReencryptAll(t *testing.T) {
	rot, mgr, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()

	// Insert 3 credentials encrypted with the original DEK
	origSnap := mgr.Current()
	origKEK := origSnap.KEK.Bytes()
	origDEK := origSnap.DEK.Bytes()

	for i := 0; i < 3; i++ {
		ct, err := encryptPlaintext(origKEK, origDEK, []byte(fmt.Sprintf("plain-%d", i)))
		if err != nil {
			t.Fatal(err)
		}
		cred := &store.Credential{
			UserID: fmt.Sprintf("u%d", i), APIBase: "https://api.example.com", KeyTag: "default",
			APIKeyCipher: ct, AuthType: "openai",
			KekVersion: 1, DekVersion: 1,
		}
		if err := s.InsertCredential(ctx, cred); err != nil {
			t.Fatalf("Insert %d: %v", i, err)
		}
	}

	// Drive KEK→DEK rotation manually: BeginKEKRotation flips kek_version,
	// then we swap current's DEK below to simulate Phase A having completed.
	if err := rot.BeginKEKRotation(ctx); err != nil {
		t.Fatalf("Begin: %v", err)
	}
	// Now manually rotate DEK: swap current's DEK to a new random DEK
	newDEK := make([]byte, 16)
	for i := range newDEK {
		newDEK[i] = byte(i + 50)
	}
	cur := mgr.Current()
	cur.DEK = crypto.NewKeyBytes(newDEK)

	// Set up pending DEK version in key_metadata manually for Phase A
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingDekVersion = 2
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	// Run Phase A
	total, loops, err := rot.PhaseA_ReencryptAll(ctx, 2, 100, 100)
	if err != nil {
		t.Fatalf("PhaseA: %v", err)
	}
	if total != 3 {
		t.Errorf("reencrypted=%d, want 3", total)
	}
	if loops < 1 {
		t.Errorf("loops=%d, want ≥1", loops)
	}

	// Verify all rows now have dek_version=2
	creds, _ := s.ListCredentials(ctx, 0, 0)
	for _, c := range creds {
		if c.DekVersion != 2 {
			t.Errorf("cred dek_version=%d, want 2", c.DekVersion)
		}
	}
}

func encryptPlaintext(kek, dek []byte, plaintext []byte) ([]byte, error) {
	return crypto.EncryptCredential(crypto.ModeAES, dek, plaintext)
}

func TestBeginDEKRotationSuccess(t *testing.T) {
	rot, mgr, _, _, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()
	origSnap := mgr.Current()
	origDekVer := origSnap.DekVersion

	if err := rot.BeginDEKRotation(ctx); err != nil {
		t.Fatalf("BeginDEK: %v", err)
	}

	newSnap := mgr.Current()
	if newSnap.DekVersion != origDekVer+1 {
		t.Errorf("DekVersion=%d, want %d", newSnap.DekVersion, origDekVer+1)
	}
	if mgr.Previous() == nil {
		t.Error("Previous should be set after swap")
	}
	// KEK should be unchanged
	if newSnap.KekVersion != origSnap.KekVersion {
		t.Errorf("KekVersion changed: was %d, now %d", origSnap.KekVersion, newSnap.KekVersion)
	}
}

func TestBeginDEKRotationWhileInProgress(t *testing.T) {
	rot, _, _, _, cleanup := setupTestRotator(t)
	defer cleanup()
	unlock := rot.LockRotationForTesting()
	defer unlock()

	err := rot.BeginDEKRotation(context.Background())
	if err != keystore.ErrRotationInProgress {
		t.Errorf("got %v, want ErrRotationInProgress", err)
	}
}

// TestCrossTypeRotationRejected is the rotator-level twin of the admin 409
// test: with a pending KEK rotation, BeginDEKRotation must
// fail with ErrRotationInProgress, and with a pending DEK rotation
// BeginKEKRotation must fail the same way.
func TestCrossTypeRotationRejected(t *testing.T) {
	rot, _, s, _, cleanup := setupTestRotator(t)
	defer cleanup()
	ctx := context.Background()

	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = km.ActiveKekVersion + 1
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	if err := rot.BeginDEKRotation(ctx); !errors.Is(err, keystore.ErrRotationInProgress) {
		t.Errorf("BeginDEKRotation with KEK pending: got %v, want ErrRotationInProgress", err)
	}

	km, err = s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = 0
	km.PendingDekVersion = km.ActiveDekVersion + 1
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	if err := rot.BeginKEKRotation(ctx); !errors.Is(err, keystore.ErrRotationInProgress) {
		t.Errorf("BeginKEKRotation with DEK pending: got %v, want ErrRotationInProgress", err)
	}
}

// TestAutoRotateFiresImmediatelyAfterStart verifies that on a fresh install
// (dek_rotated_at=0, gate always satisfied) the auto-rotate
// ticker triggers DEK rotation within the first period window.
func TestAutoRotateFiresImmediatelyAfterStart(t *testing.T) {
	rot, mgr, _, _, cleanup := setupTestRotator(t)
	defer cleanup()

	origSnap := mgr.Current()
	origDekVer := origSnap.DekVersion

	stop := rot.StartAutoRotate(context.Background(),
		100*time.Millisecond, 5*time.Second, 1000, 100)
	defer stop()

	deadline := time.Now().Add(500 * time.Millisecond)
	for time.Now().Before(deadline) {
		if mgr.Current().DekVersion > origDekVer {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}

	t.Errorf("DEK did not rotate within 500ms; DekVersion=%d (was %d)",
		mgr.Current().DekVersion, origDekVer)
}

// TestRunAutoRotateGateSkipsOnFreshInstall verifies that runAutoRotate on a
// fresh install is a silent no-op: SelfInit seeds dek_rotated_at=now, so
// time.Since(dek_rotated_at) < period and the gate blocks. Rotating a DEK
// that was just generated has zero security benefit (no ciphertext exposure
// to bound) and only costs Phase A + a metadata write.
func TestRunAutoRotateGateSkipsOnFreshInstall(t *testing.T) {
	rot, mgr, _, _, cleanup := setupTestRotator(t)
	defer cleanup()

	origDekVer := mgr.Current().DekVersion
	ctx := context.Background()

	if err := rot.RunAutoRotateForTesting(ctx, 1*time.Hour, 5*time.Second, 1000, 100); err != nil {
		t.Fatalf("runAutoRotate: %v", err)
	}
	if mgr.Current().DekVersion != origDekVer {
		t.Errorf("DekVersion=%d, want %d (gate should skip on fresh install)",
			mgr.Current().DekVersion, origDekVer)
	}
}

func TestBeginKEKRotationBackupCapturesPrePendingState(t *testing.T) {
	rot, _, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	if err := rot.BeginKEKRotation(context.Background()); err != nil {
		t.Fatalf("BeginKEKRotation: %v", err)
	}

	backupDir := filepath.Join(filepath.Dir(s.Path()), "backups")
	entries, err := os.ReadDir(backupDir)
	if err != nil {
		t.Fatalf("read backup dir: %v", err)
	}
	var newest string
	var newestMod time.Time
	for _, e := range entries {
		if filepath.Ext(e.Name()) != ".db" {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		if info.ModTime().After(newestMod) {
			newestMod = info.ModTime()
			newest = filepath.Join(backupDir, e.Name())
		}
	}
	if newest == "" {
		t.Fatal("no backup .db file found")
	}

	backupDB, err := sql.Open("sqlite3", newest+"?mode=ro")
	if err != nil {
		t.Fatalf("open backup db: %v", err)
	}
	defer backupDB.Close()

	var pendingKEK int64
	if err := backupDB.QueryRow("SELECT pending_kek_version FROM key_metadata WHERE id=1").Scan(&pendingKEK); err != nil {
		t.Fatalf("query backup: %v", err)
	}
	if pendingKEK != 0 {
		t.Errorf("backup DB has pending_kek_version=%d, want 0 (backup must run before UpdateKeyMetadata)", pendingKEK)
	}
}

// TestBeginS2RotationBackupBeforeUpdate: a backup produced
// during BeginS2Rotation must capture the DB state BEFORE the new
// pending_kek_version is written, so the on-disk copy is a valid recovery
// anchor if the TX commit succeeds but the process crashes before any
// subsequent backup is taken.
func TestBeginS2RotationBackupBeforeUpdate(t *testing.T) {
	rot, _, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	s2hex := "0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20"
	if err := rot.BeginS2Rotation(context.Background(), s2hex); err != nil {
		t.Fatalf("BeginS2Rotation: %v", err)
	}

	backupDir := filepath.Join(filepath.Dir(s.Path()), "backups")
	entries, err := os.ReadDir(backupDir)
	if err != nil {
		t.Fatalf("read backup dir: %v", err)
	}
	var newest string
	var newestMod time.Time
	for _, e := range entries {
		if filepath.Ext(e.Name()) != ".db" {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		if info.ModTime().After(newestMod) {
			newestMod = info.ModTime()
			newest = filepath.Join(backupDir, e.Name())
		}
	}
	if newest == "" {
		t.Fatal("no backup .db file found")
	}

	backupDB, err := sql.Open("sqlite3", newest+"?mode=ro")
	if err != nil {
		t.Fatalf("open backup db: %v", err)
	}
	defer backupDB.Close()

	var pendingKEK int64
	if err := backupDB.QueryRow("SELECT pending_kek_version FROM key_metadata WHERE id=1").Scan(&pendingKEK); err != nil {
		t.Fatalf("query backup: %v", err)
	}
	if pendingKEK != 0 {
		t.Errorf("backup DB has pending_kek_version=%d, want 0 (backup must run before UpdateKeyMetadata)", pendingKEK)
	}
}

// TestRunAutoRotateSkipsWhenGateNotSatisfied (branch a):
// when time.Since(dek_rotated_at) < Period the gate is not satisfied and
// runAutoRotate must be a no-op — no error, no rotation, no pending state.
func TestRunAutoRotateSkipsWhenGateNotSatisfied(t *testing.T) {
	rot, mgr, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	// SelfInit seeds dek_rotated_at=now; set it explicitly to guard against
	// any future setupTestRotator change that resets it to 0 (the gate check
	// below assumes a non-zero value within the 1h period).
	km.DekRotatedAt = time.Now().Unix()
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	// No Rotation struct exists; the period is a runAutoRotate parameter.
	if err := rot.RunAutoRotateForTesting(ctx, time.Hour, 5*time.Second, 1000, 100); err != nil {
		t.Fatalf("runAutoRotate: %v (gate miss must be a silent no-op, not an error)", err)
	}

	if got := mgr.Current().DekVersion; got != 1 {
		t.Errorf("manager DekVersion=%d, want 1 (gate miss must not rotate)", got)
	}
	km2, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km2.ActiveDekVersion != 1 {
		t.Errorf("ActiveDekVersion=%d, want 1", km2.ActiveDekVersion)
	}
	if km2.PendingDekVersion != 0 {
		t.Errorf("PendingDekVersion=%d, want 0", km2.PendingDekVersion)
	}
	if km2.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0", km2.PendingKekVersion)
	}
}

// TestRunAutoRotateResumesPendingKEK (branch b): when a KEK rotation is
// left pending, runAutoRotate must auto-complete it so the DB converges
// to a consistent state, not stabilise in a half-rotated state.
func TestRunAutoRotateResumesPendingKEK(t *testing.T) {
	rot, _, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = 2 // simulate a previous tick's interrupted KEK rotation
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	if err := rot.RunAutoRotateForTesting(ctx, time.Hour, 5*time.Second, 1000, 100); err != nil {
		t.Fatalf("runAutoRotate: %v", err)
	}

	km2, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km2.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0 (pending KEK must be auto-completed)", km2.PendingKekVersion)
	}
	if km2.ActiveKekVersion != 2 {
		t.Errorf("ActiveKekVersion=%d, want 2 (KEK rotation promoted)", km2.ActiveKekVersion)
	}
	if km2.PendingDekVersion != 0 {
		t.Errorf("PendingDekVersion=%d, want 0", km2.PendingDekVersion)
	}
	if km2.ActiveDekVersion != 1 {
		t.Errorf("ActiveDekVersion=%d, want 1", km2.ActiveDekVersion)
	}
}

// TestBeginKEKRotationBackupFailureRefuses:
// when the pre-rotation backup fails, BeginKEKRotation must refuse the
// rotation and leave the old state fully intact (pending_kek_version stays 0,
// manager never swaps).
//
// The keystore layer returns a generic wrapped error ("keystore: backup: …")
// with no exported sentinel, so the test asserts the message prefix rather
// than errors.Is. errs.CodeBackupFailed exists at the errs layer but is not
// referenced by the admin handlers, which surface rotation errors via raw
// respondError.
func TestBeginKEKRotationBackupFailureRefuses(t *testing.T) {
	rot, mgr, s, secretsDir, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()

	// Point a second backup manager at an unwritable path: an intermediate
	// path component is a regular FILE, so os.MkdirAll fails with ENOTDIR
	// regardless of uid (a 0o000 dir would be bypassed when running as root).
	blocker := filepath.Join(t.TempDir(), "blocker")
	if err := os.WriteFile(blocker, []byte("not a dir"), 0o600); err != nil {
		t.Fatal(err)
	}
	badBM, err := backup.NewBackupManager(backup.BackupConfig{
		BackupDir: filepath.Join(blocker, "backups"),
	}, s)
	if err != nil {
		t.Fatal(err)
	}
	badRot := keystore.NewRotator(mgr, s, badBM, secretsDir)

	origKEKVersion := mgr.Current().KekVersion
	kmBefore, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}

	err = badRot.BeginKEKRotation(ctx)
	if err == nil {
		t.Fatal("BeginKEKRotation: expected error when backup fails")
	}
	if !strings.Contains(err.Error(), "backup") {
		t.Errorf("error %q does not mention the backup step", err)
	}

	// Old state preserved: rotation never started (UpdateKeyMetadata runs
	// after the backup in BeginKEKRotation).
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0 (rotation must not start on backup failure)", km.PendingKekVersion)
	}
	if km.PendingDekVersion != 0 {
		t.Errorf("PendingDekVersion=%d, want 0", km.PendingDekVersion)
	}
	if len(km.PendingWrappedDEK) != 0 {
		t.Error("PendingWrappedDEK must be empty after refused rotation")
	}
	if km.FileShardVersion != kmBefore.FileShardVersion {
		t.Errorf("FileShardVersion changed: %d -> %d", kmBefore.FileShardVersion, km.FileShardVersion)
	}
	if got := mgr.Current().KekVersion; got != origKEKVersion {
		t.Errorf("manager KekVersion=%d, want %d (manager must not swap)", got, origKEKVersion)
	}
	if mgr.Previous() != nil {
		t.Error("Previous snapshot must remain nil after refused rotation")
	}

	// The preserved state is a valid pre-rotation state: a retry with a
	// healthy backup manager must succeed.
	if err := rot.BeginKEKRotation(ctx); err != nil {
		t.Fatalf("retry with healthy backup manager: %v", err)
	}
}

// TestPhaseADecryptFailureAbortsRotation:
// a row whose ciphertext fails AEAD decrypt during Phase A is FATAL
// (phase_a_decrypt_failed) — the rotation must abort and no row's dek_version
// may advance (the batch TX rolls back). PhaseA_ReencryptAll returns a
// wrapped store.ErrPhaseADecryptFail; callers (main.go, admin handler) map
// this to exit 78 / HTTP 500. In-process error propagation of a non-fatal
// Phase A failure is covered separately by
// TestCompleteDEKRotationPropagatesPhaseAError.
func TestPhaseADecryptFailureAbortsRotation(t *testing.T) {
	rot, mgr, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()

	origDEK := mgr.Current().DEK.Bytes()
	for i := 0; i < 3; i++ {
		ct, err := crypto.EncryptCredential(crypto.ModeAES, origDEK, []byte(fmt.Sprintf("plain-%d", i)))
		if err != nil {
			t.Fatal(err)
		}
		cred := &store.Credential{
			UserID:       fmt.Sprintf("u%d", i),
			APIBase:      "https://api.example.com",
			KeyTag:       "default",
			APIKeyCipher: ct,
			AuthType:     "openai",
			KekVersion:   1,
			DekVersion:   1,
		}
		if err := s.InsertCredential(ctx, cred); err != nil {
			t.Fatalf("Insert %d: %v", i, err)
		}
	}

	if err := rot.BeginDEKRotation(ctx); err != nil {
		t.Fatal(err)
	}

	// Corrupt the middle row's ciphertext in place. The blob layout is
	// [format(1) || nonce(12) || ct+tag], so flipping a byte at len/2 lands
	// in the AEAD payload/tag and makes DecryptCredential fail.
	creds, err := s.ListCredentials(ctx, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(creds) != 3 {
		t.Fatalf("creds=%d, want 3", len(creds))
	}
	corrupt := creds[1]
	bad := append([]byte(nil), corrupt.APIKeyCipher...)
	bad[len(bad)/2] ^= 0xff
	if _, err := s.AdminDB().ExecContext(ctx,
		`UPDATE credentials SET api_key_cipher=? WHERE id=?`, bad, corrupt.ID); err != nil {
		t.Fatalf("corrupt row: %v", err)
	}

	err = rot.CompleteDEKRotation(ctx, 5*time.Second, 100, 100)
	if err == nil {
		t.Fatal("CompleteDEKRotation: expected ErrPhaseADecryptFail, got nil")
	}
	if !errors.Is(err, store.ErrPhaseADecryptFail) {
		t.Errorf("err=%v, want errors.Is(err, store.ErrPhaseADecryptFail)", err)
	}

	// On-disk state after the abort: rotation never committed.
	for _, c := range creds {
		if c.DekVersion != 1 {
			t.Errorf("cred %d dek_version=%d, want 1 (Phase A TX must roll back; rotation aborted)", c.ID, c.DekVersion)
		}
	}
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingDekVersion != 2 {
		t.Errorf("PendingDekVersion=%d, want 2 (rotation must stay pending, never promoted)", km.PendingDekVersion)
	}
	if km.ActiveDekVersion != 1 {
		t.Errorf("ActiveDekVersion=%d, want 1", km.ActiveDekVersion)
	}
}

// TestCompleteDEKRotationPropagatesPhaseAError covers the non-fatal arm of
// "a Phase A failure on the first iteration must propagate out of
// CompleteDEKRotation rather than being swallowed or continuing": a batch
// error (limit<=0) surfaces as an error, and the rotation stays pending.
func TestCompleteDEKRotationPropagatesPhaseAError(t *testing.T) {
	rot, _, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()
	if err := rot.BeginDEKRotation(ctx); err != nil {
		t.Fatal(err)
	}

	// maxRowsPerTx=0 makes ReencryptDekVersionBatch fail immediately on the
	// first batch ("store: limit must be positive") — a non-fatal Phase A
	// error that must propagate through runDEKPhaseAConvergence out of
	// CompleteDEKRotation.
	err := rot.CompleteDEKRotation(ctx, time.Second, 0, 100)
	if err == nil {
		t.Fatal("CompleteDEKRotation: expected Phase A error to propagate")
	}
	if !strings.Contains(err.Error(), "Phase A") {
		t.Errorf("error %q does not identify the Phase A failure", err)
	}

	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingDekVersion != 2 {
		t.Errorf("PendingDekVersion=%d, want 2 (rotation must stay pending)", km.PendingDekVersion)
	}
	if km.ActiveDekVersion != 1 {
		t.Errorf("ActiveDekVersion=%d, want 1", km.ActiveDekVersion)
	}
}
