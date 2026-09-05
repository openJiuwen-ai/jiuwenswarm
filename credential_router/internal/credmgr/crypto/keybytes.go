// Package crypto provides cryptographic primitives for credential_router.
//
// KeyBytes is a defensive byte wrapper that prevents accidental leakage of
// sensitive key material through its deep-copy-on-construct, copy-on-read,
// and explicit zeroisation semantics.
//
// AES-128-GCM encryption uses the on-disk AEAD format:
//
//	1B format_id + 12B nonce + N ciphertext + 16B tag
package crypto

import (
	"crypto/rand"
	"encoding/hex"
)

// KeyBytes is a defensive wrapper around a byte slice containing sensitive
// key material. It deep-copies on construction and returns copies on reads
// to prevent accidental or malicious mutation of the internal buffer.
type KeyBytes struct {
	b []byte
}

// NewKeyBytes creates a KeyBytes from src by deep-copying the bytes.
// The caller may safely clear or reuse src after this call.
//
// Returns a pointer rather than a value type so callers must explicitly
// call Zero() to invalidate the underlying buffer. Value semantics would
// let key material be silently copied into a fresh struct on assignment,
// which is exactly what KeyBytes exists to prevent.
func NewKeyBytes(src []byte) *KeyBytes {
	cp := make([]byte, len(src))
	copy(cp, src)
	return &KeyBytes{b: cp}
}

// NewRandomKey creates a KeyBytes of length n filled with cryptographically
// random bytes. Returns an error if the OS entropy source fails.
func NewRandomKey(n int) (*KeyBytes, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return nil, err
	}
	return &KeyBytes{b: b}, nil
}

// Bytes returns a deep copy of the internal key material. The caller may
// safely read or discard the returned slice without affecting the KeyBytes.
func (k KeyBytes) Bytes() []byte {
	cp := make([]byte, len(k.b))
	copy(cp, k.b)
	return cp
}

// Len returns the number of bytes in the key.
func (k KeyBytes) Len() int {
	return len(k.b)
}

// IsZero reports whether every byte in the key is zero.
func (k KeyBytes) IsZero() bool {
	for _, v := range k.b {
		if v != 0 {
			return false
		}
	}
	return true
}

// Zero clears all bytes in the internal buffer, marking the key material as
// invalidated. After calling Zero the KeyBytes should not be used for
// cryptographic operations.
func (k *KeyBytes) Zero() {
	clear(k.b)
}

// Hex returns the hex encoding of the key bytes.
func (k KeyBytes) Hex() string {
	return hex.EncodeToString(k.b)
}
