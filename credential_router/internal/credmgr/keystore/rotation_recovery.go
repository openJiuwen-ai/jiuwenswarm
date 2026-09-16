//go:build cgo

package keystore

import (
	"context"
	"errors"
	"fmt"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/platform"
	"credential_router/internal/credmgr/store"
)

// Recovery state machine constants — the RecoveryCase enum classifies
// what RecoverFromState found in key_metadata on startup and which action
// it took to bring the system back to a consistent state.
type RecoveryCase int

const (
	RecoveryCase1Clean RecoveryCase = iota + 1
	RecoveryCase4KEKForwardS1
	RecoveryCase5KEKForwardS2
	RecoveryCase7DEKForward
	RecoveryCaseNested // both KEK and DEK pending — FATAL; concurrent rotation in two streams
)

// RecoveryResult describes what the recovery did.
type RecoveryResult struct {
	Case               RecoveryCase
	Actions            []string
	Snapshot           *KeySnapshot
	OldWrappedDEK      []byte
	PromotedKekVersion int64
	PromotedDekVersion int64
}

// RecoverFromState inspects key_metadata to determine recovery case and
// reconciles state.
//
// Cases:
//
//	1 (clean): nothing pending → no-op
//	4 (KEK forward S1): pending_kek>0, pending_config_shard empty → promote
//	5 (KEK forward S2): pending_kek>0, pending_config_shard non-empty → promote
//	7 (DEK forward): pending_dek>0 → promote
//	nested: pending both KEK and DEK → FATAL
func RecoverFromState(ctx context.Context, secretsDir string, s *store.Store) (*RecoveryResult, error) {
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		if errors.Is(err, platform.ErrNotFound) {
			return &RecoveryResult{Case: RecoveryCase1Clean}, nil
		}
		return nil, fmt.Errorf("recovery: read key_metadata: %w", err)
	}

	res := &RecoveryResult{Case: RecoveryCase1Clean}

	hasPendingKEK := km.PendingKekVersion > 0
	hasPendingDEK := km.PendingDekVersion > 0

	if hasPendingKEK && hasPendingDEK {
		res.Case = RecoveryCaseNested
		return res, ErrNestedRotation
	}

	if hasPendingKEK {
		res.PromotedKekVersion = km.PendingKekVersion
		if len(km.PendingConfigShard) > 0 {
			res.Case = RecoveryCase5KEKForwardS2
			res.Actions = append(res.Actions, "promote S2 (KEK S2 path midway)")
		} else {
			res.Case = RecoveryCase4KEKForwardS1
			res.Actions = append(res.Actions, "promote S1 (KEK S1 path midway)")
		}
		promotePendingKEK(km)
		if err := s.UpdateKeyMetadata(ctx, km); err != nil {
			return res, fmt.Errorf("recovery: update kek: %w", err)
		}
	}

	if hasPendingDEK {
		res.Case = RecoveryCase7DEKForward
		res.PromotedDekVersion = km.PendingDekVersion
		res.OldWrappedDEK = append([]byte(nil), km.WrappedDEK...)
		now := km.DekRotatedAt
		if now == 0 {
			now = km.UpdatedAt
		}
		km.ActiveDekVersion = km.PendingDekVersion
		km.WrappedDEK = km.PendingWrappedDEK
		km.LastRotateAt = now
		km.PendingDekVersion = 0
		km.PendingWrappedDEK = []byte{}
		if err := s.UpdateKeyMetadata(ctx, km); err != nil {
			return res, fmt.Errorf("recovery: promote dek: %w", err)
		}
	}

	res.Snapshot, err = buildSnapshotFromMetadata(secretsDir, km)
	if err != nil {
		return res, fmt.Errorf("recovery: build snapshot: %w", err)
	}
	return res, nil
}

// promotePendingKEK copies pending KEK fields into active slots. Caller persists.
func promotePendingKEK(km *store.KeyMetadata) {
	km.ActiveKekVersion = km.PendingKekVersion
	km.WrappedDEK = append([]byte(nil), km.PendingWrappedDEK...)
	if len(km.PendingConfigShard) > 0 {
		km.ActiveConfigShard = append([]byte(nil), km.PendingConfigShard...)
	}
	km.PendingKekVersion = 0
	km.PendingWrappedDEK = []byte{}
	km.PendingConfigShard = []byte{}
}

// ErrNestedRotation indicates an attempt to start a rotation while
// another rotation is already pending — both a KEK rotation and a DEK
// rotation are in flight at the same time. This is fatal because the
// state-machine invariants assume at most one rotation pending per kind.
var ErrNestedRotation = errors.New("keystore: nested KEK+DEK rotation (both pending)")

// StartupSyncConvergence checks DB and Manager state match after recovery.
// Logs mismatches but does not fail; caller decides whether to abort.
func StartupSyncConvergence(ctx context.Context, mgr *Manager, s *store.Store) error {
	if mgr.Current() == nil {
		return fmt.Errorf("sync: manager has no current snapshot")
	}
	km, err := s.GetKeyMetadata(ctx)
	if err != nil {
		return fmt.Errorf("sync: read key_metadata: %w", err)
	}
	cur := mgr.Current()
	if int64(cur.KekVersion) != km.ActiveKekVersion {
		return fmt.Errorf("sync: kek version mismatch (manager=%d, db=%d)", cur.KekVersion, km.ActiveKekVersion)
	}
	if int64(cur.DekVersion) != km.ActiveDekVersion {
		return fmt.Errorf("sync: dek version mismatch (manager=%d, db=%d)", cur.DekVersion, km.ActiveDekVersion)
	}
	if km.PendingKekVersion > 0 || km.PendingDekVersion > 0 {
		return fmt.Errorf("sync: DB still has pending fields after recovery (kek=%d, dek=%d)", km.PendingKekVersion, km.PendingDekVersion)
	}
	return nil
}

func buildSnapshotFromMetadata(secretsDir string, km *store.KeyMetadata) (*KeySnapshot, error) {
	s1, err := LoadS1FromFile(S1ShardPath(secretsDir, km.FileShardVersion))
	if err != nil {
		return nil, fmt.Errorf("load s1: %w", err)
	}
	var s2 [ShardSize]byte
	copy(s2[:], km.ActiveConfigShard)
	kek, err := DeriveKEK(s1, s2, km.CryptoMode)
	if err != nil {
		return nil, fmt.Errorf("derive kek: %w", err)
	}
	dek, err := crypto.UnwrapDEK(km.CryptoMode, kek.Bytes(), km.WrappedDEK)
	if err != nil {
		return nil, fmt.Errorf("unwrap dek: %w", err)
	}
	return &KeySnapshot{
		KEK:        crypto.NewKeyBytes(kek.Bytes()),
		DEK:        crypto.NewKeyBytes(dek),
		KekVersion: uint64(km.ActiveKekVersion),
		DekVersion: uint64(km.ActiveDekVersion),
		CryptoMode: km.CryptoMode,
	}, nil
}
