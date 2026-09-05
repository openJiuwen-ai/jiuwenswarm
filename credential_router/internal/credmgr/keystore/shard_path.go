package keystore

import (
	"fmt"
	"path/filepath"
)

// S1ShardPath returns the canonical on-disk path for shard 1 at the given
// version. Versions are monotonically increasing across KEK S1 rotations.
//
// Layout: <secretsDir>/s1.bin.<N>, with N >= 1. The DB stores the active
// version in key_metadata.file_shard_version; this helper is the single
// source of truth for deriving the path. Write directly to the versioned
// path; the legacy write-then-rename pattern left a crash window where
// the DB could claim a new version before the file existed on disk.
func S1ShardPath(secretsDir string, version int64) string {
	return filepath.Join(secretsDir, fmt.Sprintf("s1.bin.%d", version))
}
