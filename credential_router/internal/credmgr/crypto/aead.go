package crypto

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"errors"
	"fmt"
)

const (
	// AESKeySize is the required key size for AES-128: 16 bytes.
	AESKeySize = 16

	// NonceSize is the GCM nonce size in bytes: 12 bytes (96 bits).
	NonceSize = 12

	// TagSize is the GCM authentication tag size in bytes: 16 bytes (128 bits).
	TagSize = 16
)

// Sentinel errors for AES operations.
var (
	ErrInvalidKeySize    = errors.New("crypto: invalid key size")
	ErrInvalidNonceSize  = errors.New("crypto: invalid nonce size")
	ErrInvalidCiphertext = errors.New("crypto: invalid ciphertext")
)

// AESEncrypt encrypts plaintext with the given AES-128 key using GCM mode.
// It returns a randomly generated 12-byte nonce and the ciphertext
// (which includes the 16-byte authentication tag appended by Seal).
//
// The key MUST be exactly AESKeySize (16) bytes; otherwise ErrInvalidKeySize
// is returned. Plaintext may be empty.
func AESEncrypt(key, plaintext []byte) (nonce, ciphertext []byte, err error) {
	if len(key) != AESKeySize {
		return nil, nil, fmt.Errorf("%w: got %d, want %d", ErrInvalidKeySize, len(key), AESKeySize)
	}

	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, nil, fmt.Errorf("crypto: aes.NewCipher: %w", err)
	}

	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, nil, fmt.Errorf("crypto: cipher.NewGCM: %w", err)
	}

	nonce = make([]byte, NonceSize)
	if _, err := rand.Read(nonce); err != nil {
		return nil, nil, err
	}

	ciphertext = aead.Seal(nil, nonce, plaintext, nil)
	return nonce, ciphertext, nil
}

// AESDecrypt decrypts ciphertext using AES-128-GCM with the given key and nonce.
// It authenticates the ciphertext and returns an error if the authentication
// tag is invalid (indicating tampering or wrong key).
//
// The key MUST be exactly AESKeySize (16) bytes. The nonce MUST be exactly
// NonceSize (12) bytes. The ciphertext MUST be at least TagSize (16) bytes.
func AESDecrypt(key, nonce, ciphertext []byte) ([]byte, error) {
	if len(key) != AESKeySize {
		return nil, fmt.Errorf("%w: got %d, want %d", ErrInvalidKeySize, len(key), AESKeySize)
	}

	if len(nonce) != NonceSize {
		return nil, fmt.Errorf("%w: got %d, want %d", ErrInvalidNonceSize, len(nonce), NonceSize)
	}

	if len(ciphertext) < TagSize {
		return nil, fmt.Errorf("%w: ciphertext too short: got %d bytes, need at least %d", ErrInvalidCiphertext, len(ciphertext), TagSize)
	}

	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("crypto: aes.NewCipher: %w", err)
	}

	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("crypto: cipher.NewGCM: %w", err)
	}

	plaintext, err := aead.Open(nil, nonce, ciphertext, nil)
	if err != nil {
		return nil, err
	}

	return plaintext, nil
}
