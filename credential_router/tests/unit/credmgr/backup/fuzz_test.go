//go:build cgo

// Fuzz tests for internal/backup.
//
// Quick:  go test -run=^$ -fuzz=^Fuzz -fuzztime=10s ./tests/unit/backup/...
// Full:   go test -run=^$ -fuzz=^Fuzz -fuzztime=60s ./tests/unit/backup/...

package backup_test

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"

	"credential_router/internal/credmgr/backup"
)

// FuzzDeleteOldest — retention file-selection logic. Builds a random set of
// backup files with random-but-distinct mtimes, runs deleteOldest with a
// random keep policy, and asserts the kept/deleted classification matches the
// policy exactly (never panics, never returns a spurious error, and never
// keeps more files than the newest `keep`).
func FuzzDeleteOldest(f *testing.F) {
	f.Add([]byte{3, 2})
	f.Add([]byte{})
	f.Add([]byte{1, 0})
	f.Add([]byte{0, 20})
	f.Add([]byte{8, 8})
	f.Add([]byte{4, 4, 1, 2, 3, 4})
	f.Add([]byte{2, 1, 0x00, 0xff})
	f.Add([]byte{12, 23})
	f.Fuzz(func(t *testing.T, data []byte) {
		n := 3
		keep := 2
		if len(data) > 0 {
			n = 1 + int(data[0])%12 // 1..12 files
		}
		if len(data) > 1 {
			keep = int(data[1]) % 24 // 0..23, covers keep==0 and keep>n
		}

		// Deterministic permutation of file indices derived from data, so the
		// relative mtime ordering is corpus-driven but always distinct.
		idx := make([]int, n)
		for i := range idx {
			idx[i] = i
		}
		rank := func(i int) int {
			if len(data) == 0 {
				return i
			}
			return int(data[i%len(data)])
		}
		sort.Slice(idx, func(a, b int) bool {
			ra, rb := rank(idx[a]), rank(idx[b])
			if ra != rb {
				return ra < rb
			}
			return idx[a] < idx[b]
		})

		dir := t.TempDir()
		for k, fi := range idx {
			path := filepath.Join(dir, fmt.Sprintf("backup-kek-%d.db", fi))
			if err := os.WriteFile(path, []byte("data"), 0o600); err != nil {
				t.Fatalf("write %s: %v", path, err)
			}
			// rank 0 = oldest (n hours ago), rank n-1 = newest (1 hour ago).
			mtime := time.Now().Add(-time.Duration(n-k) * time.Hour)
			if err := os.Chtimes(path, mtime, mtime); err != nil {
				t.Fatalf("chtimes %s: %v", path, err)
			}
		}

		if err := backup.DeleteOldestForTesting(filepath.Join(dir, "backup-kek-*.db"), keep); err != nil {
			t.Fatalf("deleteOldest(keep=%d, n=%d): %v", keep, n, err)
		}

		// The newest `keep` ranks survive; rem = min(n, keep).
		rem := min(n, keep)
		kept := make([]string, 0, rem)
		for k := n - rem; k < n; k++ {
			kept = append(kept, fmt.Sprintf("backup-kek-%d.db", idx[k]))
		}
		got, err := filepath.Glob(filepath.Join(dir, "backup-kek-*.db"))
		if err != nil {
			t.Fatalf("glob: %v", err)
		}
		for i := range got {
			got[i] = filepath.Base(got[i])
		}
		sort.Strings(got)
		sort.Strings(kept)
		if len(got) != len(kept) {
			t.Fatalf("keep=%d n=%d: survivors=%v, want %d files %v",
				keep, n, got, len(kept), kept)
		}
		for i := range got {
			if got[i] != kept[i] {
				t.Fatalf("keep=%d n=%d: survivor %q not in expected newest set %v",
					keep, n, got[i], kept)
			}
		}
	})
}

// FuzzTplFilename — snapshot naming. Template expansion must be deterministic,
// substitute both {type} and {ts} placeholders, and produce names that the
// derived retention glob matches (so retention can find what naming wrote).
func FuzzTplFilename(f *testing.F) {
	f.Add([]byte("backup-{type}-{ts}.db"))
	f.Add([]byte("key-snapshot-{ts}.bin"))
	f.Add([]byte("custom-{type}-{ts}.backup"))
	f.Add([]byte("{ts}"))
	f.Add([]byte("static.db"))
	f.Add([]byte(""))
	f.Add([]byte("中文-{ts}-备份"))
	f.Add([]byte("a{ts}{ts}b"))
	f.Add(bytes.Repeat([]byte("x"), 300))
	f.Fuzz(func(t *testing.T, data []byte) {
		tpl := string(data)
		btype := "kek"
		switch len(data) % 3 {
		case 1:
			btype = "dek"
		case 2:
			btype = "junk-type"
		}
		var ts int64
		if len(data) >= 8 {
			ts = int64(binary.LittleEndian.Uint64(data[:8]))
		} else {
			ts = int64(len(data))
		}

		out := backup.TplFilenameForTesting(tpl, btype, ts)
		if out == "" && tpl != "" {
			t.Fatalf("tplFilename(%q, %q, %d) = empty", tpl, btype, ts)
		}
		if out != backup.TplFilenameForTesting(tpl, btype, ts) {
			t.Fatalf("tplFilename(%q, %q, %d) not deterministic", tpl, btype, ts)
		}
		tsStr := strconv.FormatInt(ts, 10)
		if strings.Contains(tpl, "{ts}") && !strings.Contains(out, tsStr) {
			t.Fatalf("tpl=%q btype=%q ts=%d: %q missing {ts} replacement",
				tpl, btype, ts, out)
		}
		if strings.Contains(tpl, "{type}") && !strings.Contains(out, btype) {
			t.Fatalf("tpl=%q btype=%q: %q missing {type} replacement", tpl, btype, out)
		}
		// The retention glob derived from the same template must match the name.
		// Skip templates carrying their own glob metachars, which change
		// filepath.Match semantics in ways unrelated to {ts}.
		if !strings.ContainsAny(tpl, "*?[\\") {
			glob := backup.TplGlobForTesting(tpl, btype)
			ok, err := filepath.Match(glob, out)
			if err != nil {
				t.Fatalf("filepath.Match(%q, %q): %v", glob, out, err)
			}
			if !ok {
				t.Fatalf("tpl=%q: retention glob %q does not match produced name %q",
					tpl, glob, out)
			}
		}
	})
}

// validKeySnapshotBytes builds a syntactically valid 122-byte key snapshot for seeds.
func validKeySnapshotBytes() []byte {
	var buf [backup.KeySnapshotSize]byte
	copy(buf[0:4], backup.KeySnapshotMagic)
	buf[4] = backup.KeySnapshotFormatVer
	buf[5] = backup.KeySnapshotModeAES
	binary.LittleEndian.PutUint64(buf[6:14], 1700000000)
	return buf[:]
}

// FuzzReadKeySnapshot — parsing of on-disk key snapshot blobs. Fixed-size layout with
// magic/version/mode gates; random bytes of any length must never panic, and
// any accepted 122-byte blob must be fully consistent (magic, version, mode,
// and a 44-byte wrapped DEK).
func FuzzReadKeySnapshot(f *testing.F) {
	f.Add(validKeySnapshotBytes())
	f.Add([]byte{})
	f.Add(make([]byte, backup.KeySnapshotSize))
	f.Add(make([]byte, backup.KeySnapshotSize-1))
	f.Add(make([]byte, backup.KeySnapshotSize+1))
	f.Add([]byte("KSNP\x01\x01"))
	f.Add([]byte("\xff\xfe\xfd\xfc\xfb\xfa\xf9\xf8\xf7\xf6\xf5"))
	f.Fuzz(func(t *testing.T, data []byte) {
		path := filepath.Join(t.TempDir(), "key_snapshot.bin")
		if err := os.WriteFile(path, data, 0o600); err != nil {
			t.Fatalf("write: %v", err)
		}
		snap, err := backup.ReadKeySnapshot(path)
		if len(data) != backup.KeySnapshotSize {
			if err == nil {
				t.Fatalf("backup.ReadKeySnapshot accepted %d-byte file, want error", len(data))
			}
			return
		}
		if err == nil {
			if snap.FormatVersion != backup.KeySnapshotFormatVer {
				t.Fatalf("FormatVersion=0x%02x, want 0x%02x", snap.FormatVersion, backup.KeySnapshotFormatVer)
			}
			if snap.CryptoMode != backup.KeySnapshotModeAES && snap.CryptoMode != backup.KeySnapshotModeSM {
				t.Fatalf("CryptoMode=0x%02x, want AES(0x01) or SM(0x02)", snap.CryptoMode)
			}
			if len(snap.WrappedDEK) != 44 {
				t.Fatalf("WrappedDEK len=%d, want 44", len(snap.WrappedDEK))
			}
		}
	})
}

// FuzzWriteKeySnapshot — serialization boundary. A non-44-byte wrapped DEK must
// fail cleanly; a 44-byte DEK must produce an exact-size file.
func FuzzWriteKeySnapshot(f *testing.F) {
	f.Add(bytes.Repeat([]byte{0xAB}, 44))
	f.Add([]byte{})
	f.Add([]byte{0xAB})
	f.Add(bytes.Repeat([]byte{0xFF}, 300))
	f.Fuzz(func(t *testing.T, data []byte) {
		var mode byte
		if len(data) > 0 {
			mode = data[0]
		}
		path := filepath.Join(t.TempDir(), "key_snapshot.bin")
		var s1, s2 [32]byte
		snap := backup.NewKeySnapshot(mode, s1, s2, data)
		err := backup.WriteKeySnapshot(path, snap)
		if len(data) != 44 {
			if err == nil {
				t.Fatalf("backup.WriteKeySnapshot accepted wrapped_dek len=%d, want error", len(data))
			}
			return
		}
		if err != nil {
			t.Fatalf("backup.WriteKeySnapshot failed for 44-byte wrapped_dek: %v", err)
		}
		info, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat: %v", err)
		}
		if info.Size() != backup.KeySnapshotSize {
			t.Fatalf("size=%d, want %d", info.Size(), backup.KeySnapshotSize)
		}
	})
}
