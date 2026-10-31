package crypto_test

import (
	"bytes"
	"errors"
	"testing"

	"credential_router/internal/credmgr/crypto"
)

func TestAESRoundTrip(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	for i := range key {
		key[i] = byte(i)
	}
	plaintext := []byte("hello, aes-128-gcm")

	nonce, ct, err := crypto.AESEncrypt(key, plaintext)
	if err != nil {
		t.Fatalf("AESEncrypt failed: %v", err)
	}

	got, err := crypto.AESDecrypt(key, nonce, ct)
	if err != nil {
		t.Fatalf("AESDecrypt failed: %v", err)
	}

	if !bytes.Equal(got, plaintext) {
		t.Errorf("Decrypted text = %x, want %x", got, plaintext)
	}
}

func TestAESRoundTripEmptyPlaintext(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	nonce, ct, err := crypto.AESEncrypt(key, []byte{})
	if err != nil {
		t.Fatalf("AESEncrypt(empty) failed: %v", err)
	}
	got, err := crypto.AESDecrypt(key, nonce, ct)
	if err != nil {
		t.Fatalf("AESDecrypt(empty) failed: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("Decrypted len = %d, want 0", len(got))
	}
}

func TestAESEncryptWrongKeySize(t *testing.T) {
	_, _, err := crypto.AESEncrypt(make([]byte, 8), []byte("test"))
	if err == nil {
		t.Fatal("AESEncrypt with 8-byte key: expected error")
	}
}

func TestAESDecryptWrongKeySize(t *testing.T) {
	_, err := crypto.AESDecrypt(make([]byte, 24), make([]byte, crypto.NonceSize), make([]byte, 32))
	if err == nil {
		t.Fatal("AESDecrypt with 24-byte key: expected error")
	}
}

func TestAESDecryptWrongNonceSize(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	_, err := crypto.AESDecrypt(key, make([]byte, 4), make([]byte, 32))
	if err == nil {
		t.Fatal("AESDecrypt with 4-byte nonce: expected error")
	}
}

func TestAESDecryptTruncatedCiphertext(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	// Ciphertext shorter than tag size (16 bytes)
	_, err := crypto.AESDecrypt(key, make([]byte, crypto.NonceSize), make([]byte, 8))
	if err == nil {
		t.Fatal("AESDecrypt with 8-byte ciphertext: expected error")
	}
}

func TestAESDecryptTamperedTag(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	plaintext := []byte("sensitive data")
	nonce, ct, err := crypto.AESEncrypt(key, plaintext)
	if err != nil {
		t.Fatalf("AESEncrypt failed: %v", err)
	}

	// Flip the last byte of ciphertext (the tag is at the end)
	ct[len(ct)-1] ^= 0x01

	_, err = crypto.AESDecrypt(key, nonce, ct)
	if err == nil {
		t.Fatal("AESDecrypt with tampered tag: expected authentication error")
	}
}

func TestAESDecryptTamperedCiphertext(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	plaintext := []byte("sensitive data")
	nonce, ct, err := crypto.AESEncrypt(key, plaintext)
	if err != nil {
		t.Fatalf("AESEncrypt failed: %v", err)
	}

	// Flip a byte in the ciphertext body (not the tag)
	ct[4] ^= 0x01

	_, err = crypto.AESDecrypt(key, nonce, ct)
	if err == nil {
		t.Fatal("AESDecrypt with tampered ciphertext: expected authentication error")
	}
}

func TestAESEncryptUniqueNonces(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	pt := []byte("same plaintext")

	nonce1, ct1, err := crypto.AESEncrypt(key, pt)
	if err != nil {
		t.Fatalf("first AESEncrypt failed: %v", err)
	}
	nonce2, ct2, err := crypto.AESEncrypt(key, pt)
	if err != nil {
		t.Fatalf("second AESEncrypt failed: %v", err)
	}

	if bytes.Equal(nonce1, nonce2) {
		t.Errorf("Nonces should be unique across calls, got same: %x", nonce1)
	}
	if bytes.Equal(ct1, ct2) {
		t.Errorf("Ciphertexts should differ when nonces differ, got same: %x", ct1)
	}
}

func TestAESDecryptWrongKey(t *testing.T) {
	key1 := make([]byte, crypto.AESKeySize)
	key1[0] = 0x01
	key2 := make([]byte, crypto.AESKeySize)
	key2[0] = 0x02

	nonce, ct, err := crypto.AESEncrypt(key1, []byte("secret"))
	if err != nil {
		t.Fatalf("AESEncrypt failed: %v", err)
	}

	_, err = crypto.AESDecrypt(key2, nonce, ct)
	if err == nil {
		t.Fatal("AESDecrypt with wrong key: expected authentication error")
	}
}

func TestErrInvalidNonceSize(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	_, err := crypto.AESDecrypt(key, []byte{1, 2, 3}, make([]byte, crypto.TagSize))
	if !errors.Is(err, crypto.ErrInvalidNonceSize) {
		t.Errorf("expected errors.Is(err, ErrInvalidNonceSize), got %v", err)
	}
}
