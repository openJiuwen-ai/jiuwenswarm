//go:build cgo

package keystore_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"credential_router/internal/credmgr/keystore"
)

func TestRecoverFromStateClean(t *testing.T) {
	_, _, s, secretsDir, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()
	res, err := keystore.RecoverFromState(ctx, secretsDir, s)
	if err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if res.Case != keystore.RecoveryCase1Clean {
		t.Errorf("Case=%d, want %d", res.Case, keystore.RecoveryCase1Clean)
	}
	if len(res.Actions) != 0 {
		t.Errorf("expected no actions, got %v", res.Actions)
	}
}

func TestStartupSyncConvergenceMatch(t *testing.T) {
	_, mgr, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	if err := keystore.StartupSyncConvergence(context.Background(), mgr, s); err != nil {
		t.Errorf("expected no error, got %v", err)
	}
}

func TestStartupSyncConvergenceMismatch(t *testing.T) {
	_, mgr, s, _, cleanup := setupTestRotator(t)
	defer cleanup()

	// Manually mutate DB to mismatch manager
	km, err := s.GetKeyMetadata(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	km.ActiveKekVersion = 999
	if err := s.UpdateKeyMetadata(context.Background(), km); err != nil {
		t.Fatal(err)
	}

	if err := keystore.StartupSyncConvergence(context.Background(), mgr, s); err == nil {
		t.Error("expected mismatch error")
	}
}

func TestRecoverFromStateNested(t *testing.T) {
	_, _, s, secretsDir, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = 2
	km.PendingDekVersion = 2
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	_, err = keystore.RecoverFromState(ctx, secretsDir, s)
	if err != keystore.ErrNestedRotation {
		t.Errorf("got %v, want ErrNestedRotation", err)
	}
}

// TestRecoverOrphanShardFile — orphan s1.bin.<N> files are not cleaned up
// by recovery; the operator must remove them.
func TestRecoverOrphanShardFile(t *testing.T) {
	_, _, s, secretsDir, cleanup := setupTestRotator(t)
	defer cleanup()

	orphanPath := filepath.Join(secretsDir, "s1.bin.99")
	if err := os.WriteFile(orphanPath, []byte("12345678901234567890123456789012"), 0o600); err != nil {
		t.Fatal(err)
	}

	ctx := context.Background()
	res, err := keystore.RecoverFromState(ctx, secretsDir, s)
	if err != nil {
		t.Fatalf("Recover: %v", err)
	}
	if res.Case != keystore.RecoveryCase1Clean {
		t.Errorf("expected Clean case for orphan-only state, got %d", res.Case)
	}
	if _, err := os.Stat(orphanPath); os.IsNotExist(err) {
		t.Error("orphan file must remain on disk (operator cleanup)")
	}
}

func TestRecoverKEKForwardNoNewFile(t *testing.T) {
	_, _, s, secretsDir, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = 2
	km.PendingWrappedDEK = []byte("not-real-wrapped-data")
	km.PendingConfigShard = []byte("shard2")
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	_, _ = keystore.RecoverFromState(ctx, secretsDir, s)
	km2, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km2.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0 after promote", km2.PendingKekVersion)
	}
	if km2.ActiveKekVersion != 2 {
		t.Errorf("ActiveKekVersion=%d, want 2", km2.ActiveKekVersion)
	}
}

// TestRecoverCase4ForwardS1 — the rollback path was removed in the
// versioned-shard design. With pending_kek>0 and pending_config_shard
// empty, recovery always promotes to Case 4 (KEKForwardS1).
func TestRecoverCase4ForwardS1(t *testing.T) {
	_, _, s, secretsDir, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingKekVersion = km.ActiveKekVersion + 1
	km.PendingWrappedDEK = append([]byte(nil), km.WrappedDEK...)
	km.PendingConfigShard = []byte{}
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	res, err := keystore.RecoverFromState(ctx, secretsDir, s)
	if err != nil {
		t.Fatalf("RecoverFromState: %v", err)
	}
	if res.Case != keystore.RecoveryCase4KEKForwardS1 {
		t.Errorf("Case=%d, want %d (Case4 forward S1)", res.Case, keystore.RecoveryCase4KEKForwardS1)
	}

	km2, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km2.PendingKekVersion != 0 {
		t.Errorf("PendingKekVersion=%d, want 0 after promote", km2.PendingKekVersion)
	}
	if len(km2.PendingWrappedDEK) != 0 {
		t.Errorf("PendingWrappedDEK len=%d, want 0", len(km2.PendingWrappedDEK))
	}
	if km2.ActiveKekVersion != km.PendingKekVersion {
		t.Errorf("ActiveKekVersion=%d, want %d (promote to pending)", km2.ActiveKekVersion, km.PendingKekVersion)
	}
}

func TestRecoverDEKForward(t *testing.T) {
	_, _, s, secretsDir, cleanup := setupTestRotator(t)
	defer cleanup()

	ctx := context.Background()
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	km.PendingDekVersion = 3
	km.PendingWrappedDEK = []byte("not-real-dek-wrap")
	if err := s.UpdateKeyMetadata(ctx, km); err != nil {
		t.Fatal(err)
	}

	_, _ = keystore.RecoverFromState(ctx, secretsDir, s)
	km2, err := s.GetKeyMetadata(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if km2.PendingDekVersion != 0 {
		t.Errorf("PendingDekVersion=%d, want 0", km2.PendingDekVersion)
	}
	if km2.ActiveDekVersion != 3 {
		t.Errorf("ActiveDekVersion=%d, want 3", km2.ActiveDekVersion)
	}
}
