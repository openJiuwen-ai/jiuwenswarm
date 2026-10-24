package crypto

import (
	"crypto/cipher"
	"crypto/rand"
	"fmt"

	"github.com/tjfoc/gmsm/sm4"
)

const (
	// SM4KeySize is the required key size for SM4: 16 bytes (128 bits).
	SM4KeySize = 16
)

// SM4Encrypt encrypts plaintext with the given SM4 key using GCM mode.
// It returns a randomly generated 12-byte nonce and the ciphertext
// (which includes the 16-byte authentication tag appended by Seal).
//
// The key MUST be exactly SM4KeySize (16) bytes; otherwise ErrInvalidKeySize
// is returned. Plaintext may be empty.
func SM4Encrypt(key, plaintext []byte) (nonce, ciphertext []byte, err error) {
	if len(key) != SM4KeySize {
		return nil, nil, fmt.Errorf("%w: got %d, want %d", ErrInvalidKeySize, len(key), SM4KeySize)
	}

	block, err := sm4.NewCipher(key)
	if err != nil {
		return nil, nil, fmt.Errorf("crypto: sm4.NewCipher: %w", err)
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

// SM4Decrypt decrypts ciphertext using SM4-GCM with the given key and nonce.
// It authenticates the ciphertext and returns an error if the authentication
// tag is invalid (indicating tampering or wrong key).
//
// The key MUST be exactly SM4KeySize (16) bytes. The nonce MUST be exactly
// NonceSize (12) bytes. The ciphertext MUST be at least TagSize (16) bytes.
func SM4Decrypt(key, nonce, ciphertext []byte) ([]byte, error) {
	if len(key) != SM4KeySize {
		return nil, fmt.Errorf("%w: got %d, want %d", ErrInvalidKeySize, len(key), SM4KeySize)
	}

	if len(nonce) != NonceSize {
		return nil, fmt.Errorf("%w: got %d, want %d", ErrInvalidNonceSize, len(nonce), NonceSize)
	}

	if len(ciphertext) < TagSize {
		return nil, fmt.Errorf("%w: ciphertext too short: got %d bytes, need at least %d", ErrInvalidCiphertext, len(ciphertext), TagSize)
	}

	block, err := sm4.NewCipher(key)
	if err != nil {
		return nil, fmt.Errorf("crypto: sm4.NewCipher: %w", err)
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
