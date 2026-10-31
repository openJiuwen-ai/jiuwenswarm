package crypto

import (
	"fmt"
	"hash"

	"golang.org/x/crypto/pbkdf2"
)

const (
	// PBKDF2Iterations is the iteration count for PBKDF2-HMAC key derivation.
	PBKDF2Iterations = 10000

	// SaltSize is the required salt length in bytes (fixed at compile time).
	SaltSize = 16

	// KEKSize is the key-encryption-key length in bytes (16 bytes, fixed at compile time).
	KEKSize = 16
)

// Sentinel errors for PBKDF2 operations.
var (
	ErrInvalidSaltLen = fmt.Errorf("crypto: invalid salt size: must be %d bytes", SaltSize)
	ErrInvalidKeyLen  = fmt.Errorf("crypto: invalid key length: must be %d bytes", KEKSize)
	ErrNilHashFunc    = fmt.Errorf("crypto: hash function must not be nil")
)

// PBKDF2HMAC derives a 16-byte KEK from password and salt using PBKDF2-HMAC.
//
// hashFunc must return a hash.Hash implementation such as crypto/sha256.New or
// sm3.New. The salt must be exactly SaltSize (16) bytes. The iteration count
// is fixed at PBKDF2Iterations (10000).
//
// Returns the derived key as a KeyBytes defensive wrapper.
func PBKDF2HMAC(password, salt []byte, hashFunc func() hash.Hash) (*KeyBytes, error) {
	if hashFunc == nil {
		return nil, ErrNilHashFunc
	}
	if len(salt) != SaltSize {
		return nil, fmt.Errorf("%w: got %d bytes", ErrInvalidSaltLen, len(salt))
	}

	derived := pbkdf2.Key(password, salt, PBKDF2Iterations, KEKSize, hashFunc)
	return NewKeyBytes(derived), nil
}
