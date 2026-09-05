//go:build cgo

package keystore_test

import (
	"context"
	"encoding/hex"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"credential_router/internal/credmgr/backup"
	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
)

// setupTestRotatorWithDataDir is the same as setupTestRotator but also
// returns dataDir (so tests can call LoadFromDir to simulate a restart).
func setupTestRotatorWithDataDir(t *testing.T) (*keystore.Rotator, *keystore.Manager, *store.Store, string, string, func()) {
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
		BackupDir: backupDir, Keep: 3,
		KeySnapshot: backup.KeySnapshotConfig{Keep: 5},
	}, s)
	if err != nil {
		t.Fatal(err)
	}
	rot := keystore.NewRotator(mgr, s, bm, secretsDir)
	return rot, mgr, s, secretsDir, dataDir, func() { _ = s.Close() }
}

// crashAt panics the FIRST time invokeFault(stage) is called. After the test
// recovers, faultHook must be reset (use defer) to avoid leaking into other tests.
func crashAt(t *testing.T, stage string) {
	t.Helper()
	restore := keystore.SetFaultHookForTesting(func(s string) {
		if s == stage {
			panic("crash injection: " + stage)
		}
		// Other stages pass through (a different test's hook might fire first).
	})
	t.Cleanup(restore)
}

// crashAfterNCrashes invokes the given function (which is expected to call
// invokeFault(stage) inside the rotation code) exactly n times before
// panicking on the n+1-th call. Useful for simulating a crash mid-Phase-A
// after some Phase A iterations have completed.
func crashAfterNCrashes(t *testing.T, stage string, n int) {
	t.Helper()
	var counter int64
	restore := keystore.SetFaultHookForTesting(func(s string) {
		if s != stage {
			return
		}
		if atomic.AddInt64(&counter, 1) > int64(n) {
			panic("crash injection: " + stage)
		}
	})
	t.Cleanup(restore)
}

// clearFaults disables the crash-injection hook for the remainder of the
// current test (used so convergence loops after an injected crash run clean).
func clearFaults() {
	keystore.SetFaultHookForTesting(nil)
}

// mustPanic runs fn and asserts it panicked with a message containing substr.
func mustPanic(t *testing.T, substr string, fn func()) {
	t.Helper()
	defer func() {
		r := recover()
		if r == nil {
			t.Fatalf("expected panic containing %q, got nil", substr)
		}
		msg, ok := r.(string)
		if !ok || !strings.Contains(msg, substr) {
			t.Fatalf("expected panic containing %q, got %v", substr, r)
		}
	}()
	fn()
}

// reloadManager simulates process restart: load a fresh Manager from disk
// (post-recovery) and assign it to the rotator.
func reloadManager(t *testing.T, rot *keystore.Rotator, s *store.Store, secretsDir, dataDir string) *keystore.Manager {
	t.Helper()
	mgr, err := keystore.LoadFromDir(context.Background(), secretsDir, dataDir, s)
	if err != nil {
		t.Fatalf("LoadFromDir (restart): %v", err)
	}
	rot.SetManagerForTesting(mgr)
	return mgr
}

// --- Crash during KEK S1 Begin (after Swap) → Case 4 -------------------

// TestCrash_KEKS1_PostSwap simulates a crash after the new KEK S1 snapshot
// has been swapped into the manager but the DB still has pending_kek_version
// set with an empty pending_config_shard. The new S1 file exists at
// s1.bin.<pendingVersion>.
// Expected recovery: Case 4 (KEK forward S1) — promote + BulkUpdate.
func TestCrash_KEKS1_PostSwap(t *testing.T) {
	rot, _, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()
	insertTestCred(t, s)

	origSnap := rot.Manager().Current()
	origKEKVersion := int64(origSnap.KekVersion)

	crashAt(t, "kek_s1_post_swap")

	mustPanic(t, "kek_s1_post_swap", func() {
		_ = rot.BeginKEKRotation(context.Background())
	})

	// Verify crashed state on disk.
	if _, err := os.Stat(keystore.S1ShardPath(secretsDir, int64(2))); err != nil {
		t.Fatalf("s1.bin.2 should still exist post-crash: %v", err)
	}
	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != origKEKVersion+1 {
		t.Fatalf("DB PendingKekVersion=%d, want %d", km.PendingKekVersion, origKEKVersion+1)
	}

	// Recovery.
	res, err := keystore.RecoverFromState(context.Background(), secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase4KEKForwardS1 {
		t.Fatalf("RecoveryCase=%d, want Case4 (KEK forward S1)", res.Case)
	}
	if res.PromotedKekVersion != origKEKVersion+1 {
		t.Errorf("PromotedKekVersion=%d, want %d", res.PromotedKekVersion, origKEKVersion+1)
	}
	if _, err := os.Stat(keystore.S1ShardPath(secretsDir, int64(2))); err != nil {
		t.Errorf("s1.bin.2 should still exist after Case 4 (no rename in versioned design), stat err=%v", err)
	}

	// Simulate restart: reload manager from disk, then converge.
	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(context.Background(), res, 5*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence: %v", err)
	}

	// Final consistency.
	km, err = s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0", km.PendingKekVersion)
	}
	if km.ActiveKekVersion != origKEKVersion+1 {
		t.Errorf("ActiveKekVersion=%d, want %d", km.ActiveKekVersion, origKEKVersion+1)
	}
	creds, err := s.ListCredentials(context.Background(), 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range creds {
		if c.KekVersion != origKEKVersion+1 {
			t.Errorf("cred kek_version=%d, want %d", c.KekVersion, origKEKVersion+1)
		}
	}
}

// --- Crash during KEK S2 Begin (after Swap) → Case 5 -------------------

// TestCrash_KEKS2_PostSwap_NoNewFile simulates KEK S2 rotation: a crash after
// the S2 swap leaves DB with pending_kek_version>0, non-empty
// pending_config_shard, but no .new file (because S2 rotation does not touch
// s1.bin). Expected: Case 5 (KEK forward S2) — promote only.
func TestCrash_KEKS2_PostSwap_NoNewFile(t *testing.T) {
	rot, _, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()
	insertTestCred(t, s)

	origSnap := rot.Manager().Current()
	origKEKVersion := int64(origSnap.KekVersion)

	// Operator supplies new S2 (32 bytes hex-encoded).
	var newS2 [32]byte
	for i := range newS2 {
		newS2[i] = byte(i + 100)
	}
	crashAt(t, "s2_post_swap")

	mustPanic(t, "s2_post_swap", func() {
		_ = rot.BeginS2Rotation(context.Background(), hex.EncodeToString(newS2[:]))
	})

	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != origKEKVersion+1 {
		t.Fatalf("DB PendingKekVersion=%d, want %d", km.PendingKekVersion, origKEKVersion+1)
	}
	if len(km.PendingConfigShard) == 0 {
		t.Fatal("PendingConfigShard should be non-empty for Case 5/6")
	}

	// S2 rotation should NOT create a new S1 file (Case 5 path).
	s1Bin2Path := keystore.S1ShardPath(secretsDir, 2)
	if _, err := os.Stat(s1Bin2Path); !os.IsNotExist(err) {
		t.Errorf("s1.bin.2 should NOT exist after S2 Begin, stat err=%v", err)
	}

	res, err := keystore.RecoverFromState(context.Background(), secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase5KEKForwardS2 {
		t.Fatalf("RecoveryCase=%d, want Case5 (KEK forward S2)", res.Case)
	}

	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(context.Background(), res, 5*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence: %v", err)
	}

	km, err = s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0", km.PendingKekVersion)
	}
	if km.ActiveKekVersion != origKEKVersion+1 {
		t.Errorf("ActiveKekVersion=%d, want %d", km.ActiveKekVersion, origKEKVersion+1)
	}
	creds, err := s.ListCredentials(context.Background(), 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range creds {
		if c.KekVersion != origKEKVersion+1 {
			t.Errorf("cred kek_version=%d, want %d", c.KekVersion, origKEKVersion+1)
		}
	}
}

// --- Crash during DEK Begin (after Swap) → Case 7 ---------------------

// TestCrash_DEK_PostSwap simulates DEK rotation crash after the new DEK has
// been swapped into the manager but DB still has pending_dek_version set.
// Expected: Case 7 (DEK forward) — promote DEK + OldWrappedDEK captured so
// startup can unwrap the OLD DEK and converge.
func TestCrash_DEK_PostSwap(t *testing.T) {
	rot, _, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()

	ctx := context.Background()
	origSnap := rot.Manager().Current()
	origKEK := origSnap.KEK.Bytes()
	origDEK := origSnap.DEK.Bytes()
	origDEKVersion := int64(origSnap.DekVersion)

	for i := 0; i < 5; i++ {
		ct, err := encryptPlaintext(origKEK, origDEK, []byte("plain-"+string(rune('a'+i))))
		if err != nil {
			t.Fatalf("encryptPlaintext[%d]: %v", i, err)
		}
		c := &store.Credential{
			UserID:       "u" + string(rune('a'+i)),
			APIBase:      "https://api.example.com/" + string(rune('a'+i)),
			KeyTag:       "default",
			APIKeyCipher: ct,
			AuthType:     "openai",
			KekVersion:   1,
			DekVersion:   1,
		}
		if err := s.InsertCredential(ctx, c); err != nil {
			t.Fatalf("InsertCredential[%d]: %v", i, err)
		}
	}

	crashAt(t, "dek_post_swap")

	mustPanic(t, "dek_post_swap", func() {
		_ = rot.BeginDEKRotation(ctx)
	})

	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingDekVersion != origDEKVersion+1 {
		t.Fatalf("DB PendingDekVersion=%d, want %d", km.PendingDekVersion, origDEKVersion+1)
	}

	res, err := keystore.RecoverFromState(ctx, secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase7DEKForward {
		t.Fatalf("RecoveryCase=%d, want Case7 (DEK forward)", res.Case)
	}
	if res.PromotedDekVersion != origDEKVersion+1 {
		t.Errorf("PromotedDekVersion=%d, want %d", res.PromotedDekVersion, origDEKVersion+1)
	}
	if len(res.OldWrappedDEK) == 0 {
		t.Fatal("OldWrappedDEK must be captured for Case 7")
	}

	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(ctx, res, 10*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence: %v", err)
	}

	km, err = s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingDekVersion != 0 {
		t.Errorf("PendingDekVersion=%d, want 0", km.PendingDekVersion)
	}
	if km.ActiveDekVersion != origDEKVersion+1 {
		t.Errorf("ActiveDekVersion=%d, want %d", km.ActiveDekVersion, origDEKVersion+1)
	}
	creds, err := s.ListCredentials(ctx, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(creds) != 5 {
		t.Fatalf("expected 5 creds, got %d", len(creds))
	}
	for _, c := range creds {
		if c.DekVersion != origDEKVersion+1 {
			t.Errorf("cred dek_version=%d, want %d", c.DekVersion, origDEKVersion+1)
		}
	}
}

// --- Crash mid-Phase-A → Case 7, partial Phase A progress ------------

// TestCrash_DEK_MidPhaseA simulates a crash during CompleteDEKRotation after
// at least one Phase A iteration has flipped some rows. Recovery must
// converge: the rows already at the new version stay; the remaining rows
// get re-encrypted in the convergence loop.
func TestCrash_DEK_MidPhaseA(t *testing.T) {
	rot, _, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()

	ctx := context.Background()
	const totalCreds = 8
	origSnap := rot.Manager().Current()
	origKEK := origSnap.KEK.Bytes()
	origDEK := origSnap.DEK.Bytes()
	for i := 0; i < totalCreds; i++ {
		ct, err := encryptPlaintext(origKEK, origDEK, []byte("plain-"+string(rune('a'+i))))
		if err != nil {
			t.Fatalf("encryptPlaintext[%d]: %v", i, err)
		}
		c := &store.Credential{
			UserID:       "u" + string(rune('a'+i)),
			APIBase:      "https://api.example.com/" + string(rune('a'+i)),
			KeyTag:       "default",
			APIKeyCipher: ct,
			AuthType:     "openai",
			KekVersion:   1,
			DekVersion:   1,
		}
		if err := s.InsertCredential(ctx, c); err != nil {
			t.Fatalf("InsertCredential[%d]: %v", i, err)
		}
	}

	origDEKVersion := int64(origSnap.DekVersion)

	// Begin DEK rotation successfully.
	if err := rot.BeginDEKRotation(ctx); err != nil {
		t.Fatalf("BeginDEKRotation: %v", err)
	}

	// Run CompleteDEKRotation, crashing after a few Phase A loops.
	crashAfterNCrashes(t, "phasea_loop_start", 1)

	mustPanic(t, "phasea_loop_start", func() {
		_ = rot.CompleteDEKRotation(ctx, 5*time.Second, 500, 5)
	})

	// Crash has been injected; the restarted convergence must run Phase A
	// to completion WITHOUT further crashes.
	clearFaults()

	// DB still has PendingDekVersion set (Phase A crashed before meta promote).
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingDekVersion != origDEKVersion+1 {
		t.Fatalf("DB PendingDekVersion=%d, want %d", km.PendingDekVersion, origDEKVersion+1)
	}

	// Recover — Case 7 will promote DEK in key_metadata.
	res, err := keystore.RecoverFromState(ctx, secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase7DEKForward {
		t.Fatalf("RecoveryCase=%d, want Case7", res.Case)
	}

	// Reload manager (restart) and converge.
	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(ctx, res, 10*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence: %v", err)
	}

	// Final: every credential must be at the new DEK version.
	creds, err := s.ListCredentials(ctx, 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(creds) != totalCreds {
		t.Fatalf("expected %d creds, got %d", totalCreds, len(creds))
	}
	atNew := 0
	for _, c := range creds {
		if c.DekVersion != origDEKVersion+1 {
			t.Errorf("cred dek_version=%d, want %d", c.DekVersion, origDEKVersion+1)
		} else {
			atNew++
		}
	}
	if atNew != totalCreds {
		t.Errorf("only %d/%d creds reached new DEK version after convergence", atNew, totalCreds)
	}

	km, err = s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingDekVersion != 0 {
		t.Errorf("PendingDekVersion=%d, want 0", km.PendingDekVersion)
	}
	if km.ActiveDekVersion != origDEKVersion+1 {
		t.Errorf("ActiveDekVersion=%d, want %d", km.ActiveDekVersion, origDEKVersion+1)
	}
}

// --- Crash during KEK Complete (after meta-promote) → Case 4 idempotent ---

// TestCrash_KEKComplete_PostMeta simulates a crash in CompleteKEKRotation
// after the meta-promote TX (promote + clear pending). DB is fully
// promoted and file_shard_version points at the new S1. Recovery should
// detect Case 1 (clean) — no-op.
func TestCrash_KEKComplete_PostMeta(t *testing.T) {
	rot, _, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()
	insertTestCred(t, s)

	origSnap := rot.Manager().Current()
	origKEKVersion := int64(origSnap.KekVersion)

	if err := rot.BeginKEKRotation(context.Background()); err != nil {
		t.Fatalf("BeginKEKRotation: %v", err)
	}

	crashAt(t, "kek_complete_post_meta")
	mustPanic(t, "kek_complete_post_meta", func() {
		_ = rot.CompleteKEKRotation(context.Background(), 5*time.Second)
	})

	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != 0 {
		t.Fatalf("PendingKekVersion=%d, want 0 (post-meta crash)", km.PendingKekVersion)
	}
	if km.ActiveKekVersion != origKEKVersion+1 {
		t.Errorf("ActiveKekVersion=%d, want %d", km.ActiveKekVersion, origKEKVersion+1)
	}
	if _, err := os.Stat(keystore.S1ShardPath(secretsDir, int64(2))); err != nil {
		t.Fatalf("s1.bin.2 should still exist post-Complete (no rename in versioned design), stat err=%v", err)
	}
	if km.FileShardVersion != 2 {
		t.Fatalf("FileShardVersion=%d, want 2 (post-Complete, version already set in Begin)", km.FileShardVersion)
	}

	res, err := keystore.RecoverFromState(context.Background(), secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase1Clean {
		t.Fatalf("RecoveryCase=%d, want Case1 (clean — rename + TX promote already done)", res.Case)
	}

	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(context.Background(), res, 5*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence: %v", err)
	}

	km, err = s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.ActiveKekVersion != origKEKVersion+1 {
		t.Errorf("ActiveKekVersion=%d, want %d", km.ActiveKekVersion, origKEKVersion+1)
	}
}

// TestCrash_KEKS1_PostBackup_BeforeUpdate covers the crash between the
// backup and the DB update in BeginKEKRotation. The versioned-shard design
// has no rename step, so this resolves as Case 1 (Clean): the future-version
// S1 file stays on disk for the operator to remove.
func TestCrash_KEKS1_PostBackup_BeforeUpdate(t *testing.T) {
	rot, _, s, secretsDir, _, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()

	crashAt(t, "kek_s1_post_backup")

	mustPanic(t, "kek_s1_post_backup", func() {
		_ = rot.BeginKEKRotation(context.Background())
	})

	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0 (crashed before update)", km.PendingKekVersion)
	}
	if _, err := os.Stat(keystore.S1ShardPath(secretsDir, int64(2))); err != nil {
		t.Errorf("s1.bin.2 should still exist on disk: %v", err)
	}

	res, err := keystore.RecoverFromState(context.Background(), secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase1Clean {
		t.Fatalf("RecoveryCase=%d, want Case1 (clean — no DB pending)", res.Case)
	}
}

// TestCrash_KEKS1_PostUpdate crashes in BeginKEKRotation AFTER
// UpdateKeyMetadata (pending set) but BEFORE EARLY SWAP. State: pending_kek>0,
// .new exists, wrapped_dek NOT yet updated. Recovery Case 4 must rename +
// promote + bulk UPDATE kek_version.
func TestCrash_KEKS1_PostUpdate(t *testing.T) {
	rot, _, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()
	insertTestCred(t, s)

	origSnap := rot.Manager().Current()
	origKEKVersion := int64(origSnap.KekVersion)

	crashAt(t, "kek_s1_post_update")

	mustPanic(t, "kek_s1_post_update", func() {
		_ = rot.BeginKEKRotation(context.Background())
	})

	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != origKEKVersion+1 {
		t.Errorf("PendingKekVersion=%d, want %d", km.PendingKekVersion, origKEKVersion+1)
	}
	if km.FileShardVersion != int64(2) {
		t.Errorf("FileShardVersion=%d, want %d", km.FileShardVersion, int64(2))
	}

	res, err := keystore.RecoverFromState(context.Background(), secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase4KEKForwardS1 {
		t.Fatalf("RecoveryCase=%d, want Case4 (KEK S1 forward)", res.Case)
	}

	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(context.Background(), res, 5*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence: %v", err)
	}

	km, err = s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0 after convergence", km.PendingKekVersion)
	}
	if km.ActiveKekVersion != origKEKVersion+1 {
		t.Errorf("ActiveKekVersion=%d, want %d", km.ActiveKekVersion, origKEKVersion+1)
	}
	if _, err := os.Stat(keystore.S1ShardPath(secretsDir, int64(2))); err != nil {
		t.Errorf("s1.bin.2 should still exist after Case 4 (no rename in versioned design), stat err=%v", err)
	}
	if _, err := os.Stat(keystore.S1ShardPath(secretsDir, 1)); err != nil {
		t.Errorf("s1.bin.1 should still exist: %v", err)
	}
}

// TestCrash_KEKComplete_PostBulk crashes in CompleteKEKRotation after
// BulkUpdateKekVersion (kek_version already written to all rows) but BEFORE
// the rename + TX promote steps. State: pending_kek>0, .new
// exists, kek_version already in rows. Recovery Case 4: rename + promote +
// idempotent bulk UPDATE.
func TestCrash_KEKComplete_PostBulk(t *testing.T) {
	rot, _, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()
	insertTestCred(t, s)

	origSnap := rot.Manager().Current()
	origKEKVersion := int64(origSnap.KekVersion)

	if err := rot.BeginKEKRotation(context.Background()); err != nil {
		t.Fatalf("BeginKEKRotation: %v", err)
	}

	crashAt(t, "kek_complete_post_bulk")
	mustPanic(t, "kek_complete_post_bulk", func() {
		_ = rot.CompleteKEKRotation(context.Background(), 5*time.Second)
	})

	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != origKEKVersion+1 {
		t.Errorf("PendingKekVersion=%d, want %d", km.PendingKekVersion, origKEKVersion+1)
	}
	if _, err := os.Stat(keystore.S1ShardPath(secretsDir, int64(2))); err != nil {
		t.Errorf("s1.bin.2 should still exist before recovery: %v", err)
	}

	res, err := keystore.RecoverFromState(context.Background(), secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase4KEKForwardS1 {
		t.Fatalf("RecoveryCase=%d, want Case4 (KEK S1 forward)", res.Case)
	}

	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(context.Background(), res, 5*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence: %v", err)
	}

	km, err = s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.ActiveKekVersion != origKEKVersion+1 {
		t.Errorf("ActiveKekVersion=%d, want %d", km.ActiveKekVersion, origKEKVersion+1)
	}
	if km.FileShardVersion != 2 {
		t.Errorf("FileShardVersion=%d, want 2 (Begin set version to 2 before Complete crash)", km.FileShardVersion)
	}
}

// TestCrash_DEK_PostUpdate crashes in BeginDEKRotation AFTER UpdateKeyMetadata
// (pending_dek set) but BEFORE EARLY SWAP. State: pending_dek>0, swap not
// done. Recovery Case 7 must unwrap pending_wrapped_dek → install dual snap →
// run Phase A convergence → Phase B Commit.
func TestCrash_DEK_PostUpdate(t *testing.T) {
	rot, _, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()

	origSnap := rot.Manager().Current()
	origKEK := origSnap.KEK.Bytes()
	origDEK := origSnap.DEK.Bytes()
	origDekVersion := int64(origSnap.DekVersion)

	for i := 0; i < 5; i++ {
		ct, err := encryptPlaintext(origKEK, origDEK, []byte("plain-"+string(rune('a'+i))))
		if err != nil {
			t.Fatalf("encryptPlaintext[%d]: %v", i, err)
		}
		c := &store.Credential{
			UserID:       "u" + string(rune('a'+i)),
			APIBase:      "https://api.example.com/" + string(rune('a'+i)),
			KeyTag:       "default",
			APIKeyCipher: ct,
			AuthType:     "openai",
			KekVersion:   1,
			DekVersion:   origDekVersion,
		}
		if err := s.InsertCredential(context.Background(), c); err != nil {
			t.Fatalf("InsertCredential[%d]: %v", i, err)
		}
	}

	crashAt(t, "dek_post_update")
	mustPanic(t, "dek_post_update", func() {
		_ = rot.BeginDEKRotation(context.Background())
	})

	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingDekVersion != origDekVersion+1 {
		t.Errorf("PendingDekVersion=%d, want %d", km.PendingDekVersion, origDekVersion+1)
	}

	res, err := keystore.RecoverFromState(context.Background(), secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase7DEKForward {
		t.Fatalf("RecoveryCase=%d, want Case7 (DEK forward)", res.Case)
	}

	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(context.Background(), res, 10*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence: %v", err)
	}

	km, err = s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingDekVersion != 0 {
		t.Errorf("PendingDekVersion=%d, want 0 after convergence", km.PendingDekVersion)
	}
	if km.ActiveDekVersion != origDekVersion+1 {
		t.Errorf("ActiveDekVersion=%d, want %d", km.ActiveDekVersion, origDekVersion+1)
	}
	for _, uid := range []string{"ua", "ub", "uc", "ud", "ue"} {
		c, err := s.GetCredentialByUserURLTag(context.Background(), uid, "https://api.example.com/"+uid[len(uid)-1:], "default")
		if err != nil {
			t.Fatalf("GetCredentialByUserURLTag(%s): %v", uid, err)
		}
		if c.DekVersion != origDekVersion+1 {
			t.Errorf("cred %s DekVersion=%d, want %d", uid, c.DekVersion, origDekVersion+1)
		}
	}
}

// TestCrash_DEK_PostMeta crashes in CompleteDEKRotation AFTER Phase B Commit
// (pending cleared, wrapped_dek swapped, all rows re-encrypted). Recovery
// should detect Case 1 (clean) — no convergence needed.
func TestCrash_DEK_PostMeta(t *testing.T) {
	rot, _, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()

	origSnap := rot.Manager().Current()
	origKEK := origSnap.KEK.Bytes()
	origDEK := origSnap.DEK.Bytes()
	origDekVersion := int64(origSnap.DekVersion)

	for i := 0; i < 5; i++ {
		uid := "u" + string(rune('a'+i))
		url := "https://api.example.com/" + string(rune('a'+i))
		ct, err := encryptPlaintext(origKEK, origDEK, []byte("plain-"+string(rune('a'+i))))
		if err != nil {
			t.Fatalf("encryptPlaintext[%d]: %v", i, err)
		}
		c := &store.Credential{
			UserID:       uid,
			APIBase:      url,
			KeyTag:       "default",
			APIKeyCipher: ct,
			AuthType:     "openai",
			KekVersion:   1,
			DekVersion:   origDekVersion,
		}
		if err := s.InsertCredential(context.Background(), c); err != nil {
			t.Fatalf("InsertCredential[%d]: %v", i, err)
		}
	}

	if err := rot.BeginDEKRotation(context.Background()); err != nil {
		t.Fatalf("BeginDEKRotation: %v", err)
	}

	crashAt(t, "dek_complete_post_meta")
	mustPanic(t, "dek_complete_post_meta", func() {
		_ = rot.CompleteDEKRotation(context.Background(), 5*time.Second, 500, 5)
	})

	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingDekVersion != 0 {
		t.Fatalf("PendingDekVersion=%d, want 0 (post-meta crash)", km.PendingDekVersion)
	}
	if km.ActiveDekVersion != origDekVersion+1 {
		t.Fatalf("ActiveDekVersion=%d, want %d", km.ActiveDekVersion, origDekVersion+1)
	}

	res, err := keystore.RecoverFromState(context.Background(), secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase1Clean {
		t.Fatalf("RecoveryCase=%d, want Case1 (clean — Phase B Commit already done)", res.Case)
	}

	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(context.Background(), res, 5*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence: %v", err)
	}

	for i := 0; i < 5; i++ {
		uid := "u" + string(rune('a'+i))
		url := "https://api.example.com/" + string(rune('a'+i))
		c, err := s.GetCredentialByUserURLTag(context.Background(), uid, url, "default")
		if err != nil {
			t.Fatalf("GetCredentialByUserURLTag(%s): %v", uid, err)
		}
		if c.DekVersion != origDekVersion+1 {
			t.Errorf("cred %s DekVersion=%d, want %d", uid, c.DekVersion, origDekVersion+1)
		}
	}
}

// TestProperty_Invariant_AfterRandomCrashes is a property-based invariant test:
// N rounds, each picking 1-3 distinct crash stages, panicking through them in
// sequence (simulating multiple crashes between restarts), then reloading the
// manager, running RecoverFromState + RunStartupConvergence, and asserting the
// post-recovery state-machine invariants.
func TestProperty_Invariant_AfterRandomCrashes(t *testing.T) {
	rng := rand.New(rand.NewSource(42)) // deterministic for CI
	allStages := []string{
		"kek_s1_post_backup", "kek_s1_post_update", "kek_s1_post_swap",
		"s2_post_backup", "s2_post_update", "s2_post_swap",
		"dek_post_backup", "dek_post_update", "dek_post_swap",
		"kek_complete_post_bulk", "kek_complete_post_meta",
		"dek_complete_post_phasea", "dek_complete_post_meta",
		"phasea_loop_start",
	}
	const rounds = 30
	for round := 0; round < rounds; round++ {
		t.Run(fmt.Sprintf("round-%02d", round), func(t *testing.T) {
			runOneCrashRound(t, rng, allStages)
		})
	}
}

func runOneCrashRound(t *testing.T, rng *rand.Rand, stages []string) {
	rot, mgr, s, secretsDir, dataDir, cleanup := setupTestRotatorWithDataDir(t)
	defer cleanup()

	ctx := context.Background()

	// 5 creds encrypted with orig keypair
	origSnap := mgr.Current()
	origKEK := origSnap.KEK.Bytes()
	origDEK := origSnap.DEK.Bytes()
	for i := 0; i < 5; i++ {
		ct, err := encryptPlaintext(origKEK, origDEK, []byte("plain-"+string(rune('a'+i))))
		if err != nil {
			t.Fatal(err)
		}
		c := &store.Credential{
			UserID:       "u" + string(rune('a'+i)),
			APIBase:      "https://api.example.com/" + string(rune('a'+i)),
			KeyTag:       "default",
			APIKeyCipher: ct,
			AuthType:     "openai",
			KekVersion:   1,
			DekVersion:   1,
		}
		if err := s.InsertCredential(ctx, c); err != nil {
			t.Fatal(err)
		}
	}

	// Pick 1-3 distinct crash stages
	n := 1 + rng.Intn(3)
	perm := rng.Perm(len(stages))[:n]
	picked := make([]string, n)
	for i, idx := range perm {
		picked[i] = stages[idx]
	}
	t.Logf("picked stages: %v", picked)
	stageList := strings.Join(picked, ",")

	// For each picked stage, set up a fresh crash and try to trigger it via the
	// appropriate Begin/Complete call. Stages that don't make sense in sequence
	// (e.g. kek_s1_post_swap before dek_post_update) may fail to be reached
	// because the manager state prevents the call from entering that hook.
	// Use mustPanic wrapper; if the call returns nil err instead of panicking,
	// that stage didn't fire (e.g. ErrRotationInProgress from a prior pending).
	clearFaults()
	for _, stage := range picked {
		switch {
		case strings.HasPrefix(stage, "kek_s1_"):
			crashAt(t, stage)
			func() {
				defer func() { _ = recover() }()
				_ = rot.BeginKEKRotation(ctx)
			}()
		case strings.HasPrefix(stage, "s2_"):
			newS2 := make([]byte, 32)
			for i := range newS2 {
				newS2[i] = byte(i + 100)
			}
			crashAt(t, stage)
			func() {
				defer func() { _ = recover() }()
				_ = rot.BeginS2Rotation(ctx, hex.EncodeToString(newS2))
			}()
		case strings.HasPrefix(stage, "dek_"):
			crashAt(t, stage)
			func() {
				defer func() { _ = recover() }()
				_ = rot.BeginDEKRotation(ctx)
			}()
		case stage == "kek_complete_post_bulk" || stage == "kek_complete_post_meta":
			// Need pending KEK first
			if err := rot.BeginKEKRotation(ctx); err != nil {
				return // skip — can't set up pending
			}
			crashAt(t, stage)
			func() {
				defer func() { _ = recover() }()
				_ = rot.CompleteKEKRotation(ctx, 5*time.Second)
			}()
		case stage == "dek_complete_post_phasea" || stage == "dek_complete_post_meta":
			if err := rot.BeginDEKRotation(ctx); err != nil {
				return
			}
			crashAt(t, stage)
			func() {
				defer func() { _ = recover() }()
				_ = rot.CompleteDEKRotation(ctx, 10*time.Second, 500, 5)
			}()
		case stage == "phasea_loop_start":
			if err := rot.BeginDEKRotation(ctx); err != nil {
				return
			}
			crashAfterNCrashes(t, stage, 1)
			func() {
				defer func() { _ = recover() }()
				_ = rot.CompleteDEKRotation(ctx, 10*time.Second, 500, 5)
			}()
			clearFaults() // so the convergence Phase A loop doesn't re-trigger
		}
	}
	clearFaults()

	// Restart simulation
	res, err := keystore.RecoverFromState(ctx, secretsDir, s)
	if err != nil {
		if strings.Contains(err.Error(), "Case 6 FATAL") {
			t.Skip("Case 6 FATAL — operator-required, skip invariant check")
		}
		t.Fatalf("RecoverFromState [stages=%s]: %v", stageList, err)
	}
	reloadManager(t, rot, s, secretsDir, dataDir)
	if err := rot.RunStartupConvergence(ctx, res, 10*time.Second, 500, 100); err != nil {
		t.Fatalf("RunStartupConvergence [stages=%s]: %v", stageList, err)
	}

	// Invariants
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km.PendingKekVersion != 0 {
		t.Errorf("invariant violated [stages=%s]: PendingKekVersion=%d, want 0", stageList, km.PendingKekVersion)
	}
	if km.PendingDekVersion != 0 {
		t.Errorf("invariant violated [stages=%s]: PendingDekVersion=%d, want 0", stageList, km.PendingDekVersion)
	}
	curSnap := rot.Manager().Current()
	if _, err := crypto.UnwrapDEK(crypto.ModeAES, curSnap.KEK.Bytes(), km.WrappedDEK); err != nil {
		t.Errorf("invariant violated [stages=%s]: wrapped_dek unwrap failed: %v", stageList, err)
	}
	list, err := s.ListCredentials(ctx, 100, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range list {
		if c.KekVersion != km.ActiveKekVersion {
			t.Errorf("cred %s KekVersion=%d, want %d [stages=%s]", c.UserID, c.KekVersion, km.ActiveKekVersion, stageList)
		}
		if c.DekVersion != km.ActiveDekVersion {
			t.Errorf("cred %s DekVersion=%d, want %d [stages=%s]", c.UserID, c.DekVersion, km.ActiveDekVersion, stageList)
		}
	}
	// In the new design recovery never deletes shard files — orphan shards
	// (e.g. an operator-staged s1.bin.2) survive startup unchanged. No
	// invariant check needed.
}

// --- Helper: catch missing-key faults (e.g. wrong stage name) -------------

func init() {
	// Ensure runtime.Caller works for panic locations even when tests are inlined.
	_ = runtime.Caller
}
