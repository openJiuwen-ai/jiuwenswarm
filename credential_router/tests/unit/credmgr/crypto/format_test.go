package crypto_test

import (
	"bytes"
	"errors"
	"testing"

	"credential_router/internal/credmgr/crypto"
)

// ---------------------------------------------------------------------------
// EncryptCredential / DecryptCredential
// ---------------------------------------------------------------------------

func TestEncryptDecryptRoundTripAES(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	for i := range key {
		key[i] = byte(i)
	}
	plaintext := []byte("api-key-12345")

	blob, err := crypto.EncryptCredential(crypto.ModeAES, key, plaintext)
	if err != nil {
		t.Fatalf("EncryptCredential(ModeAES) failed: %v", err)
	}

	got, err := crypto.DecryptCredential(crypto.ModeAES, key, blob)
	if err != nil {
		t.Fatalf("DecryptCredential(ModeAES) failed: %v", err)
	}

	if !bytes.Equal(got, plaintext) {
		t.Errorf("Decrypted text = %x, want %x", got, plaintext)
	}
}

func TestEncryptDecryptRoundTripSM4(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	for i := range key {
		key[i] = byte(i)
	}
	plaintext := []byte("api-key-sm4-67890")

	blob, err := crypto.EncryptCredential(crypto.ModeSM4, key, plaintext)
	if err != nil {
		t.Fatalf("EncryptCredential(ModeSM4) failed: %v", err)
	}

	got, err := crypto.DecryptCredential(crypto.ModeSM4, key, blob)
	if err != nil {
		t.Fatalf("DecryptCredential(ModeSM4) failed: %v", err)
	}

	if !bytes.Equal(got, plaintext) {
		t.Errorf("Decrypted text = %x, want %x", got, plaintext)
	}
}

func TestEncryptCredentialFormatByteAES(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	blob, err := crypto.EncryptCredential(crypto.ModeAES, key, []byte("test"))
	if err != nil {
		t.Fatalf("EncryptCredential(ModeAES) failed: %v", err)
	}
	if len(blob) == 0 {
		t.Fatal("EncryptCredential returned empty blob")
	}
	if blob[0] != 0x01 {
		t.Errorf("AES format byte = 0x%02x, want 0x01", blob[0])
	}
}

func TestEncryptCredentialFormatByteSM4(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	blob, err := crypto.EncryptCredential(crypto.ModeSM4, key, []byte("test"))
	if err != nil {
		t.Fatalf("EncryptCredential(ModeSM4) failed: %v", err)
	}
	if len(blob) == 0 {
		t.Fatal("EncryptCredential returned empty blob")
	}
	if blob[0] != 0x02 {
		t.Errorf("SM4 format byte = 0x%02x, want 0x02", blob[0])
	}
}

func TestEncryptDecryptModeMismatchAES(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	blob, err := crypto.EncryptCredential(crypto.ModeAES, key, []byte("secret"))
	if err != nil {
		t.Fatalf("EncryptCredential(ModeAES) failed: %v", err)
	}

	// Decrypt with SM4 mode — format byte mismatch
	_, err = crypto.DecryptCredential(crypto.ModeSM4, key, blob)
	if err == nil {
		t.Fatal("DecryptCredential with ModeSM4 on AES blob: expected error")
	}
	if !errors.Is(err, crypto.ErrUnsupportedCipherFormat) {
		t.Errorf("expected errors.Is(err, ErrUnsupportedCipherFormat), got %v", err)
	}
}

func TestEncryptDecryptModeMismatchSM4(t *testing.T) {
	key := make([]byte, crypto.SM4KeySize)
	blob, err := crypto.EncryptCredential(crypto.ModeSM4, key, []byte("secret"))
	if err != nil {
		t.Fatalf("EncryptCredential(ModeSM4) failed: %v", err)
	}

	// Decrypt with AES mode — format byte mismatch
	_, err = crypto.DecryptCredential(crypto.ModeAES, key, blob)
	if err == nil {
		t.Fatal("DecryptCredential with ModeAES on SM4 blob: expected error")
	}
	if !errors.Is(err, crypto.ErrUnsupportedCipherFormat) {
		t.Errorf("expected errors.Is(err, ErrUnsupportedCipherFormat), got %v", err)
	}
}

func TestDecryptCredentialFormatMismatch(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	blob, err := crypto.EncryptCredential(crypto.ModeAES, key, []byte("secret"))
	if err != nil {
		t.Fatalf("EncryptCredential(ModeAES) failed: %v", err)
	}

	// Pass ModeSM4 but blob starts with 0x01 (AES)
	_, err = crypto.DecryptCredential(crypto.ModeSM4, key, blob)
	if !errors.Is(err, crypto.ErrUnsupportedCipherFormat) {
		t.Errorf("expected ErrUnsupportedCipherFormat, got %v", err)
	}
}

func TestDecryptCredentialTruncatedBlob(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	// 5 bytes is shorter than FormatByteSize+NonceSize+TagSize = 1+12+16 = 29
	_, err := crypto.DecryptCredential(crypto.ModeAES, key, []byte{0x01, 0x02, 0x03, 0x04, 0x05})
	if err == nil {
		t.Fatal("DecryptCredential with 5-byte blob: expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidCiphertext) {
		t.Errorf("expected errors.Is(err, ErrInvalidCiphertext), got %v", err)
	}
}

func TestEncryptCredentialInvalidMode(t *testing.T) {
	_, err := crypto.EncryptCredential(crypto.Mode(0x00), make([]byte, 16), []byte("test"))
	if err == nil {
		t.Fatal("EncryptCredential with invalid mode: expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidMode) {
		t.Errorf("expected errors.Is(err, ErrInvalidMode), got %v", err)
	}
}

func TestDecryptCredentialInvalidMode(t *testing.T) {
	key := make([]byte, crypto.AESKeySize)
	blob, err := crypto.EncryptCredential(crypto.ModeAES, key, []byte("test"))
	if err != nil {
		t.Fatalf("EncryptCredential(ModeAES) failed: %v", err)
	}

	// Pass a mode that doesn't match AES or SM4
	_, err = crypto.DecryptCredential(crypto.Mode(0x00), key, blob)
	if err == nil {
		t.Fatal("DecryptCredential with Mode(0x00): expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidMode) {
		t.Errorf("expected errors.Is(err, ErrInvalidMode), got %v", err)
	}
}

func TestEncryptCredentialWrongKeySizeAES(t *testing.T) {
	_, err := crypto.EncryptCredential(crypto.ModeAES, make([]byte, 8), []byte("test"))
	if err == nil {
		t.Fatal("EncryptCredential(ModeAES) with 8-byte key: expected error")
	}
}

func TestEncryptCredentialWrongKeySizeSM4(t *testing.T) {
	_, err := crypto.EncryptCredential(crypto.ModeSM4, make([]byte, 8), []byte("test"))
	if err == nil {
		t.Fatal("EncryptCredential(ModeSM4) with 8-byte key: expected error")
	}
}

// ---------------------------------------------------------------------------
// WrapDEK / UnwrapDEK
// ---------------------------------------------------------------------------

func TestWrapDEKRoundTripAES(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	for i := range kek {
		kek[i] = byte(i)
	}
	dek := make([]byte, crypto.DEKSize)
	for i := range dek {
		dek[i] = byte(0x10 + i)
	}

	wrapped, err := crypto.WrapDEK(crypto.ModeAES, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK(ModeAES) failed: %v", err)
	}

	got, err := crypto.UnwrapDEK(crypto.ModeAES, kek, wrapped)
	if err != nil {
		t.Fatalf("UnwrapDEK(ModeAES) failed: %v", err)
	}

	if !bytes.Equal(got, dek) {
		t.Errorf("Unwrapped DEK = %x, want %x", got, dek)
	}
}

func TestWrapDEKRoundTripSM4(t *testing.T) {
	kek := make([]byte, crypto.SM4KeySize)
	for i := range kek {
		kek[i] = byte(i)
	}
	dek := make([]byte, crypto.DEKSize)
	for i := range dek {
		dek[i] = byte(0x20 + i)
	}

	wrapped, err := crypto.WrapDEK(crypto.ModeSM4, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK(ModeSM4) failed: %v", err)
	}

	got, err := crypto.UnwrapDEK(crypto.ModeSM4, kek, wrapped)
	if err != nil {
		t.Fatalf("UnwrapDEK(ModeSM4) failed: %v", err)
	}

	if !bytes.Equal(got, dek) {
		t.Errorf("Unwrapped DEK = %x, want %x", got, dek)
	}
}

func TestWrapDEKSize(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	dek := make([]byte, crypto.DEKSize)
	wrapped, err := crypto.WrapDEK(crypto.ModeAES, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK failed: %v", err)
	}
	if len(wrapped) != crypto.WrappedDEKSize {
		t.Errorf("Wrapped DEK length = %d, want %d", len(wrapped), crypto.WrappedDEKSize)
	}
}

func TestWrapDEKTamperedLastByte(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	dek := make([]byte, crypto.DEKSize)
	for i := range dek {
		dek[i] = byte(i)
	}

	wrapped, err := crypto.WrapDEK(crypto.ModeAES, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK failed: %v", err)
	}

	// Flip the last byte (tag)
	wrapped[len(wrapped)-1] ^= 0x01

	_, err = crypto.UnwrapDEK(crypto.ModeAES, kek, wrapped)
	if err == nil {
		t.Fatal("UnwrapDEK with tampered tag: expected error")
	}
}

func TestWrapDEKTamperedNonce(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	dek := make([]byte, crypto.DEKSize)

	wrapped, err := crypto.WrapDEK(crypto.ModeAES, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK failed: %v", err)
	}

	// Flip a byte in the nonce (first 12 bytes)
	wrapped[5] ^= 0x01

	_, err = crypto.UnwrapDEK(crypto.ModeAES, kek, wrapped)
	if err == nil {
		t.Fatal("UnwrapDEK with tampered nonce: expected error")
	}
}

func TestWrapDEKInvalidDEKSize(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	_, err := crypto.WrapDEK(crypto.ModeAES, kek, make([]byte, 8))
	if err == nil {
		t.Fatal("WrapDEK with 8-byte DEK: expected error")
	}
}

func TestUnwrapDEKInvalidWrappedLength(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	_, err := crypto.UnwrapDEK(crypto.ModeAES, kek, make([]byte, 10))
	if err == nil {
		t.Fatal("UnwrapDEK with 10-byte wrapped: expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidWrappedDEKLength) {
		t.Errorf("expected errors.Is(err, ErrInvalidWrappedDEKLength), got %v", err)
	}
}

func TestWrapDEKWrongKeySizeAES(t *testing.T) {
	_, err := crypto.WrapDEK(crypto.ModeAES, make([]byte, 8), make([]byte, crypto.DEKSize))
	if err == nil {
		t.Fatal("WrapDEK(ModeAES) with 8-byte key: expected error")
	}
}

func TestWrapDEKWrongKeySizeSM4(t *testing.T) {
	_, err := crypto.WrapDEK(crypto.ModeSM4, make([]byte, 8), make([]byte, crypto.DEKSize))
	if err == nil {
		t.Fatal("WrapDEK(ModeSM4) with 8-byte key: expected error")
	}
}

func TestWrapDEKInvalidMode(t *testing.T) {
	_, err := crypto.WrapDEK(crypto.Mode(0x00), make([]byte, 16), make([]byte, crypto.DEKSize))
	if err == nil {
		t.Fatal("WrapDEK with invalid mode: expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidMode) {
		t.Errorf("expected errors.Is(err, ErrInvalidMode), got %v", err)
	}
}

func TestUnwrapDEKInvalidMode(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	dek := make([]byte, crypto.DEKSize)
	wrapped, err := crypto.WrapDEK(crypto.ModeAES, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK failed: %v", err)
	}

	_, err = crypto.UnwrapDEK(crypto.Mode(0x00), kek, wrapped)
	if err == nil {
		t.Fatal("UnwrapDEK with invalid mode: expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidMode) {
		t.Errorf("expected errors.Is(err, ErrInvalidMode), got %v", err)
	}
}

// ---------------------------------------------------------------------------
// ParseMode / Mode.String
// ---------------------------------------------------------------------------

func TestParseModeAES(t *testing.T) {
	m, err := crypto.ParseMode("aes")
	if err != nil {
		t.Fatalf("ParseMode(\"aes\") failed: %v", err)
	}
	if m != crypto.ModeAES {
		t.Errorf("ParseMode(\"aes\") = %v, want ModeAES", m)
	}
}

func TestParseModeSM4(t *testing.T) {
	m, err := crypto.ParseMode("sm")
	if err != nil {
		t.Fatalf("ParseMode(\"sm\") failed: %v", err)
	}
	if m != crypto.ModeSM4 {
		t.Errorf("ParseMode(\"sm\") = %v, want ModeSM4", m)
	}
}

func TestParseModeUnknown(t *testing.T) {
	_, err := crypto.ParseMode("unknown")
	if err == nil {
		t.Fatal("ParseMode(\"unknown\"): expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidMode) {
		t.Errorf("expected errors.Is(err, ErrInvalidMode), got %v", err)
	}
}

func TestParseModeEmpty(t *testing.T) {
	_, err := crypto.ParseMode("")
	if err == nil {
		t.Fatal("ParseMode(\"\"): expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidMode) {
		t.Errorf("expected errors.Is(err, ErrInvalidMode), got %v", err)
	}
}

func TestModeStringAES(t *testing.T) {
	if got := crypto.ModeAES.String(); got != "aes" {
		t.Errorf("ModeAES.String() = %q, want \"aes\"", got)
	}
}

func TestModeStringSM4(t *testing.T) {
	if got := crypto.ModeSM4.String(); got != "sm" {
		t.Errorf("ModeSM4.String() = %q, want \"sm\"", got)
	}
}

func TestModeStringUnknown(t *testing.T) {
	m := crypto.Mode(0x99)
	got := m.String()
	want := "unknown(153)"
	if got != want {
		t.Errorf("Mode(0x99).String() = %q, want %q", got, want)
	}
}

// ---------------------------------------------------------------------------
// ProbeUnwrapDEK — wrap/unwrap round-trip probe
// ---------------------------------------------------------------------------

func TestProbeUnwrapDEK_FreshlyWrapped_ReturnsNil(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	for i := range kek {
		kek[i] = byte(i)
	}
	dek := make([]byte, crypto.DEKSize)
	for i := range dek {
		dek[i] = byte(0x10 + i)
	}

	wrapped, err := crypto.WrapDEK(crypto.ModeAES, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK failed: %v", err)
	}

	if err := crypto.ProbeUnwrapDEK(crypto.ModeAES, kek, dek, wrapped); err != nil {
		t.Errorf("ProbeUnwrapDEK on freshly wrapped DEK: %v", err)
	}
}

func TestProbeUnwrapDEK_FreshlyWrapped_SM4(t *testing.T) {
	kek := make([]byte, crypto.SM4KeySize)
	for i := range kek {
		kek[i] = byte(i)
	}
	dek := make([]byte, crypto.DEKSize)
	for i := range dek {
		dek[i] = byte(0x20 + i)
	}

	wrapped, err := crypto.WrapDEK(crypto.ModeSM4, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK failed: %v", err)
	}

	if err := crypto.ProbeUnwrapDEK(crypto.ModeSM4, kek, dek, wrapped); err != nil {
		t.Errorf("ProbeUnwrapDEK on freshly wrapped DEK SM4: %v", err)
	}
}

func TestProbeUnwrapDEK_WrongKEK_ReturnsError(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	for i := range kek {
		kek[i] = byte(i)
	}
	dek := make([]byte, crypto.DEKSize)
	for i := range dek {
		dek[i] = byte(0x10 + i)
	}

	wrapped, err := crypto.WrapDEK(crypto.ModeAES, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK failed: %v", err)
	}

	wrongKEK := make([]byte, crypto.AESKeySize)
	for i := range wrongKEK {
		wrongKEK[i] = byte(0xFF)
	}

	if err := crypto.ProbeUnwrapDEK(crypto.ModeAES, wrongKEK, dek, wrapped); err == nil {
		t.Error("ProbeUnwrapDEK with wrong KEK: expected error")
	}
}

func TestProbeUnwrapDEK_WrongDEK_AAD_ReturnsError(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	for i := range kek {
		kek[i] = byte(i)
	}
	dek := make([]byte, crypto.DEKSize)
	for i := range dek {
		dek[i] = byte(0x10 + i)
	}

	wrapped, err := crypto.WrapDEK(crypto.ModeAES, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK failed: %v", err)
	}

	wrongDEK := make([]byte, crypto.DEKSize)
	for i := range wrongDEK {
		wrongDEK[i] = byte(0xAA)
	}

	if err := crypto.ProbeUnwrapDEK(crypto.ModeAES, kek, wrongDEK, wrapped); err == nil {
		t.Error("ProbeUnwrapDEK with wrong DEK (AAD): expected error")
	}
}

func TestProbeUnwrapDEK_CorruptedBlob_ReturnsError(t *testing.T) {
	kek := make([]byte, crypto.AESKeySize)
	for i := range kek {
		kek[i] = byte(i)
	}
	dek := make([]byte, crypto.DEKSize)
	for i := range dek {
		dek[i] = byte(0x10 + i)
	}

	wrapped, err := crypto.WrapDEK(crypto.ModeAES, kek, dek)
	if err != nil {
		t.Fatalf("WrapDEK failed: %v", err)
	}

	wrapped[len(wrapped)-1] ^= 0x01 // flip last tag byte

	if err := crypto.ProbeUnwrapDEK(crypto.ModeAES, kek, dek, wrapped); err == nil {
		t.Error("ProbeUnwrapDEK with corrupted blob: expected error")
	}
}

func TestProbeUnwrapDEK_InvalidWrappedLength(t *testing.T) {
	if err := crypto.ProbeUnwrapDEK(crypto.ModeAES, make([]byte, 16), make([]byte, 16), make([]byte, 10)); err == nil {
		t.Error("ProbeUnwrapDEK with 10-byte wrapped: expected error")
	}
}

func TestProbeUnwrapDEK_InvalidMode(t *testing.T) {
	if err := crypto.ProbeUnwrapDEK(crypto.Mode(0x00), make([]byte, 16), make([]byte, 16), make([]byte, 44)); err == nil {
		t.Error("ProbeUnwrapDEK with invalid mode: expected error")
	}
}
