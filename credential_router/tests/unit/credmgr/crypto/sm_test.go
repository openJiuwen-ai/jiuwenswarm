package crypto_test

import (
	"bytes"
	"errors"
	"testing"

	"credential_router/internal/credmgr/crypto"
)

func TestSM4RoundTrip(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	for i := range key {
		key[i] = byte(i)
	}
	plaintext := []byte("hello, sm4-gcm")

	nonce, ct, err := crypto.SM4Encrypt(key, plaintext)
	if err != nil {
		t.Fatalf("SM4Encrypt failed: %v", err)
	}

	got, err := crypto.SM4Decrypt(key, nonce, ct)
	if err != nil {
		t.Fatalf("SM4Decrypt failed: %v", err)
	}

	if !bytes.Equal(got, plaintext) {
		t.Errorf("Decrypted text = %x, want %x", got, plaintext)
	}
}

func TestSM4RoundTripEmptyPlaintext(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	nonce, ct, err := crypto.SM4Encrypt(key, []byte{})
	if err != nil {
		t.Fatalf("SM4Encrypt(empty) failed: %v", err)
	}
	got, err := crypto.SM4Decrypt(key, nonce, ct)
	if err != nil {
		t.Fatalf("SM4Decrypt(empty) failed: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("Decrypted len = %d, want 0", len(got))
	}
}

func TestSM4EncryptWrongKeySize(t *testing.T) {
	_, _, err := crypto.SM4Encrypt(make([]byte, 8), []byte("test"))
	if err == nil {
		t.Fatal("SM4Encrypt with 8-byte key: expected error")
	}
}

func TestSM4DecryptWrongKeySize(t *testing.T) {
	_, err := crypto.SM4Decrypt(make([]byte, 24), make([]byte, crypto.NonceSize), make([]byte, 32))
	if err == nil {
		t.Fatal("SM4Decrypt with 24-byte key: expected error")
	}
}

func TestSM4DecryptWrongNonceSize(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	_, err := crypto.SM4Decrypt(key, make([]byte, 4), make([]byte, 32))
	if err == nil {
		t.Fatal("SM4Decrypt with 4-byte nonce: expected error")
	}
}

func TestSM4DecryptTruncatedCiphertext(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	// Ciphertext shorter than tag size (16 bytes)
	_, err := crypto.SM4Decrypt(key, make([]byte, crypto.NonceSize), make([]byte, 8))
	if err == nil {
		t.Fatal("SM4Decrypt with 8-byte ciphertext: expected error")
	}
}

func TestSM4DecryptTamperedTag(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	plaintext := []byte("sensitive data")
	nonce, ct, err := crypto.SM4Encrypt(key, plaintext)
	if err != nil {
		t.Fatalf("SM4Encrypt failed: %v", err)
	}

	// Flip the last byte of ciphertext (the tag is at the end)
	ct[len(ct)-1] ^= 0x01

	_, err = crypto.SM4Decrypt(key, nonce, ct)
	if err == nil {
		t.Fatal("SM4Decrypt with tampered tag: expected authentication error")
	}
}

func TestSM4DecryptTamperedCiphertext(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	plaintext := []byte("sensitive data")
	nonce, ct, err := crypto.SM4Encrypt(key, plaintext)
	if err != nil {
		t.Fatalf("SM4Encrypt failed: %v", err)
	}

	// Flip a byte in the ciphertext body (not the tag)
	ct[4] ^= 0x01

	_, err = crypto.SM4Decrypt(key, nonce, ct)
	if err == nil {
		t.Fatal("SM4Decrypt with tampered ciphertext: expected authentication error")
	}
}

func TestSM4EncryptUniqueNonces(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	pt := []byte("same plaintext")

	nonce1, ct1, err := crypto.SM4Encrypt(key, pt)
	if err != nil {
		t.Fatalf("first SM4Encrypt failed: %v", err)
	}
	nonce2, ct2, err := crypto.SM4Encrypt(key, pt)
	if err != nil {
		t.Fatalf("second SM4Encrypt failed: %v", err)
	}

	if bytes.Equal(nonce1, nonce2) {
		t.Errorf("Nonces should be unique across calls, got same: %x", nonce1)
	}
	if bytes.Equal(ct1, ct2) {
		t.Errorf("Ciphertexts should differ when nonces differ, got same: %x", ct1)
	}
}

func TestSM4DecryptWrongKey(t *testing.T) {
	key1 := make([]byte, crypto.SM4KeySize)
	key1[0] = 0x01
	key2 := make([]byte, crypto.SM4KeySize)
	key2[0] = 0x02

	nonce, ct, err := crypto.SM4Encrypt(key1, []byte("secret"))
	if err != nil {
		t.Fatalf("SM4Encrypt failed: %v", err)
	}

	_, err = crypto.SM4Decrypt(key2, nonce, ct)
	if err == nil {
		t.Fatal("SM4Decrypt with wrong key: expected authentication error")
	}
}

func TestErrInvalidNonceSizeSM4(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	_, err := crypto.SM4Decrypt(key, []byte{1, 2, 3}, make([]byte, crypto.TagSize))
	if !errors.Is(err, crypto.ErrInvalidNonceSize) {
		t.Errorf("expected errors.Is(err, ErrInvalidNonceSize), got %v", err)
	}
}
