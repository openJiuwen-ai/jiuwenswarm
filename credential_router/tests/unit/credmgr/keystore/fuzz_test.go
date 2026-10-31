//go:build cgo

// Fuzz tests for internal/keystore.
//
// Quick:  go test -run=^$ -fuzz=^Fuzz -fuzztime=10s ./tests/unit/keystore/...
// Full:   go test -run=^$ -fuzz=^Fuzz -fuzztime=60s ./tests/unit/keystore/...
//
// The recovery target is the important one: RecoverFromState is the case
// detector. Fuzzing feeds it arbitrary key_metadata rows plus
// arbitrary on-disk state (presence + mtime of orphan s1.bin.2) and asserts:
//   - it never panics and never returns a nil result
//   - a nil error always yields a case in the valid 1, 4, 5, 7 range, with a
//     DB-after-state consistent with the detected case (pending fields are
//     always cleared on success; promotions write the pending version into
//     the active slot; rollback leaves the active slot untouched)
//   - a non-nil error is a clean error (ErrNestedRotation is the nested-rotation
//     invariant and must pair with RecoveryCaseNested)
//
// DeriveKEK is fuzzed for determinism, 16-byte output and avalanche.
package keystore_test

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"credential_router/internal/credmgr/crypto"
	"credential_router/internal/credmgr/keystore"
	"credential_router/internal/credmgr/store"
)

// ---------- DeriveKEK ----------

// FuzzDeriveKEK — S1+S2 are 32-byte shards; the mode selects the PRF
// (SHA-256 for "aes", SM3 for "sm"). The derived KEK must always be 16 bytes,
// deterministic for identical shards, and avalanche-sensitive to a single
// shard byte flip. Unknown modes must error cleanly.
func FuzzDeriveKEK(f *testing.F) {
	known := make([]byte, 1+2*keystore.ShardSize)
	copy(known[1:1+keystore.ShardSize], bytes.Repeat([]byte{0x11}, keystore.ShardSize)) // S1
	copy(known[1+keystore.ShardSize:], bytes.Repeat([]byte{0x22}, keystore.ShardSize))  // S2

	f.Add([]byte{})                                                                  // empty → zero shards, aes
	f.Add([]byte{0x00})                                                              // aes, zero shards
	f.Add([]byte{0x01})                                                              // sm, zero shards
	f.Add(known)                                                                     // known-valid shards, aes
	f.Add(bytes.Repeat([]byte{0xff}, 1+2*keystore.ShardSize))                        // all-ones shards, sm
	f.Add(append([]byte{0x7f}, bytes.Repeat([]byte{0xaa}, 2*keystore.ShardSize)...)) // invalid mode
	f.Fuzz(func(t *testing.T, data []byte) {
		buf := make([]byte, 1+2*keystore.ShardSize)
		copy(buf, data)

		var s1, s2 [keystore.ShardSize]byte
		copy(s1[:], buf[1:1+keystore.ShardSize])
		copy(s2[:], buf[1+keystore.ShardSize:])

		mode := recMode(buf[0])
		if mode != crypto.ModeAES && mode != crypto.ModeSM4 {
			if _, err := keystore.DeriveKEK(s1, s2, mode); err == nil {
				t.Fatalf("DeriveKEK(%v) with unknown mode must error", mode)
			}
			return
		}

		k1, err := keystore.DeriveKEK(s1, s2, mode)
		if err != nil {
			t.Fatalf("DeriveKEK(%q) failed: %v", mode, err)
		}
		if k1 == nil || k1.Len() != crypto.KEKSize {
			t.Fatalf("KEK len=%v, want %d", k1, crypto.KEKSize)
		}

		// Determinism.
		k2, err := keystore.DeriveKEK(s1, s2, mode)
		if err != nil {
			t.Fatalf("second derivation failed: %v", err)
		}
		if !bytes.Equal(k1.Bytes(), k2.Bytes()) {
			t.Fatalf("DeriveKEK(%q) not deterministic", mode)
		}

		// Avalanche: flip one byte of S1 → different KEK.
		s1f := s1
		s1f[0] ^= 0x01
		k3, err := keystore.DeriveKEK(s1f, s2, mode)
		if err != nil {
			t.Fatalf("mutated derivation failed: %v", err)
		}
		if bytes.Equal(k1.Bytes(), k3.Bytes()) {
			t.Fatalf("avalanche violated: single S1 byte flip produced identical KEK")
		}
	})
}

// ---------- RecoverFromState ----------

// Recovery fuzz seed layout (fixed offsets, zero-padded when the input is
// short):
//
//	[0:8]   ActiveKekVersion   int64 LE
//	[8:16]  PendingKekVersion  int64 LE
//	[16:24] ActiveDekVersion   int64 LE
//	[24:32] PendingDekVersion  int64 LE
//	[32]    CryptoMode byte    (0x01=AES, 0x02=SM4, any other = unknown Mode)
//	[33:65] ActiveConfigShard  [32]byte
//	[65:97] PendingConfigShard [32]byte
//	[97]    flags byte         bit0 = create orphan s1.bin.2 on disk
//	                          bit1 = use valid key material (see below)
//	[98]    mtimeDelta int8    orphan mtime = km.UpdatedAt + delta (seconds)
//	[99:]   wrapped payload    split in half: WrappedDEK | PendingWrappedDEK
//
// bit1 ("use valid material") replaces the fuzz shards/wrapped DEKs with a
// precomputed self-consistent set (S1 on disk + S2 + wrapped DEK derived from
// them), so the seeds can reach the full happy path where recovery succeeds
// AND the snapshot builds. When bit1 is clear, every byte is arbitrary —
// including impossible rows like pending both KEK and DEK.
const (
	recOffActiveKEK    = 0
	recOffPendingKEK   = 8
	recOffActiveDEK    = 16
	recOffPendingDEK   = 24
	recOffCryptoMode   = 32
	recOffActiveShard  = 33
	recOffPendingShard = 65
	recOffFlags        = 97
	recOffMtimeDelta   = 98
	recOffWrapped      = 99

	recFlagHasNew   = 0x01
	recFlagValidMat = 0x02
)

// Valid key material shared by the recovery seeds. Lazily derived once: the
// harness S1 file on disk pairs with recS2 to produce the KEK that wrapped
// the valid DEK. Two wrapped variants (AES/SM4) cover both crypto modes.
var (
	recS1, recS2       [keystore.ShardSize]byte
	recDEK             [DEKSize]byte
	recValidWrappedAES []byte
	recValidWrappedSM  []byte
	recValidShard      []byte
)

// DEKSize mirrors crypto.DEKSize (16). Defined here to keep the material
// builder independent of keystore's internal constants.
const DEKSize = 16

func initRecoveryMaterial() {
	if recValidWrappedAES != nil {
		return
	}
	for i := range recS1 {
		recS1[i] = byte(i)
	}
	for i := range recS2 {
		recS2[i] = byte(i + 100)
	}
	for i := range recDEK {
		recDEK[i] = byte(i + 200)
	}
	ka, err := keystore.DeriveKEK(recS1, recS2, crypto.ModeAES)
	if err != nil {
		panic(err)
	}
	recValidWrappedAES, err = crypto.WrapDEK(crypto.ModeAES, ka.Bytes(), recDEK[:])
	if err != nil {
		panic(err)
	}
	ka.Zero()
	ks, err := keystore.DeriveKEK(recS1, recS2, crypto.ModeSM4)
	if err != nil {
		panic(err)
	}
	recValidWrappedSM, err = crypto.WrapDEK(crypto.ModeSM4, ks.Bytes(), recDEK[:])
	if err != nil {
		panic(err)
	}
	ks.Zero()
	recValidShard = append([]byte(nil), recS2[:]...)
}

// recMode maps the layout's mode byte to a CryptoMode enum.
func recMode(b byte) crypto.Mode {
	switch b {
	case 0x01:
		return crypto.ModeAES
	case 0x02:
		return crypto.ModeSM4
	default:
		return crypto.Mode(b)
	}
}

// recSeed builds a fuzz seed byte slice from explicit recovery state.
func recSeed(activeKEK, pendingKEK, activeDEK, pendingDEK int64, modeByte byte, activeShard, pendingShard []byte, hasNew, useValid bool, mtimeDelta int8, wrapped []byte) []byte {
	out := make([]byte, recOffWrapped+len(wrapped))
	binary.LittleEndian.PutUint64(out[recOffActiveKEK:recOffPendingKEK], uint64(activeKEK))
	binary.LittleEndian.PutUint64(out[recOffPendingKEK:recOffActiveDEK], uint64(pendingKEK))
	binary.LittleEndian.PutUint64(out[recOffActiveDEK:recOffPendingDEK], uint64(activeDEK))
	binary.LittleEndian.PutUint64(out[recOffPendingDEK:recOffCryptoMode], uint64(pendingDEK))
	out[recOffCryptoMode] = modeByte
	copy(out[recOffActiveShard:recOffPendingShard], activeShard)
	copy(out[recOffPendingShard:recOffFlags], pendingShard)
	if hasNew {
		out[recOffFlags] |= recFlagHasNew
	}
	if useValid {
		out[recOffFlags] |= recFlagValidMat
	}
	out[recOffMtimeDelta] = byte(mtimeDelta)
	copy(out[recOffWrapped:], wrapped)
	return out
}

// parseRecoveryData decodes a fuzz input into a KeyMetadata row plus the
// on-disk controls. FileShardVersion is pinned to 1 (the SelfInit default)
// so buildSnapshot never reads arbitrary system paths.
func parseRecoveryData(data []byte, secretsDir string) (km *store.KeyMetadata, hasNew, useValid bool, mtimeDelta int8) {
	buf := make([]byte, recOffWrapped)
	copy(buf, data)

	km = &store.KeyMetadata{
		ActiveKekVersion:   int64(binary.LittleEndian.Uint64(buf[recOffActiveKEK:recOffPendingKEK])),
		PendingKekVersion:  int64(binary.LittleEndian.Uint64(buf[recOffPendingKEK:recOffActiveDEK])),
		ActiveDekVersion:   int64(binary.LittleEndian.Uint64(buf[recOffActiveDEK:recOffPendingDEK])),
		PendingDekVersion:  int64(binary.LittleEndian.Uint64(buf[recOffPendingDEK:recOffCryptoMode])),
		CryptoMode:         recMode(buf[recOffCryptoMode]),
		FileShardVersion:   1,
		FileShardRotatedAt: 0,
		LastRotateAt:       0,
		DekRotatedAt:       0,
	}
	km.ActiveConfigShard = append([]byte(nil), buf[recOffActiveShard:recOffPendingShard]...)
	km.PendingConfigShard = append([]byte(nil), buf[recOffPendingShard:recOffFlags]...)

	flags := buf[recOffFlags]
	hasNew = flags&recFlagHasNew != 0
	useValid = flags&recFlagValidMat != 0
	mtimeDelta = int8(buf[recOffMtimeDelta])

	var rest []byte
	if len(data) > recOffWrapped {
		rest = data[recOffWrapped:]
	}
	half := len(rest) / 2
	// Empty (non-nil) slices: nil binds as SQL NULL, violating NOT NULL columns.
	km.WrappedDEK = append([]byte{}, rest[:half]...)
	km.PendingWrappedDEK = append([]byte{}, rest[half:]...)

	// When valid material is requested, replace the arbitrary shards and
	// wrapped DEKs with the self-consistent set for the parsed mode. This lets
	// the seed corpus exercise the full happy path (recovery + snapshot build).
	if useValid {
		switch km.CryptoMode {
		case crypto.ModeAES:
			km.WrappedDEK = append([]byte(nil), recValidWrappedAES...)
			km.PendingWrappedDEK = append([]byte(nil), recValidWrappedAES...)
		case crypto.ModeSM4:
			km.WrappedDEK = append([]byte(nil), recValidWrappedSM...)
			km.PendingWrappedDEK = append([]byte(nil), recValidWrappedSM...)
		}
		km.ActiveConfigShard = append([]byte(nil), recValidShard...)
		km.PendingConfigShard = append([]byte(nil), recValidShard...)
	}
	return km, hasNew, useValid, mtimeDelta
}

// Shared recovery harness: one store + secrets dir per worker process. Each
// iteration resets the singleton row, restores the canonical S1 file, and
// controls the orphan s1.bin.2 presence, so inputs stay isolated while skipping
// the fresh-store migration cost on every call.
var (
	recHarnessOnce   sync.Once
	recHarnessStore  *store.Store
	recHarnessDir    string
	recHarnessS1Path string
)

func recHarness() (*store.Store, string) {
	recHarnessOnce.Do(func() {
		dir, err := os.MkdirTemp("", "recfuzz")
		if err != nil {
			panic(err)
		}
		recHarnessDir = dir
		recHarnessS1Path = filepath.Join(dir, "s1.bin.1")
		if err := os.WriteFile(recHarnessS1Path, recS1[:], 0o600); err != nil {
			panic(err)
		}
		recHarnessStore, err = store.OpenForTesting(filepath.Join(dir, "creds.db"))
		if err != nil {
			panic(err)
		}
	})
	return recHarnessStore, recHarnessDir
}

// FuzzRecoverFromState — arbitrary key_metadata row + arbitrary disk state.
// The case detector must never panic, never return a nil result, and
// must either reconcile the DB to a consistent post-recovery state (nil error
// → case ∈ 1..7) or return a clean error.
func FuzzRecoverFromState(f *testing.F) {
	initRecoveryMaterial()

	// Known-valid seed states, one per recovery case. bit1 keeps the key material
	// self-consistent so successful recoveries also build a snapshot.
	f.Add(recSeed(1, 0, 1, 0, 0x00, recValidShard, recValidShard, false, true, 0, nil)) // Case 1 clean
	f.Add(recSeed(1, 0, 1, 0, 0x00, recValidShard, recValidShard, true, true, 0, nil))  // Case 2 orphan .new
	f.Add(recSeed(1, 2, 1, 0, 0x00, recValidShard, nil, false, true, 0, nil))           // Case 3 KEK rollback
	f.Add(recSeed(1, 2, 1, 0, 0x00, recValidShard, nil, true, true, 0, nil))            // Case 4 KEK forward S1
	f.Add(recSeed(1, 2, 1, 0, 0x00, recValidShard, recValidShard, false, true, 0, nil)) // Case 5 KEK forward S2
	f.Add(recSeed(1, 2, 1, 0, 0x00, recValidShard, recValidShard, true, true, -1, nil)) // Case 6 stale .new → Case 5
	f.Add(recSeed(1, 2, 1, 0, 0x00, recValidShard, recValidShard, true, true, 0, nil))  // Case 6 .new newer → FATAL error
	f.Add(recSeed(1, 0, 1, 2, 0x00, recValidShard, recValidShard, false, true, 0, nil)) // Case 7 DEK forward
	f.Add(recSeed(1, 2, 1, 2, 0x00, recValidShard, recValidShard, false, true, 0, nil)) // Nested rotation → ErrNestedRotation
	f.Add(recSeed(1, 0, 1, 0, 0x01, recValidShard, recValidShard, false, true, 0, nil)) // Case 1 clean, sm mode
	f.Add([]byte{})                                                                     // arbitrary empty state
	f.Add(recSeed(1, 2, 1, 0, 0x7f, nil, nil, false, false, 0, nil))                    // invalid mode + pending KEK
	f.Add(bytes.Repeat([]byte{0xff}, 500))                                              // max-length garbage

	f.Fuzz(func(t *testing.T, data []byte) {
		ctx := context.Background()
		s, secretsDir := recHarness()
		s1Path := filepath.Join(secretsDir, "s1.bin.1")
		if err := os.WriteFile(s1Path, recS1[:], 0o600); err != nil {
			t.Fatal(err)
		}

		km, hasNew, useValid, mtimeDelta := parseRecoveryData(data, secretsDir)
		before := *km

		// Reset the singleton row, then insert the fuzz-controlled state via
		// raw SQL so invalid modes (per fuzz input) reach the recovery path.
		if _, err := s.AdminDB().ExecContext(ctx, `DELETE FROM key_metadata`); err != nil {
			t.Fatal(err)
		}
		if _, err := s.AdminDB().ExecContext(ctx, `INSERT INTO key_metadata (
			id, active_kek_version, pending_kek_version, active_dek_version, pending_dek_version,
			crypto_mode, active_config_shard, pending_config_shard, file_shard_version,
			file_shard_rotated_at, last_rotate_at, wrapped_dek, pending_wrapped_dek,
			dek_rotated_at, updated_at
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			1, km.ActiveKekVersion, km.PendingKekVersion, km.ActiveDekVersion, km.PendingDekVersion,
			km.CryptoMode.String(), km.ActiveConfigShard, km.PendingConfigShard, km.FileShardVersion,
			km.FileShardRotatedAt, km.LastRotateAt, km.WrappedDEK, km.PendingWrappedDEK,
			km.DekRotatedAt, km.UpdatedAt); err != nil {
			t.Fatal(err)
		}

		newPath := filepath.Join(secretsDir, "s1.bin.2")
		if hasNew {
			content := []byte(data)
			if useValid {
				content = recS1[:] // valid S1 for the new KEK after rename
			}
			if err := os.WriteFile(newPath, content, 0o600); err != nil {
				t.Fatal(err)
			}
			mt := time.Unix(km.UpdatedAt, 0).Add(time.Duration(mtimeDelta) * time.Second)
			if err := os.Chtimes(newPath, mt, mt); err != nil {
				t.Fatal(err)
			}
		} else if err := os.Remove(newPath); err != nil && !os.IsNotExist(err) {
			t.Fatal(err)
		}

		res, rerr := keystore.RecoverFromState(ctx, secretsDir, s)
		if res == nil && rerr == nil {
			t.Fatalf("RecoverFromState returned (nil, nil)")
		}

		if rerr == nil {
			if res.Case < keystore.RecoveryCase1Clean || res.Case > keystore.RecoveryCase7DEKForward {
				t.Fatalf("nil-error recovery returned out-of-range case %d", res.Case)
			}
			after, gerr := s.GetKeyMetadata(ctx)
			if gerr != nil {
				t.Fatalf("re-fetch key_metadata: %v", gerr)
			}

			// A successful recovery must never leave nested pending.
			if after.PendingKekVersion > 0 && after.PendingDekVersion > 0 {
				t.Fatalf("successful recovery left nested pending (kek=%d dek=%d)", after.PendingKekVersion, after.PendingDekVersion)
			}

			if before.PendingKekVersion > 0 && before.PendingDekVersion == 0 {
				if after.PendingKekVersion != 0 {
					t.Fatalf("KEK recovery did not clear pending_kek (after=%d)", after.PendingKekVersion)
				}
				switch res.Case {
				case keystore.RecoveryCase4KEKForwardS1, keystore.RecoveryCase5KEKForwardS2:
					if after.ActiveKekVersion != before.PendingKekVersion {
						t.Fatalf("KEK promote set active_kek=%d, want %d", after.ActiveKekVersion, before.PendingKekVersion)
					}
				}
			}

			// DEK pending: must promote to Case 7 with the pending version.
			if before.PendingDekVersion > 0 && before.PendingKekVersion == 0 {
				if res.Case != keystore.RecoveryCase7DEKForward {
					t.Fatalf("pending DEK but recovery case=%d", res.Case)
				}
				if after.PendingDekVersion != 0 {
					t.Fatalf("DEK recovery did not clear pending_dek (after=%d)", after.PendingDekVersion)
				}
				if after.ActiveDekVersion != before.PendingDekVersion {
					t.Fatalf("DEK promote set active_dek=%d, want %d", after.ActiveDekVersion, before.PendingDekVersion)
				}
			}

			// Nothing pending: clean (Case 1).
			if before.PendingKekVersion == 0 && before.PendingDekVersion == 0 {
				if res.Case != keystore.RecoveryCase1Clean {
					t.Fatalf("no pending versions but recovery case=%d", res.Case)
				}
			}

			// A successful recovery must never modify the on-disk shard files — the
			// new design has no rename / delete in the recovery path. Orphans
			// (hasNew=true) survive every recovery case unchanged.
			if before.PendingKekVersion == 0 && before.PendingDekVersion == 0 {
				if hasNew {
					if _, serr := os.Stat(newPath); serr != nil {
						t.Fatalf("recovery unexpectedly removed orphan shard file (case=%d): %v", res.Case, serr)
					}
				}
			}
		} else {
			// Clean error: the nested-rotation error must pair with RecoveryCaseNested.
			if errors.Is(rerr, keystore.ErrNestedRotation) && res.Case != keystore.RecoveryCaseNested {
				t.Fatalf("ErrNestedRotation returned with case=%d, want %d", res.Case, keystore.RecoveryCaseNested)
			}
		}
	})
}
