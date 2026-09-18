// rotation.go — key-rotation state machine.
//
// Drives the full rotation lifecycle:
//
//	KEK rotation:    S1/S2 shard swap with 3 fault-injection stages each
//	                 (kek_s1_post_backup|update|swap; s2_post_backup|update|swap;
//	                  kek_complete_post_bulk|meta)
//	DEK rotation:    re-encrypt all wrapped DEKs under the new KEK
//	Phase A loop:    stream credentials, rewrite under the new DEK,
//	                 loop until `dek_count_remaining == 0` (or maxPhaseALoops)
//	StartAutoRotate: background ticker that periodically triggers DEK rotation
//	faultHook:       test-only crash-injection (nil in production builds)
//
// Coordination:
//
//	snapshot_lifecycle.go — atomic KeySnapshot swap (current/previous)
//	backup.BackupManager  — pre-rotation snapshot
//	store.Store           — dek_version bookkeeping + Phase A row reads
//	rotation_recovery.go  — startup-time convergence from partial-failure state
//
//go:build cgo

package keystore

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"sync"
	"time"

	"credential_router/internal/credmgr/backup"
	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/store"
)

// Errors for rotation flow.
var (
	ErrRotationInProgress = fmt.Errorf("keystore: rotation already in progress")
	ErrInvalidNewS1       = fmt.Errorf("keystore: invalid new S1 file")
)

// faultHook is a test-only crash-injection mechanism. Tests set it to a
// function that panics at a chosen stage to simulate process death at
// specific points during rotation. In production builds, faultHook is nil
// and invokeFault is a single nil-check. See crash_injection_test.go for the
// full list of supported stages.
//
// Stage names (consumed by invokeFault):
//
//	KEK S1 Begin:    kek_s1_post_backup | kek_s1_post_update | kek_s1_post_swap
//	KEK S2 Begin:    s2_post_backup      | s2_post_update      | s2_post_swap
//	DEK Begin:       dek_post_backup     | dek_post_update     | dek_post_swap
//	KEK Complete:    kek_complete_post_bulk | kek_complete_post_meta
//	DEK Complete:    dek_complete_post_phasea | dek_complete_post_meta
//	Phase A loop:    phasea_loop_start   (fires at start of each iteration)
var faultHook func(stage string)

func invokeFault(stage string) {
	if faultHook != nil {
		faultHook(stage)
	}
}

// checkPendingRotation validates the requested rotation start state.
// Returns ErrNestedRotation if BOTH kek and dek are pending — fatal.
// Returns ErrRotationInProgress if only one type is pending — caller waits
// or surfaces as 409.
func checkPendingRotation(km *store.KeyMetadata) error {
	if km.PendingKekVersion > 0 && km.PendingDekVersion > 0 {
		return fmt.Errorf("%w: kek=%d, dek=%d", ErrNestedRotation, km.PendingKekVersion, km.PendingDekVersion)
	}
	if km.PendingKekVersion > 0 || km.PendingDekVersion > 0 {
		return fmt.Errorf("%w: pending rotation exists (kek=%d, dek=%d)",
			ErrRotationInProgress, km.PendingKekVersion, km.PendingDekVersion)
	}
	return nil
}

// beginRotation acquires the rotation mutex, reads key_metadata, validates
// no pending rotation, and returns the current snapshot. Concurrency with
// concurrent admin CRUD is now mediated by row_version + dek_version guards
// in the store layer (see ReencryptDekVersionBatch row_version predicate).
// On any error path the mutex is released before returning; the helper
// always either succeeds with a usable release func, or returns nil release.
func (r *Rotator) beginRotation(ctx context.Context) (*store.KeyMetadata, *KeySnapshot, func(), error) {
	if !r.adminRotationMu.TryLock() {
		return nil, nil, nil, ErrRotationInProgress
	}
	release := r.adminRotationMu.Unlock

	km, err := r.store.GetKeyMetadata(ctx)
	if err != nil {
		release()
		return nil, nil, nil, fmt.Errorf("keystore: read key_metadata: %w", err)
	}
	if err := checkPendingRotation(km); err != nil {
		release()
		return nil, nil, nil, err
	}
	current := r.manager.Current()
	if current == nil {
		release()
		return nil, nil, nil, fmt.Errorf("keystore: no current snapshot")
	}
	return km, current, release, nil
}

// Rotator orchestrates KEK/DEK rotation. The single adminRotationMu
// mutex prevents nested rotation — only one rotation can be in flight
// at any time, which is what makes the swap+drain state machine tractable.
type Rotator struct {
	adminRotationMu sync.Mutex
	manager         *Manager
	store           *store.Store
	backupMgr       *backup.BackupManager
	secretsDir      string

	convergeMu sync.Mutex
	converge   convergenceStatus
}

// convergenceStatus is the public observable of RunStartupConvergence.
// "idle" is the initial value before the goroutine starts; "running" while
// Phase A is in flight; "completed" on success; "failed" with Err set on any
// error. Surfaced via /v1/health so operators can detect a stalled startup.
type convergenceStatus struct {
	State      string
	Err        string
	StartedAt  time.Time
	FinishedAt time.Time
}

const (
	convergeIdle      = "idle"
	convergeRunning   = "running"
	convergeCompleted = "completed"
	convergeFailed    = "failed"
)

// MaxRowsPerTx is the upper bound on rows re-encrypted per SQLite
// transaction during Phase A convergence. Not configurable: a single
// transaction must stay short to avoid holding the credentials-table
// write lock longer than the rotation drain window.
const MaxRowsPerTx = 1000

func (r *Rotator) ConvergenceState() convergenceStatus {
	r.convergeMu.Lock()
	defer r.convergeMu.Unlock()
	return r.converge
}

func (r *Rotator) setConvergence(state string, err error) {
	r.convergeMu.Lock()
	defer r.convergeMu.Unlock()
	r.converge.State = state
	r.converge.FinishedAt = time.Now()
	if r.converge.StartedAt.IsZero() {
		r.converge.StartedAt = r.converge.FinishedAt
	}
	if state == convergeRunning {
		r.converge.StartedAt = r.converge.FinishedAt
		r.converge.FinishedAt = time.Time{}
		r.converge.Err = ""
	}
	if err != nil {
		r.converge.Err = err.Error()
	}
}

// NewRotator creates a Rotator.
// SecretsDir returns the secrets directory used by the rotator.
func (r *Rotator) SecretsDir() string {
	return r.secretsDir
}

func NewRotator(mgr *Manager, s *store.Store, bm *backup.BackupManager, secretsDir string) *Rotator {
	return &Rotator{
		manager:    mgr,
		store:      s,
		backupMgr:  bm,
		secretsDir: secretsDir,
	}
}

// Manager returns the underlying key Manager for direct snapshot access.
func (r *Rotator) Manager() *Manager {
	return r.manager
}

// BeginKEKRotation starts a KEK rotation. A fresh random S1 is generated
// from crypto/rand and persisted to <secretsDir>/s1.bin.<FileShardVersion+1>;
// the active S2 from key_metadata is reused. Exposed via:
//	POST /v1/keystore/shards {action: "rotate-s1"}
//
// Steps:
//  1. Acquire rotation mutex
//  2. LockWrite
//  3. Read key_metadata, check no pending
//  4. Generate new S1, write to s1.bin.<FileShardVersion+1>, derive new KEK
//     via DeriveKEK(s1, s2)
//  5. Wrap current DEK with new KEK → newWrappedDEK
//  6. Update key_metadata: pending_kek_version=active+1, pending_wrapped_dek,
//     pending_config_shard, file_shard_version=newS1Version
//  7. Backup DB + key snapshot (type=kek) BEFORE the DB TX commit, so a
//     crash between commit and backup doesn't leave the DB referencing
//     shards without a recoverable on-disk copy
//  8. Swap Manager: current=newSnap, previous=oldSnap (early swap)
//  9. UnlockWrite
func (r *Rotator) BeginKEKRotation(ctx context.Context) error {
	km, current, release, err := r.beginRotation(ctx)
	if err != nil {
		return err
	}
	defer release()

	newS1Version := km.FileShardVersion + 1

	var newS1 [ShardSize]byte
	if _, err := io.ReadFull(rand.Reader, newS1[:]); err != nil {
		return fmt.Errorf("keystore: generate new S1: %w", err)
	}
	if err := WriteS1ToFile(S1ShardPath(r.secretsDir, newS1Version), newS1); err != nil {
		return fmt.Errorf("keystore: write S1 shard: %w", err)
	}

	var activeS2 [ShardSize]byte
	copy(activeS2[:], km.ActiveConfigShard)
	newKEK, err := DeriveKEK(newS1, activeS2, current.CryptoMode)
	if err != nil {
		return fmt.Errorf("keystore: derive new KEK: %w", err)
	}

	newWrappedDEK, err := crypto.WrapDEK(current.CryptoMode, newKEK.Bytes(), current.DEK.Bytes())
	if err != nil {
		return fmt.Errorf("keystore: wrap DEK with new KEK: %w", err)
	}

	newPendingVer := km.ActiveKekVersion + 1
	km.PendingKekVersion = newPendingVer
	km.PendingWrappedDEK = newWrappedDEK
	km.PendingConfigShard = []byte{}
	km.FileShardVersion = newS1Version
	km.FileShardRotatedAt = time.Now().Unix()

	if r.backupMgr != nil {
		if err := r.backupMgr.Backup(ctx, backup.BackupKEK, byte(current.CryptoMode), newS1, activeS2, newWrappedDEK); err != nil {
			return fmt.Errorf("keystore: backup: %w", err)
		}
	}
	invokeFault("kek_s1_post_backup")

	if err := r.store.UpdateKeyMetadata(ctx, km); err != nil {
		return fmt.Errorf("keystore: update key_metadata: %w", err)
	}
	invokeFault("kek_s1_post_update")

	// Defensive copy DEK (manager will own it after Swap)
	dekCopy := current.DEK.Bytes()
	newSnap := &KeySnapshot{
		KEK:        crypto.NewKeyBytes(newKEK.Bytes()),
		DEK:        crypto.NewKeyBytes(dekCopy),
		KekVersion: uint64(newPendingVer),
		DekVersion: current.DekVersion,
		CryptoMode: current.CryptoMode,
	}
	newKEK.Zero() // zero source after copy

	r.manager.Swap(newSnap)
	invokeFault("kek_s1_post_swap")

	return nil
}

// BeginS2Rotation starts an S2-only KEK rotation. The active S1 file is
// reused; a fresh random S2 is generated and persisted as pending_config_shard.
// Exposed via:
//	POST /v1/keystore/shards {action: "rotate-s2"}
//
// Steps mirror BeginKEKRotation except:
//   - No new S1 to load from disk
//   - S2 is either operator-provided (operatorS2 hex string, 64 chars) or freshly generated
//   - FileShardRotatedAt is NOT updated — S2 lives in the DB, not the file,
//     so the S1-shard's rotation timestamp is unrelated to S2 changes
func (r *Rotator) BeginS2Rotation(ctx context.Context, operatorS2 string) error {
	km, current, release, err := r.beginRotation(ctx)
	if err != nil {
		return err
	}
	defer release()

	activeS1, err := LoadS1FromFile(S1ShardPath(r.secretsDir, km.FileShardVersion))
	if err != nil {
		return fmt.Errorf("keystore: load active S1: %w", err)
	}

	var newS2 [ShardSize]byte
	if operatorS2 != "" {
		raw, decErr := hex.DecodeString(operatorS2)
		if decErr != nil || len(raw) != ShardSize {
			return fmt.Errorf("%w: operator S2 must be %d-byte hex (%d chars)",
				ErrInvalidNewS1, ShardSize, ShardSize*2)
		}
		copy(newS2[:], raw)
	} else {
		if _, err := io.ReadFull(rand.Reader, newS2[:]); err != nil {
			return fmt.Errorf("keystore: generate new S2: %w", err)
		}
	}

	newKEK, err := DeriveKEK(activeS1, newS2, current.CryptoMode)
	if err != nil {
		return fmt.Errorf("keystore: derive new KEK: %w", err)
	}

	newWrappedDEK, err := crypto.WrapDEK(current.CryptoMode, newKEK.Bytes(), current.DEK.Bytes())
	if err != nil {
		return fmt.Errorf("keystore: wrap DEK with new KEK: %w", err)
	}

	newPendingVer := km.ActiveKekVersion + 1
	km.PendingKekVersion = newPendingVer
	km.PendingWrappedDEK = newWrappedDEK
	km.PendingConfigShard = newS2[:]

	// Backup MUST precede the DB TX commit: a crash between commit and
	// backup would leave the row referencing shards that exist only in
	// memory, with no on-disk recoverable copy.
	if r.backupMgr != nil {
		if err := r.backupMgr.Backup(ctx, backup.BackupKEK, byte(current.CryptoMode), activeS1, newS2, newWrappedDEK); err != nil {
			return fmt.Errorf("keystore: backup: %w", err)
		}
	}
	invokeFault("s2_post_backup")

	if err := r.store.UpdateKeyMetadata(ctx, km); err != nil {
		return fmt.Errorf("keystore: update key_metadata: %w", err)
	}
	invokeFault("s2_post_update")

	dekCopy := current.DEK.Bytes()
	newSnap := &KeySnapshot{
		KEK:        crypto.NewKeyBytes(newKEK.Bytes()),
		DEK:        crypto.NewKeyBytes(dekCopy),
		KekVersion: uint64(newPendingVer),
		DekVersion: current.DekVersion,
		CryptoMode: current.CryptoMode,
	}
	newKEK.Zero()

	r.manager.Swap(newSnap)
	invokeFault("s2_post_swap")

	return nil
}

// CompleteKEKRotation is the Phase B inline completion after BeginKEKRotation.
//
// Steps:
//  1. Wait for pre-swap CRUDs to drain
//  2. Bulk-update kek_version column on all credentials rows
//  3. Update key_metadata: clear pending fields, promote pending→active
//  4. Clear previous snapshot (DEK unchanged, no Phase A needed)
func (r *Rotator) CompleteKEKRotation(ctx context.Context, timeout time.Duration) error {
	if !r.adminRotationMu.TryLock() {
		return ErrRotationInProgress
	}
	defer r.adminRotationMu.Unlock()

	km, err := r.store.GetKeyMetadata(ctx)
	if err != nil {
		return fmt.Errorf("keystore: read key_metadata: %w", err)
	}
	if km.PendingKekVersion == 0 {
		return fmt.Errorf("keystore: no pending KEK rotation to complete")
	}
	if km.PendingDekVersion != 0 {
		return fmt.Errorf("keystore: pending DEK rotation exists; complete that first")
	}

	drainCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	if err := r.manager.WaitInflightDrained(drainCtx); err != nil {
		return fmt.Errorf("keystore: wait drain: %w", err)
	}

	newActive := km.PendingKekVersion
	if _, err := r.store.BulkUpdateKekVersion(ctx, newActive); err != nil {
		return fmt.Errorf("keystore: bulk update kek_version: %w", err)
	}
	invokeFault("kek_complete_post_bulk")

	km.ActiveKekVersion = km.PendingKekVersion
	km.WrappedDEK = km.PendingWrappedDEK
	// Empty pending_config_shard → keep the original active_config_shard
	// (which the new KEK was derived with). Non-empty pending means an S2
	// rotation ran during the KEK rotation, and the new S2 should now be
	// active.
	if len(km.PendingConfigShard) > 0 {
		km.ActiveConfigShard = km.PendingConfigShard
	}
	km.PendingKekVersion = 0
	km.PendingWrappedDEK = []byte{}
	km.PendingConfigShard = []byte{}
	if err := r.store.UpdateKeyMetadata(ctx, km); err != nil {
		return fmt.Errorf("keystore: update key_metadata: %w", err)
	}
	invokeFault("kek_complete_post_meta")

	r.manager.ClearPrevious()

	return nil
}

// PhaseA_ReencryptAll re-encrypts all credentials with dek_version < targetDek,
// batch by batch, until no rows match or maxPhaseALoops is reached.
//
// Returns:
//   - rowsReencrypted: total count
//   - loopsUsed: number of batch iterations executed
//   - error if max loops exhausted with rows remaining
func (r *Rotator) PhaseA_ReencryptAll(ctx context.Context, targetDek int64, maxRowsPerTx int64, maxLoops int) (int64, int, error) {
	var (
		total       int64
		loopCount   int
		oldDEKBytes []byte
		newDEKBytes []byte
		mode        crypto.Mode
	)

	for loop := 0; loop < maxLoops; loop++ {
		if err := ctx.Err(); err != nil {
			return total, loopCount, fmt.Errorf("keystore: Phase A cancelled: %w", err)
		}
		invokeFault("phasea_loop_start")
		loopCount++

		oldSnap := r.manager.Previous()
		newSnap := r.manager.Current()
		if oldSnap == nil || newSnap == nil {
			return total, loopCount, fmt.Errorf("keystore: missing snapshot for Phase A (old=%v new=%v)", oldSnap != nil, newSnap != nil)
		}
		oldDEKBytes = oldSnap.DEK.Bytes()
		newDEKBytes = newSnap.DEK.Bytes()
		mode = newSnap.CryptoMode

		n, err := r.store.ReencryptDekVersionBatch(ctx, oldDEKBytes, newDEKBytes, mode, targetDek, maxRowsPerTx)
		if err != nil {
			if errors.Is(err, store.ErrPhaseADecryptFail) {
				slog.Error("Phase A decrypt fail — data corruption detected", "loop", loop+1, "err", err.Error())
				return total, loopCount, fmt.Errorf("keystore: Phase A decrypt fail: %w", err)
			}
			return total, loopCount, fmt.Errorf("keystore: Phase A batch %d: %w", loop+1, err)
		}
		total += n
		if n == 0 {
			return total, loopCount, nil
		}
	}
	return total, loopCount, fmt.Errorf("keystore: Phase A exhausted %d loops, rows may remain", maxLoops)
}

// BeginDEKRotation starts a DEK rotation. A fresh DEK is generated; the existing KEK
// wraps it. The DB is updated with pending fields, then the Manager swaps so that
// new CRUDs use the new DEK (the Manager.Previous() snapshot keeps the old DEK so
// pre-swap CRUDs and old-version rows can still decrypt via Phase A / try-2).
func (r *Rotator) BeginDEKRotation(ctx context.Context) error {
	km, current, release, err := r.beginRotation(ctx)
	if err != nil {
		return err
	}
	defer release()

	mode := current.CryptoMode

	newDEK, err := crypto.NewRandomKey(16)
	if err != nil {
		return fmt.Errorf("keystore: generate DEK: %w", err)
	}

	newWrappedDEK, err := crypto.WrapDEK(mode, current.KEK.Bytes(), newDEK.Bytes())
	if err != nil {
		newDEK.Zero()
		return fmt.Errorf("keystore: wrap new DEK: %w", err)
	}

	newPendingVer := km.ActiveDekVersion + 1
	km.PendingDekVersion = newPendingVer
	km.PendingWrappedDEK = newWrappedDEK
	km.DekRotatedAt = time.Now().Unix()
	if r.backupMgr != nil {
		s1Path := S1ShardPath(r.secretsDir, km.FileShardVersion)
		s1File, s1Err := LoadS1FromFile(s1Path)
		if s1Err != nil {
			newDEK.Zero()
			return fmt.Errorf("keystore: load active S1 shard for DEK backup: %w", s1Err)
		}
		var s2File [ShardSize]byte
		copy(s2File[:], km.ActiveConfigShard)
		if err := r.backupMgr.Backup(ctx, backup.BackupDEK, byte(current.CryptoMode), s1File, s2File, newWrappedDEK); err != nil {
			newDEK.Zero()
			return fmt.Errorf("keystore: backup: %w", err)
		}
	}
	invokeFault("dek_post_backup")

	if err := r.store.UpdateKeyMetadata(ctx, km); err != nil {
		newDEK.Zero()
		return fmt.Errorf("keystore: update key_metadata: %w", err)
	}
	invokeFault("dek_post_update")

	oldSnap := current
	newSnap := &KeySnapshot{
		KEK:        crypto.NewKeyBytes(oldSnap.KEK.Bytes()),
		DEK:        crypto.NewKeyBytes(newDEK.Bytes()),
		KekVersion: oldSnap.KekVersion,
		DekVersion: uint64(newPendingVer),
		CryptoMode: oldSnap.CryptoMode,
	}
	r.manager.Swap(newSnap)
	invokeFault("dek_post_swap")

	return nil
}

// CompleteDEKRotation runs Phase A inline (re-encrypt all credentials to new DEK)
// then promotes pending DEK metadata to active and clears the previous snapshot.
func (r *Rotator) CompleteDEKRotation(ctx context.Context, timeout time.Duration, maxRowsPerTx int64, maxPhaseALoops int) error {
	if !r.adminRotationMu.TryLock() {
		return ErrRotationInProgress
	}
	defer r.adminRotationMu.Unlock()

	km, err := r.store.GetKeyMetadata(ctx)
	if err != nil {
		return fmt.Errorf("keystore: read key_metadata: %w", err)
	}
	if km.PendingDekVersion == 0 {
		return fmt.Errorf("keystore: no pending DEK rotation to complete")
	}
	if km.PendingKekVersion != 0 {
		return fmt.Errorf("keystore: pending KEK rotation exists; complete that first")
	}

	total, err := r.runDEKPhaseAConvergence(ctx, km.PendingDekVersion, timeout, maxRowsPerTx, maxPhaseALoops)
	if err != nil {
		return fmt.Errorf("keystore: convergence: %w", err)
	}
	slog.Info("DEK rotation Phase A complete", "reencrypted_rows", total, "target_dek_version", km.PendingDekVersion)
	invokeFault("dek_complete_post_phasea")

	now := time.Now().Unix()
	km.ActiveDekVersion = km.PendingDekVersion
	km.WrappedDEK = km.PendingWrappedDEK
	km.LastRotateAt = now
	km.PendingDekVersion = 0
	km.PendingWrappedDEK = []byte{}
	if err := r.store.UpdateKeyMetadata(ctx, km); err != nil {
		return fmt.Errorf("keystore: update key_metadata: %w", err)
	}
	invokeFault("dek_complete_post_meta")

	r.manager.ClearPrevious()

	return nil
}

// runDEKPhaseAConvergence implements the Phase A convergence loop:
//
//	loop {
//	  (a) PhaseA_ReencryptAll — process all old-version rows
//	  (b) DrainCheck — if not drained, WaitInflightDrained then re-run Phase A
//	  (c) StaleCheck — if rows remain, re-run Phase A
//	  break
//	}
//
// Bound by maxPhaseALoops; ≤2 iterations expected in steady state.
// Phase A decrypt failures are fatal to the rotation: returned as wrapped
// store.ErrPhaseADecryptFail; callers map this to exit 78 / HTTP 500.
func (r *Rotator) runDEKPhaseAConvergence(ctx context.Context, targetDek int64, timeout time.Duration, maxRowsPerTx int64, maxPhaseALoops int) (int64, error) {
	for loop := 0; loop < maxPhaseALoops; loop++ {
		if err := ctx.Err(); err != nil {
			return 0, fmt.Errorf("keystore: cancelled: %w", err)
		}

		// (a) Phase A single pass — process all batches of old-version rows
		// Use maxPhaseALoops * 100 as a generous inner batch bound so that
		// one pass can drain all rows without falsely hitting the limit.
		total, _, err := r.PhaseA_ReencryptAll(ctx, targetDek, maxRowsPerTx, maxPhaseALoops*100)
		if err != nil {
			return total, fmt.Errorf("phase A pass %d: %w", loop+1, err)
		}

		// (b) Drain check — have all pre-swap CRUDs finished?
		if !r.manager.DrainCheck() {
			drainCtx, drainCancel := context.WithTimeout(ctx, timeout)
			defer drainCancel()
			if err := r.manager.WaitInflightDrained(drainCtx); err != nil {
				return total, fmt.Errorf("drain after pass %d: %w", loop+1, err)
			}
			continue // re-run Phase A: late-arriving pre-swap writes may need re-encrypt
		}

		// (c) Stale check — any rows still at old dek_version?
		stale, err := r.store.CountStragglersByDekVersion(ctx, targetDek)
		if err != nil {
			return total, fmt.Errorf("stale check pass %d: %w", loop+1, err)
		}
		if stale > 0 {
			continue // re-run Phase A: new writes after our scan need re-encrypt
		}

		// Both checks passed — converged
		return total, nil
	}
	return 0, fmt.Errorf("keystore: Phase A convergence exhausted %d loops", maxPhaseALoops)
}

// RunStartupConvergence performs the post-recovery convergence work after
// RecoverFromState + LoadFromDir. It runs the appropriate Phase A or bulk
// UPDATE depending on what was promoted during recovery.
//
// For KEK recovery: calls BulkUpdateKekVersion in a loop until no rows match.
// For DEK recovery: sets up prev/current dual snapshots and runs the
//
//	convergence loop (runDEKPhaseAConvergence).
//
// Acquires adminRotationMu for the entire convergence: during recovery, any
// concurrent admin Begin/Complete attempt will fail with ErrRotationInProgress
// because their internal TryLock fails. This is the intended behavior —
// recovery owns the rotation state until Phase A converges and the prev
// snapshot is cleared. Must be called after the Manager has been loaded
// (LoadFromDir or SelfInit).
func (r *Rotator) RunStartupConvergence(ctx context.Context, res *RecoveryResult, timeout time.Duration, maxRowsPerTx int64, maxPhaseALoops int) (err error) {
	r.adminRotationMu.Lock()
	defer r.adminRotationMu.Unlock()

	r.setConvergence(convergeRunning, nil)
	defer func() {
		if err != nil {
			r.setConvergence(convergeFailed, err)
		} else {
			r.setConvergence(convergeCompleted, nil)
		}
	}()
	switch res.Case {
	case RecoveryCase1Clean:
		return nil // nothing to converge

	case RecoveryCase4KEKForwardS1, RecoveryCase5KEKForwardS2:
		if res.PromotedKekVersion == 0 {
			return nil
		}
		for i := 0; i < maxPhaseALoops; i++ {
			if err := ctx.Err(); err != nil {
				return fmt.Errorf("keystore: startup KEK convergence cancelled: %w", err)
			}
			n, err := r.store.BulkUpdateKekVersion(ctx, res.PromotedKekVersion)
			if err != nil {
				return fmt.Errorf("keystore: startup KEK convergence: %w", err)
			}
			if n == 0 {
				return nil
			}
		}
		return fmt.Errorf("keystore: startup KEK convergence exhausted %d loops", maxPhaseALoops)

	case RecoveryCase7DEKForward:
		if res.PromotedDekVersion == 0 || len(res.OldWrappedDEK) == 0 {
			return nil
		}
		cur := r.manager.Current()
		if cur == nil {
			return fmt.Errorf("keystore: startup DEK convergence: no current snapshot")
		}
		// Unwrap old wrapped_dek to get the old DEK for prev snapshot
		oldDEK, err := crypto.UnwrapDEK(cur.CryptoMode, cur.KEK.Bytes(), res.OldWrappedDEK)
		if err != nil {
			return fmt.Errorf("keystore: startup DEK convergence: unwrap old DEK: %w", err)
		}
		prevVer := res.PromotedDekVersion - 1
		prev := &KeySnapshot{
			KEK:        crypto.NewKeyBytes(cur.KEK.Bytes()),
			DEK:        crypto.NewKeyBytes(oldDEK),
			KekVersion: cur.KekVersion,
			DekVersion: uint64(prevVer),
			CryptoMode: cur.CryptoMode,
		}
		// Install dual snap: current stays as-is, previous holds old DEK
		r.manager.InstallDualSnap(cur, prev)

		total, err := r.runDEKPhaseAConvergence(ctx, res.PromotedDekVersion, timeout, maxRowsPerTx, maxPhaseALoops)
		if err != nil {
			return fmt.Errorf("keystore: startup DEK convergence: %w", err)
		}
		slog.Info("DEK rotation startup convergence complete", "reencrypted_rows", total, "promoted_dek_version", res.PromotedDekVersion)
		r.manager.ClearPrevious()
		return nil

	default:
		// Nested or unknown — should have been caught earlier; no convergence needed
		return nil
	}
}

// StartAutoRotate runs a background ticker that auto-rotates the DEK every `period`.
// Returns a stop function that cancels the goroutine.
// On fresh installs the gate is intentionally satisfied by SelfInit seeding
// dek_rotated_at=now, so the first auto-rotate is a silent no-op and the
// first real rotation happens `period` after install. Rotating a DEK that
// was just generated has no security benefit.
func (r *Rotator) StartAutoRotate(ctx context.Context, period time.Duration, drainTimeout time.Duration, maxRowsPerTx int64, maxPhaseALoops int) (stop func()) {
	tickerCtx, cancel := context.WithCancel(ctx)
	go func() {
		_ = r.runAutoRotate(tickerCtx, period, drainTimeout, maxRowsPerTx, maxPhaseALoops)
		t := time.NewTicker(period)
		defer t.Stop()
		for {
			select {
			case <-tickerCtx.Done():
				return
			case <-t.C:
				_ = r.runAutoRotate(tickerCtx, period, drainTimeout, maxRowsPerTx, maxPhaseALoops)
			}
		}
	}()
	return cancel
}

func (r *Rotator) runAutoRotate(ctx context.Context, period time.Duration, drainTimeout time.Duration, maxRowsPerTx int64, maxPhaseALoops int) error {
	km, err := r.store.GetKeyMetadata(ctx)
	if err != nil {
		slog.Error("auto-rotate key_metadata error", "err", err.Error())
		return err
	}
	// Resume any rotation left pending by a previous tick. Pending KEK and
	// pending DEK are both safe to auto-complete: the Complete* methods are
	// idempotent and serialised by adminRotationMu, so they either push the
	// DB to a consistent state or return an error that the next tick
	// retries. This avoids the system stabilising in a half-rotated state
	// after a transient failure.
	if km.PendingKekVersion > 0 {
		slog.Info("auto-rotate resuming pending KEK rotation", "pending_kek", km.PendingKekVersion)
		if err := r.CompleteKEKRotation(ctx, drainTimeout); err != nil {
			slog.Error("auto-rotate KEK resume failed", "err", err.Error())
			return err
		}
		slog.Info("auto-rotate KEK resumed and completed")
		return nil
	}
	if km.PendingDekVersion > 0 {
		slog.Info("auto-rotate resuming pending DEK rotation", "pending_dek", km.PendingDekVersion)
		if err := r.CompleteDEKRotation(ctx, drainTimeout, maxRowsPerTx, maxPhaseALoops); err != nil {
			slog.Error("auto-rotate DEK resume failed", "err", err.Error())
			return err
		}
		slog.Info("auto-rotate DEK resumed and completed")
		return nil
	}
	if km.DekRotatedAt > 0 && time.Since(time.Unix(km.DekRotatedAt, 0)) < period {
		slog.Info("auto-rotate skip", "reason", "gate", "dek_rotated_at", km.DekRotatedAt, "period", period)
		return nil
	}
	if err := r.BeginDEKRotation(ctx); err != nil {
		slog.Error("auto-rotate begin failed", "err", err.Error())
		return err
	}
	if err := r.CompleteDEKRotation(ctx, drainTimeout, maxRowsPerTx, maxPhaseALoops); err != nil {
		slog.Error("auto-rotate complete failed", "err", err.Error())
		return err
	}
	slog.Info("auto-rotate completed")
	return nil
}
