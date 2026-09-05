package crypto

import (
	"bytes"
	"crypto/aes"
	"crypto/cipher"
	"errors"
	"fmt"

	"github.com/tjfoc/gmsm/sm4"
)

// Mode represents the cipher mode for credential encryption and DEK wrapping.
type Mode uint8

const (
	// ModeAES selects AES-128-GCM encryption. The on-disk wire format is
	// 1B format + 12B nonce + N ciphertext + 16B tag.
	ModeAES Mode = 0x01
	// ModeSM4 selects SM4-GCM encryption. Same wire format as ModeAES;
	// only the underlying block cipher changes.
	ModeSM4 Mode = 0x02
)

const (
	// FormatByteSize is the size of the format version prefix byte.
	FormatByteSize = 1

	// DEKSize is the required data-encryption-key size in bytes (16 bytes,
	// 128-bit AES or SM4 symmetric key).
	DEKSize = 16

	// ShardSize is the byte length of one KEK shard (S1, S2, S3 are
	// each ShardSize bytes). The 16-byte KEK is derived by PBKDF2
	// over the XOR of all three shards (XorThree in shards.go).
	// S3 is hardcoded in the binary; S1 is a local file, S2 lives in
	// the SQLite key_metadata table. All three are required — no
	// single shard or pair alone can reconstruct the KEK.
	ShardSize = 32

	// WrappedDEKSize is the exact byte length of a wrapped DEK:
	// nonce(12) + ciphertext(16) + tag(16) = 44 bytes.
	WrappedDEKSize = NonceSize + DEKSize + TagSize // 44
)

// Sentinel errors for format-level operations.
var (
	ErrUnsupportedCipherFormat = fmt.Errorf("crypto: unsupported cipher format")
	ErrInvalidMode             = fmt.Errorf("crypto: invalid mode")
	ErrInvalidWrappedDEKLength = fmt.Errorf("crypto: invalid wrapped DEK length")
)

// String returns a human-readable name for the mode.
func (m Mode) String() string {
	switch m {
	case ModeAES:
		return "aes"
	case ModeSM4:
		return "sm"
	default:
		return fmt.Sprintf("unknown(%d)", uint8(m))
	}
}

// ParseMode converts a human-readable mode name into a Mode value.
func ParseMode(s string) (Mode, error) {
	switch s {
	case "aes":
		return ModeAES, nil
	case "sm":
		return ModeSM4, nil
	default:
		return 0, fmt.Errorf("%w: %s", ErrInvalidMode, s)
	}
}

// EncryptCredential encrypts a plaintext API key using the given mode and DEK.
// Returns: [format_version(1B) || nonce(12B) || ciphertext+tag(N+16B)].
func EncryptCredential(mode Mode, key, plaintext []byte) ([]byte, error) {
	var nonce, ct []byte
	var err error
	switch mode {
	case ModeAES:
		nonce, ct, err = AESEncrypt(key, plaintext)
	case ModeSM4:
		nonce, ct, err = SM4Encrypt(key, plaintext)
	default:
		return nil, fmt.Errorf("%w: %v", ErrInvalidMode, mode)
	}
	if err != nil {
		return nil, fmt.Errorf("crypto: EncryptCredential: %w", err)
	}

	out := make([]byte, 0, FormatByteSize+NonceSize+len(ct))
	out = append(out, byte(mode))
	out = append(out, nonce...)
	out = append(out, ct...)
	return out, nil
}

// DecryptCredential decrypts a credential blob, verifying that the format byte
// matches the expected mode. Returns ErrInvalidMode if the mode itself is
// unrecognized, or ErrUnsupportedCipherFormat on format mismatch.
func DecryptCredential(mode Mode, key, blob []byte) ([]byte, error) {
	if mode != ModeAES && mode != ModeSM4 {
		return nil, fmt.Errorf("%w: %v", ErrInvalidMode, mode)
	}
	if len(blob) < FormatByteSize+NonceSize+TagSize {
		return nil, fmt.Errorf("%w: blob too short: %d bytes", ErrInvalidCiphertext, len(blob))
	}
	if Mode(blob[0]) != mode {
		return nil, fmt.Errorf("%w: got 0x%02x, want %v", ErrUnsupportedCipherFormat, blob[0], mode)
	}

	nonce := blob[FormatByteSize : FormatByteSize+NonceSize]
	ct := blob[FormatByteSize+NonceSize:]

	var plaintext []byte
	var err error
	switch mode {
	case ModeAES:
		plaintext, err = AESDecrypt(key, nonce, ct)
	case ModeSM4:
		plaintext, err = SM4Decrypt(key, nonce, ct)
	}
	if err != nil {
		return nil, fmt.Errorf("crypto: DecryptCredential: %w", err)
	}
	return plaintext, nil
}

// WrapDEK encrypts a DEK using the given mode and KEK.
// Returns: [wrap_nonce(12B) || ciphertext(16B) || tag(16B)] = 44 bytes.
func WrapDEK(mode Mode, kek, dek []byte) ([]byte, error) {
	if len(dek) != DEKSize {
		return nil, fmt.Errorf("crypto: invalid DEK size: got %d, want %d", len(dek), DEKSize)
	}

	nonce, ct, err := encryptRaw(mode, kek, dek)
	if err != nil {
		return nil, fmt.Errorf("crypto: WrapDEK: %w", err)
	}

	out := make([]byte, 0, WrappedDEKSize)
	out = append(out, nonce...)
	out = append(out, ct...)
	return out, nil
}

// UnwrapDEK decrypts a 44-byte wrapped DEK using the given mode and KEK.
// Returns the 16-byte DEK or an error.
func UnwrapDEK(mode Mode, kek, wrapped []byte) ([]byte, error) {
	if len(wrapped) != WrappedDEKSize {
		return nil, fmt.Errorf("%w: got %d, want %d", ErrInvalidWrappedDEKLength, len(wrapped), WrappedDEKSize)
	}

	nonce := wrapped[:NonceSize]
	ct := wrapped[NonceSize:]

	var dek []byte
	var err error
	switch mode {
	case ModeAES:
		dek, err = AESDecrypt(kek, nonce, ct)
	case ModeSM4:
		dek, err = SM4Decrypt(kek, nonce, ct)
	default:
		return nil, fmt.Errorf("%w: %v", ErrInvalidMode, mode)
	}
	if err != nil {
		return nil, fmt.Errorf("crypto: UnwrapDEK: %w", err)
	}
	if len(dek) != DEKSize {
		return nil, fmt.Errorf("crypto: unwrapped DEK invalid size: %d", len(dek))
	}
	return dek, nil
}

// ProbeUnwrapDEK unwraps the wrapped DEK blob and verifies that the
// resulting plaintext equals the expected DEK value. On success, this
// confirms two facts at once: the wrapped_dek decrypts cleanly with the
// given KEK (no corruption), and the unwrapped bytes match what we
// already loaded as DEK (no stale wrap from a prior key version).
// Returns an error on any failure.
//
// The wrapped parameter must be exactly WrappedDEKSize (44) bytes:
//
//	nonce(12) || ciphertext+tag(32)
//
// Decryption uses kek as AEAD key with nil AAD (matching WrapDEK).
// After successful AEAD.Open, the plaintext is compared to dek to
// confirm the wrapped DEK matches the already-loaded value. If any
// step fails the returned error wraps the underlying cause.
func ProbeUnwrapDEK(mode Mode, kek, dek, wrapped []byte) error {
	if len(wrapped) != WrappedDEKSize {
		return fmt.Errorf("%w: got %d, want %d", ErrInvalidWrappedDEKLength, len(wrapped), WrappedDEKSize)
	}
	var block cipher.Block
	var err error
	switch mode {
	case ModeAES:
		block, err = aes.NewCipher(kek)
	case ModeSM4:
		block, err = sm4.NewCipher(kek)
	default:
		return fmt.Errorf("%w: %v", ErrInvalidMode, mode)
	}
	if err != nil {
		return fmt.Errorf("crypto: ProbeUnwrapDEK: new cipher: %w", err)
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return fmt.Errorf("crypto: ProbeUnwrapDEK: NewGCM: %w", err)
	}
	nonce := wrapped[:NonceSize]
	ct := wrapped[NonceSize:]
	// Decrypt wrapped blob with nil AAD (matching WrapDEK), then verify
	// that the resulting plaintext matches the expected DEK value.
	plaintext, err := aead.Open(nil, nonce, ct, nil)
	if err != nil {
		return fmt.Errorf("crypto: ProbeUnwrapDEK: %w", err)
	}
	if !bytes.Equal(plaintext, dek) {
		return errors.New("crypto: ProbeUnwrapDEK: plaintext mismatch")
	}
	return nil
}

// encryptRaw is a helper that dispatches to AESEncrypt or SM4Encrypt without
// prepending a format byte. Used internally by WrapDEK.
func encryptRaw(mode Mode, key, plaintext []byte) (nonce, ciphertext []byte, err error) {
	switch mode {
	case ModeAES:
		return AESEncrypt(key, plaintext)
	case ModeSM4:
		return SM4Encrypt(key, plaintext)
	default:
		return nil, nil, fmt.Errorf("%w: %v", ErrInvalidMode, mode)
	}
}
