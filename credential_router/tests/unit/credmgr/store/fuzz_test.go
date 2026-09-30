//go:build cgo

// Fuzz tests for the store package (SQLite persistence layer).
//
// Quick:  go test -run=^$ -fuzz=^Fuzz -fuzztime=10s ./tests/unit/store/...
// Full:   go test -run=^$ -fuzz=^Fuzz -fuzztime=60s ./tests/unit/store/...
//
// Every target opens a fresh temp SQLite per iteration (t.TempDir() +
// store.OpenForTesting, closed in t.Cleanup) so no fuzz input can leak rows into a
// later one. Round-trip equality across user_id / api_base / key_tag /
// api_key_cipher / auth_type proves the SQL is parameterized: injection-style
// inputs (e.g. `'; DROP TABLE credentials;--`) are stored as literal data and
// returned verbatim. Null bytes, invalid UTF-8 and emoji are all legal in
// SQLite TEXT/BLOB and must round-trip without crashing.
package store_test

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"path/filepath"
	"strings"
	"testing"

	"credential_router/internal/platform"
	"credential_router/internal/credmgr/store"
)

// fuzzOpenStore opens a brand-new SQLite file in a per-iteration temp dir and
// schedules its closure. The file-backed DB is used (not ":memory:") to match
// production WAL behavior; t.TempDir() cleanup has been verified to run per
// fuzz iteration.
func fuzzOpenStore(t *testing.T) *store.Store {
	t.Helper()
	s, err := store.OpenForTesting(filepath.Join(t.TempDir(), "fuzz.db"))
	if err != nil {
		t.Fatalf("store.OpenForTesting: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

// fuzzCred bundles the typed fields parsed out of a []byte fuzz input.
type fuzzCred struct {
	userID, apiBase, keyTag, authType string
	cipher                            []byte
	rv, delRV                         int64 // optimistic-lock versions
}

// fuzzParseCred decodes a fuzz input into credential fields.
//
// Layout: the binary api_key_cipher is read from the head of the input via
// bytes.Reader (up to 16 bytes); the remaining bytes are string-sliced on '|'
// into the four text fields. The '|' separator lets embedded null bytes and
// arbitrary binary flow through into TEXT columns (SQLite stores them
// verbatim, verified). The two optimistic-lock int64s are derived from the
// first 16 bytes, little-endian.
func fuzzParseCred(data []byte) fuzzCred {
	r := bytes.NewReader(data)
	var buf [16]byte
	n, _ := r.Read(buf[:])
	cipher := buf[:n]

	rest := data[n:]
	parts := bytes.Split(rest, []byte{'|'})
	get := func(i int) string {
		if i < len(parts) {
			return string(parts[i])
		}
		return ""
	}
	return fuzzCred{
		userID:   get(0),
		apiBase:  get(1),
		keyTag:   get(2),
		authType: get(3),
		cipher:   cipher,
		rv:       fuzzInt64At(data, 0),
		delRV:    fuzzInt64At(data, 8),
	}
}

// fuzzInt64At reads up to 8 bytes starting at off as a little-endian int64.
// Shorter inputs zero-pad, so it is total for any []byte.
func fuzzInt64At(data []byte, off int) int64 {
	if off < 0 {
		off = 0
	}
	if off > len(data) {
		off = len(data)
	}
	end := off + 8
	if end > len(data) {
		end = len(data)
	}
	var buf [8]byte
	copy(buf[:], data[off:end])
	return int64(binary.LittleEndian.Uint64(buf[:]))
}

func fuzzInt64Bytes(v int64) []byte {
	var b [8]byte
	binary.LittleEndian.PutUint64(b[:], uint64(v))
	return b[:]
}

// fuzzCredSeed assembles a seed input in the fuzzParseCred layout: up to 16
// cipher bytes, then '|'-separated text fields.
func fuzzCredSeed(cipher, userID, apiBase, keyTag, authType string) []byte {
	var b []byte
	b = append(b, []byte(cipher)...)
	if len(b) > 16 {
		b = b[:16]
	}
	b = append(b, []byte("|"+userID+"|"+apiBase+"|"+keyTag+"|"+authType)...)
	return b
}

// fuzzCredSeeds returns the shared seed corpus for the credential targets:
// empty, single byte, known-valid, max length, unicode/emoji, null bytes,
// invalid UTF-8, and SQL-injection payloads.
func fuzzCredSeeds() [][]byte {
	return [][]byte{
		{},
		{0},
		fuzzCredSeed("sk-123", "u1", "https://api.example.com", "default", "openai"),
		fuzzCredSeed("", "", "", "", ""),
		fuzzCredSeed("sk-emoji", "用户", "https://例.com/🔑", "密钥tag", "openai"),
		fuzzCredSeed("sk\x00null", "u\x00id", "https://x\x00y", "t\x00g", "au\x00th"),
		fuzzCredSeed("sk-inj", "u'; DROP TABLE credentials;--", "https://x/?q=' OR '1'='1", "tag--", "openai"),
		fuzzCredSeed("sk-inj2", "; DROP TABLE IF EXISTS key_metadata; --", "https://x", "t", "a"),
		[]byte(strings.Repeat("a", 100000)), // max-length single field
		[]byte(strings.Repeat("🚀", 5000)),   // long multibyte
		{0xff, 0xfe, 0xfd},                  // invalid UTF-8 / binary
		fuzzCredSeed(strings.Repeat("B", 100), "user", "url", "tag", "auth"),
	}
}

// FuzzStoreInsertGet — round-trip: insert then get must return an equal
// payload for every field, including null bytes, emoji, invalid UTF-8 and
// SQL-injection strings (parameterization proof). Never panics, never nil-derefs.
func FuzzStoreInsertGet(f *testing.F) {
	for _, s := range fuzzCredSeeds() {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		c := fuzzParseCred(data)
		s := fuzzOpenStore(t)
		ctx := context.Background()

		cred := &store.Credential{
			UserID:       c.userID,
			APIBase:      c.apiBase,
			KeyTag:       c.keyTag,
			APIKeyCipher: c.cipher,
			AuthType:     c.authType,
		}
		if err := s.InsertCredential(ctx, cred); err != nil {
			t.Logf("insert rejected input (len=%d): %v", len(data), err)
			return // pathological input; nothing stored, nothing to round-trip
		}
		if cred.ID == 0 {
			t.Fatal("expected non-zero ID after insert")
		}

		got, err := s.GetCredentialByUserURLTag(ctx, c.userID, c.apiBase, c.keyTag)
		if err != nil {
			t.Fatalf("get(%q,%q,%q): %v", c.userID, c.apiBase, c.keyTag, err)
		}
		if got == nil {
			t.Fatal("GetCredentialByUserURLTag returned nil credential with nil error")
		}
		if got.UserID != c.userID {
			t.Fatalf("UserID roundtrip: %q != %q", got.UserID, c.userID)
		}
		if got.APIBase != c.apiBase {
			t.Fatalf("APIBase roundtrip: %q != %q", got.APIBase, c.apiBase)
		}
		if got.KeyTag != c.keyTag {
			t.Fatalf("KeyTag roundtrip: %q != %q", got.KeyTag, c.keyTag)
		}
		if string(got.APIKeyCipher) != string(c.cipher) {
			t.Fatalf("APIKeyCipher roundtrip: %q != %q", got.APIKeyCipher, c.cipher)
		}
		if got.AuthType != c.authType {
			t.Fatalf("AuthType roundtrip: %q != %q", got.AuthType, c.authType)
		}
	})
}

// FuzzStoreDuplicateInsert — inserting the same (user_id, api_base, key_tag)
// twice must return platform.ErrConflict, never panic and never clobber the first
// row.
func FuzzStoreDuplicateInsert(f *testing.F) {
	for _, s := range fuzzCredSeeds() {
		f.Add(s)
	}
	f.Add([]byte("dup|https://dup.example.com|default|sk|openai"))
	f.Fuzz(func(t *testing.T, data []byte) {
		c := fuzzParseCred(data)
		s := fuzzOpenStore(t)
		ctx := context.Background()

		c1 := &store.Credential{
			UserID: c.userID, APIBase: c.apiBase, KeyTag: c.keyTag,
			APIKeyCipher: c.cipher, AuthType: c.authType,
		}
		if err := s.InsertCredential(ctx, c1); err != nil {
			t.Logf("first insert rejected input (len=%d): %v", len(data), err)
			return
		}

		c2 := &store.Credential{
			UserID: c.userID, APIBase: c.apiBase, KeyTag: c.keyTag,
			APIKeyCipher: append([]byte("dup:"), c.cipher...),
			AuthType:     "different-auth",
		}
		err := s.InsertCredential(ctx, c2)
		if !errors.Is(err, platform.ErrConflict) {
			t.Fatalf("duplicate insert: expected platform.ErrConflict, got %v", err)
		}

		// First row must be intact.
		got, err := s.GetCredentialByUserURLTag(ctx, c.userID, c.apiBase, c.keyTag)
		if err != nil {
			t.Fatalf("get after failed duplicate: %v", err)
		}
		if got.ID != c1.ID {
			t.Fatalf("row id mutated by failed duplicate insert: got %d want %d", got.ID, c1.ID)
		}
		if string(got.APIKeyCipher) != string(c.cipher) {
			t.Fatalf("row cipher mutated: %q != %q", got.APIKeyCipher, c.cipher)
		}
	})
}

// FuzzStoreGetNotFound — GetCredentialByUserURLTag on a missing key returns
// platform.ErrNotFound (never a nil pointer with a nil error) for arbitrary
// strings, including injection payloads and huge inputs.
func FuzzStoreGetNotFound(f *testing.F) {
	for _, s := range fuzzCredSeeds() {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, data []byte) {
		c := fuzzParseCred(data)
		s := fuzzOpenStore(t)
		ctx := context.Background()

		got, err := s.GetCredentialByUserURLTag(ctx, c.userID, c.apiBase, c.keyTag)
		if !errors.Is(err, platform.ErrNotFound) {
			t.Fatalf("get(%q,%q,%q) on empty store: expected platform.ErrNotFound, got err=%v got=%v",
				c.userID, c.apiBase, c.keyTag, err, got)
		}
		if got != nil {
			t.Fatalf("get returned non-nil credential %+v alongside ErrNotFound", got)
		}
	})
}

// FuzzStoreUpdateDelete — optimistic row_version locking. Updates/deletes with
// the current version succeed (and bump / remove the row); stale versions must
// yield platform.ErrConflict. No panic on any combination of rv/delRV.
func FuzzStoreUpdateDelete(f *testing.F) {
	for _, s := range fuzzCredSeeds() {
		f.Add(s)
	}
	// rv=1 → update succeeds (version becomes 2); delRV=1 → stale → conflict.
	f.Add(fuzzUpdateDeleteSeed(1, 1, "u", "r", "t", "a"))
	// rv=1, delRV=2 → update succeeds, delete succeeds.
	f.Add(fuzzUpdateDeleteSeed(1, 2, "u", "r", "t", "a"))
	// rv=2 → stale update; delRV=2 → stale delete. Both conflict.
	f.Add(fuzzUpdateDeleteSeed(2, 2, "u", "r", "t", "a"))
	// rv=0 → conflict (inserted rows start at 1).
	f.Add(fuzzUpdateDeleteSeed(0, 0, "u", "r", "t", "a"))
	// extreme versions.
	f.Add(fuzzUpdateDeleteSeed(1, math.MaxInt64, "u", "r", "t", "a"))
	f.Add(fuzzUpdateDeleteSeed(math.MinInt64, math.MaxInt64, "u", "r", "t", "a"))
	f.Fuzz(func(t *testing.T, data []byte) {
		c := fuzzParseCred(data)
		s := fuzzOpenStore(t)
		ctx := context.Background()

		orig := &store.Credential{
			UserID: c.userID, APIBase: c.apiBase, KeyTag: c.keyTag,
			APIKeyCipher: c.cipher, AuthType: c.authType,
		}
		if err := s.InsertCredential(ctx, orig); err != nil {
			t.Logf("insert rejected input (len=%d): %v", len(data), err)
			return
		}
		id := orig.ID // fresh row: row_version == 1

		// Update with the fuzz-derived row_version.
		upd := &store.Credential{
			UserID:       c.userID,
			APIBase:      c.apiBase,
			KeyTag:       c.keyTag,
			APIKeyCipher: c.cipher,
			AuthType:     c.authType,
			RowVersion:   c.rv,
		}
		err := s.UpdateCredential(ctx, id, upd)
		curVersion := int64(1)
		if c.rv == 1 {
			if err != nil {
				t.Fatalf("update with matching row_version=1: %v", err)
			}
			if upd.RowVersion != 2 {
				t.Fatalf("after successful update, RowVersion=%d, want 2", upd.RowVersion)
			}
			curVersion = 2
		} else {
			if !errors.Is(err, platform.ErrConflict) {
				t.Fatalf("stale update row_version=%d: expected platform.ErrConflict, got %v", c.rv, err)
			}
		}

		// Delete with the fuzz-derived row_version.
		err = s.DeleteCredential(ctx, id, c.delRV)
		if c.delRV == curVersion {
			if err != nil {
				t.Fatalf("delete with matching row_version=%d: %v", curVersion, err)
			}
			if _, gerr := s.GetCredentialByID(ctx, id); !errors.Is(gerr, platform.ErrNotFound) {
				t.Fatalf("get by id after delete: expected platform.ErrNotFound, got %v", gerr)
			}
		} else {
			if !errors.Is(err, platform.ErrConflict) {
				t.Fatalf("delete with row_version=%d (want %d): expected platform.ErrConflict, got %v",
					c.delRV, curVersion, err)
			}
		}
	})
}

func fuzzUpdateDeleteSeed(rv, delRV int64, u, r, t, a string) []byte {
	var b []byte
	b = append(b, fuzzInt64Bytes(rv)...)
	b = append(b, fuzzInt64Bytes(delRV)...)
	b = append(b, []byte(u+"|"+r+"|"+t+"|"+a)...)
	return b
}

// FuzzStoreCountStragglers — counts rows with dek_version < target across a
// small table. Random (including negative and extreme) versions must never
// panic and must always agree with the locally computed count.
func FuzzStoreCountStragglers(f *testing.F) {
	f.Add(fuzzStragglerSeed(2, 1, 1, 2))
	f.Add(fuzzStragglerSeed(1, 1, 1, 1))
	f.Add(fuzzStragglerSeed(0, 0, 0, 0))
	f.Add(fuzzStragglerSeed(-5, -10, 3, 0))
	f.Add(fuzzStragglerSeed(math.MaxInt64, 1, 2, 3))
	f.Add(fuzzStragglerSeed(3, math.MinInt64, math.MaxInt64, 2))
	f.Add(make([]byte, 40)) // all zeros
	f.Add([]byte(strings.Repeat("a", 100)))
	f.Fuzz(func(t *testing.T, data []byte) {
		c := fuzzParseCred(data)
		s := fuzzOpenStore(t)
		ctx := context.Background()

		target := c.rv
		var dvs []int64
		for i := 0; i < 4; i++ {
			dv := fuzzInt64At(data, 8*(i+1))
			if dv == 0 {
				dv = 1 // InsertCredential defaults 0 → 1
			}
			cred := &store.Credential{
				UserID: c.userID, APIBase: c.apiBase, KeyTag: fmt.Sprintf("fz%d", i),
				APIKeyCipher: []byte("c"), AuthType: "openai", DekVersion: dv,
			}
			if err := s.InsertCredential(ctx, cred); err != nil {
				t.Fatalf("insert %d: %v", i, err)
			}
			dvs = append(dvs, dv)
		}

		var want int64
		for _, dv := range dvs {
			if dv < target {
				want++
			}
		}
		got, err := s.CountStragglersByDekVersion(ctx, target)
		if err != nil {
			t.Fatalf("CountStragglersByDekVersion(%d): %v", target, err)
		}
		if got != want {
			t.Fatalf("count(%d)=%d, want %d (dvs=%v)", target, got, want, dvs)
		}
	})
}

func fuzzStragglerSeed(target int64, dvs ...int64) []byte {
	var b []byte
	b = append(b, fuzzInt64Bytes(target)...)
	for _, dv := range dvs {
		b = append(b, fuzzInt64Bytes(dv)...)
	}
	return b
}
