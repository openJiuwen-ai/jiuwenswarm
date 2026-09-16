//go:build cgo

// Fuzz tests for internal/crypto.
//
// Quick:  go test -run=^$ -fuzz=^Fuzz -fuzztime=10s ./internal/crypto/...
// Full:   go test -run=^$ -fuzz=^Fuzz -fuzztime=60s ./internal/crypto/...
//
// Invariants exercised:
//   - AES/SM4-GCM round-trip: decrypt(encrypt(x)) == x; tamper with one
//     ciphertext byte MUST fail authentication (AEAD tag check).
//   - EncryptCredential/WrapDEK always return the documented byte layout.
//   - PBKDF2HMAC is deterministic, always yields a 16-byte key, and is
//     avalanche-sensitive (one password byte flip → different key).
//   - KeyBytes deep-copy semantics and zeroisation never panic.
//   - ParseMode round-trips exactly for the two known modes.
package crypto_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"testing"

	"github.com/tjfoc/gmsm/sm3"

	"credential_router/internal/credmgr/crypto"
)

// padKey returns a slice of exactly size bytes: data is copied in and the
// remainder is zero-padded. This keeps every fuzz-derived key at a valid
// length so the targets can distinguish "key too short" errors (which the
// functions must reject) from genuine round-trip failures.
func padKey(data []byte, size int) []byte {
	out := make([]byte, size)
	copy(out, data)
	return out
}

// splitKeyPayload splits a fuzz input into a fixed-size 16-byte key and the
// remaining bytes as the plaintext payload (possibly empty).
func splitKeyPayload(data []byte) (key, payload []byte) {
	return padKey(data, crypto.AESKeySize), data[min(len(data), crypto.AESKeySize):]
}

// FuzzAESRoundTrip — AES-128-GCM must round-trip and reject tampering.
// The AEAD tag check is the security boundary: flipping a single ciphertext
// byte must always fail decryption, never silently return a wrong plaintext.
func FuzzAESRoundTrip(f *testing.F) {
	knownValid := make([]byte, crypto.AESKeySize+len("hello, aes-128-gcm"))
	copy(knownValid[:crypto.AESKeySize], []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15})
	copy(knownValid[crypto.AESKeySize:], []byte("hello, aes-128-gcm"))

	f.Add([]byte{})                         // empty
	f.Add([]byte{0x00})                     // single byte
	f.Add(bytes.Repeat([]byte{0xff}, 1024)) // max-ish length
	f.Add(knownValid)                       // known-valid key+plaintext
	f.Add([]byte{0xde, 0xad})               // short key (padded), empty payload
	f.Add(bytes.Repeat([]byte{0x00}, 300))  // long zero payload
	f.Fuzz(func(t *testing.T, data []byte) {
		key, pt := splitKeyPayload(data)

		nonce, ct, err := crypto.AESEncrypt(key, pt)
		if err != nil {
			t.Fatalf("AESEncrypt(16B key) failed: %v", err)
		}
		if len(ct) != len(pt)+crypto.TagSize {
			t.Fatalf("ciphertext len=%d, want %d", len(ct), len(pt)+crypto.TagSize)
		}

		got, err := crypto.AESDecrypt(key, nonce, ct)
		if err != nil {
			t.Fatalf("round-trip decrypt failed for %d-byte plaintext: %v", len(pt), err)
		}
		if !bytes.Equal(got, pt) {
			t.Fatalf("round-trip mismatch: got %x, want %x", got, pt)
		}

		// Tamper: flip the first ciphertext byte. GCM must reject it.
		tampered := append([]byte(nil), ct...)
		tampered[0] ^= 0x01
		if _, err := crypto.AESDecrypt(key, nonce, tampered); err == nil {
			t.Fatalf("AESDecrypt accepted tampered ciphertext (len=%d)", len(ct))
		}

		// Arbitrary nonce/ciphertext sizes must return a clean error, never panic.
		_, _ = crypto.AESDecrypt(key, data[:len(data)%13], data)
	})
}

// FuzzSM4RoundTrip — SM4-GCM must round-trip and reject tampering, same as AES.
func FuzzSM4RoundTrip(f *testing.F) {
	knownValid := make([]byte, crypto.SM4KeySize+len("hello, sm4-gcm"))
	copy(knownValid[:crypto.SM4KeySize], []byte{15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0})
	copy(knownValid[crypto.SM4KeySize:], []byte("hello, sm4-gcm"))

	f.Add([]byte{})                         // empty
	f.Add([]byte{0x00})                     // single byte
	f.Add(bytes.Repeat([]byte{0xff}, 1024)) // max-ish length
	f.Add(knownValid)                       // known-valid key+plaintext
	f.Add(bytes.Repeat([]byte{0xab}, 64))   // 64-byte payload
	f.Fuzz(func(t *testing.T, data []byte) {
		key, pt := splitKeyPayload(data)

		nonce, ct, err := crypto.SM4Encrypt(key, pt)
		if err != nil {
			t.Fatalf("SM4Encrypt(16B key) failed: %v", err)
		}
		if len(ct) != len(pt)+crypto.TagSize {
			t.Fatalf("ciphertext len=%d, want %d", len(ct), len(pt)+crypto.TagSize)
		}

		got, err := crypto.SM4Decrypt(key, nonce, ct)
		if err != nil {
			t.Fatalf("round-trip decrypt failed for %d-byte plaintext: %v", len(pt), err)
		}
		if !bytes.Equal(got, pt) {
			t.Fatalf("round-trip mismatch: got %x, want %x", got, pt)
		}

		tampered := append([]byte(nil), ct...)
		tampered[0] ^= 0x01
		if _, err := crypto.SM4Decrypt(key, nonce, tampered); err == nil {
			t.Fatalf("SM4Decrypt accepted tampered ciphertext (len=%d)", len(ct))
		}

		_, _ = crypto.SM4Decrypt(key, data[:len(data)%13], data)
	})
}

// splitCredential splits a fuzz input into [mode byte][16-byte key][payload].
func splitCredential(data []byte) (mode crypto.Mode, key, payload []byte) {
	buf := make([]byte, 1+crypto.AESKeySize)
	copy(buf, data)
	mode = crypto.Mode(buf[0])
	key = buf[1:]
	if len(data) > 1+crypto.AESKeySize {
		payload = data[1+crypto.AESKeySize:]
	}
	return mode, key, payload
}

// FuzzCredentialRoundTrip — the blob format layer on top of the AEADs:
// [format(1) || nonce(12) || ciphertext+tag]. A valid mode must always
// round-trip; an invalid mode must error cleanly; the blob must never be
// accepted by the other mode (format byte mismatch).
func FuzzCredentialRoundTrip(f *testing.F) {
	keyAES := []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15}
	keySM4 := []byte{15, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0}
	validAES := append(append([]byte{0x01}, keyAES...), []byte("sk-test-aes")...)
	validSM4 := append(append([]byte{0x02}, keySM4...), []byte("sk-test-sm4")...)

	f.Add(validAES)                       // known-valid AES
	f.Add(validSM4)                       // known-valid SM4
	f.Add([]byte{})                       // empty
	f.Add([]byte{0x00})                   // invalid mode 0x00
	f.Add([]byte{0x7f, 0x00, 0x01})       // invalid mode 0x7f
	f.Add(bytes.Repeat([]byte{'A'}, 300)) // long garbage payload
	f.Add([]byte{0x01, 0x00})             // valid mode, zero key, empty payload
	f.Fuzz(func(t *testing.T, data []byte) {
		mode, key, pt := splitCredential(data)

		blob, err := crypto.EncryptCredential(mode, key, pt)
		if err != nil {
			if mode != crypto.ModeAES && mode != crypto.ModeSM4 {
				return // invalid mode: error is the expected contract
			}
			t.Fatalf("EncryptCredential(%v) with 16B key failed: %v", mode, err)
		}
		if len(blob) != crypto.FormatByteSize+crypto.NonceSize+len(pt)+crypto.TagSize {
			t.Fatalf("blob len=%d, want %d", len(blob), crypto.FormatByteSize+crypto.NonceSize+len(pt)+crypto.TagSize)
		}

		got, err := crypto.DecryptCredential(mode, key, blob)
		if err != nil {
			t.Fatalf("round-trip decrypt failed for %d-byte plaintext: %v", len(pt), err)
		}
		if !bytes.Equal(got, pt) {
			t.Fatalf("round-trip mismatch: got %x, want %x", got, pt)
		}

		// Tamper the first ciphertext byte (after format+nonce) — auth must fail.
		tampered := append([]byte(nil), blob...)
		tampered[crypto.FormatByteSize+crypto.NonceSize] ^= 0x01
		if _, err := crypto.DecryptCredential(mode, key, tampered); err == nil {
			t.Fatalf("DecryptCredential accepted tampered blob (mode=%v)", mode)
		}

		// The other valid mode must reject this blob (format byte mismatch).
		other := crypto.ModeAES
		if mode == crypto.ModeAES {
			other = crypto.ModeSM4
		}
		if _, err := crypto.DecryptCredential(other, key, blob); err == nil {
			t.Fatalf("blob encrypted under %v decrypted under %v", mode, other)
		}

		// Arbitrary blob input must never panic.
		_, _ = crypto.DecryptCredential(mode, key, data)
	})
}

// splitDEK splits a fuzz input into [mode byte][16-byte KEK][16-byte DEK][rest].
func splitDEK(data []byte) (mode crypto.Mode, kek, dek, rest []byte) {
	buf := make([]byte, 1+crypto.AESKeySize+crypto.DEKSize)
	copy(buf, data)
	mode = crypto.Mode(buf[0])
	kek = buf[1 : 1+crypto.AESKeySize]
	dek = buf[1+crypto.AESKeySize : 1+crypto.AESKeySize+crypto.DEKSize]
	if len(data) > 1+crypto.AESKeySize+crypto.DEKSize {
		rest = data[1+crypto.AESKeySize+crypto.DEKSize:]
	}
	return mode, kek, dek, rest
}

// FuzzDEKWrapUnwrap — the KEK-wraps-DEK layer. Wrap/Unwrap/Probe must be
// consistent, tampering must be caught, and arbitrary wrapped blobs must never
// panic (including the exact 44-byte length, which reaches the GCM path).
func FuzzDEKWrapUnwrap(f *testing.F) {
	valid := make([]byte, 1+crypto.AESKeySize+crypto.DEKSize+crypto.WrappedDEKSize)
	copy(valid[1:], []byte{0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15})                                             // KEK
	copy(valid[1+crypto.AESKeySize:], []byte{200, 201, 202, 203, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213, 214, 215}) // DEK

	f.Add([]byte{})                        // empty
	f.Add(valid)                           // known-valid layout
	f.Add([]byte{0x00, 0x01, 0x02})        // invalid mode, short
	f.Add([]byte{0x01})                    // valid mode, zero KEK/DEK, no rest
	f.Add(bytes.Repeat([]byte{0xcc}, 256)) // long garbage
	f.Fuzz(func(t *testing.T, data []byte) {
		mode, kek, dek, rest := splitDEK(data)

		wrapped, err := crypto.WrapDEK(mode, kek, dek)
		if err != nil {
			if mode != crypto.ModeAES && mode != crypto.ModeSM4 {
				return // invalid mode: expected error
			}
			t.Fatalf("WrapDEK(%v) with valid sizes failed: %v", mode, err)
		}
		if len(wrapped) != crypto.WrappedDEKSize {
			t.Fatalf("wrapped len=%d, want %d", len(wrapped), crypto.WrappedDEKSize)
		}

		unwrapped, err := crypto.UnwrapDEK(mode, kek, wrapped)
		if err != nil {
			t.Fatalf("UnwrapDEK round-trip failed: %v", err)
		}
		if !bytes.Equal(unwrapped, dek) {
			t.Fatalf("UnwrapDEK mismatch: got %x, want %x", unwrapped, dek)
		}
		if err := crypto.ProbeUnwrapDEK(mode, kek, dek, wrapped); err != nil {
			t.Fatalf("ProbeUnwrapDEK failed on valid blob: %v", err)
		}

		// Probe with a definitely-wrong DEK must fail (plaintext mismatch).
		wrongDEK := bytes.Repeat([]byte{0xff}, crypto.DEKSize)
		if bytes.Equal(wrongDEK, dek) {
			wrongDEK[0] ^= 0x01
		}
		if err := crypto.ProbeUnwrapDEK(mode, kek, wrongDEK, wrapped); err == nil {
			t.Fatalf("ProbeUnwrapDEK accepted wrong DEK")
		}

		// Tamper the first wrapped byte — auth must fail.
		tampered := append([]byte(nil), wrapped...)
		tampered[0] ^= 0x01
		if _, err := crypto.UnwrapDEK(mode, kek, tampered); err == nil {
			t.Fatalf("UnwrapDEK accepted tampered wrapped DEK")
		}

		// Arbitrary wrapped blobs must return an error, never panic.
		_, _ = crypto.UnwrapDEK(mode, kek, rest)
		_ = crypto.ProbeUnwrapDEK(mode, kek, dek, rest)
	})
}

// splitPBKDF2 splits a fuzz input into a 16-byte salt (zero-padded) and the
// remaining bytes as the password.
func splitPBKDF2(data []byte) (password, salt []byte) {
	if len(data) <= crypto.SaltSize {
		return nil, padKey(data, crypto.SaltSize)
	}
	return data[crypto.SaltSize:], data[:crypto.SaltSize]
}

// FuzzPBKDF2HMAC — PBKDF2-HMAC is the KEK derivation primitive. It must be
// deterministic, always yield KEKSize bytes, and be avalanche-sensitive to a
// single password byte flip. The nil-hash path must error cleanly.
func FuzzPBKDF2HMAC(f *testing.F) {
	salt := bytes.Repeat([]byte{0x5a}, crypto.SaltSize)
	f.Add(append(salt, []byte("correct horse battery staple")...)) // known-valid
	f.Add(make([]byte, crypto.SaltSize))                           // zero salt, empty password
	f.Add(append(salt, 0x00))                                      // single-byte password
	f.Add(bytes.Repeat([]byte{0x01}, crypto.SaltSize+64))          // 64-byte password
	f.Add(append(salt, []byte("中文密码-password")...))                // multi-byte password
	f.Add([]byte{})                                                // empty → zero salt + empty password
	f.Fuzz(func(t *testing.T, data []byte) {
		password, salt := splitPBKDF2(data)

		if _, err := crypto.PBKDF2HMAC(password, salt, nil); err == nil {
			t.Fatalf("nil hash func must error")
		}

		k1, err := crypto.PBKDF2HMAC(password, salt, sha256.New)
		if err != nil {
			t.Fatalf("PBKDF2HMAC with 16B salt failed: %v", err)
		}
		if k1 == nil || k1.Len() != crypto.KEKSize {
			t.Fatalf("derived key len=%v, want %d", k1, crypto.KEKSize)
		}

		// Determinism.
		k2, err := crypto.PBKDF2HMAC(password, salt, sha256.New)
		if err != nil {
			t.Fatalf("second derivation failed: %v", err)
		}
		if !bytes.Equal(k1.Bytes(), k2.Bytes()) {
			t.Fatalf("PBKDF2HMAC not deterministic")
		}

		// Avalanche: flip one byte of the password → different key.
		if len(password) > 0 {
			mp := append([]byte(nil), password...)
			mp[0] ^= 0x01
			k3, err := crypto.PBKDF2HMAC(mp, salt, sha256.New)
			if err != nil {
				t.Fatalf("mutated derivation failed: %v", err)
			}
			if bytes.Equal(k1.Bytes(), k3.Bytes()) {
				t.Fatalf("avalanche violated: single password-byte flip produced identical key")
			}
		}

		// SM3 PRF variant must also produce a KEKSize key.
		ks, err := crypto.PBKDF2HMAC(password, salt, sm3.New)
		if err != nil {
			t.Fatalf("sm3 PBKDF2HMAC failed: %v", err)
		}
		if ks.Len() != crypto.KEKSize {
			t.Fatalf("sm3 derived key len=%d, want %d", ks.Len(), crypto.KEKSize)
		}
	})
}

// FuzzKeyBytes — the defensive key wrapper must deep-copy on construct and
// read, track length/hex/zero status correctly, and zeroise explicitly.
func FuzzKeyBytes(f *testing.F) {
	f.Add([]byte{})                        // empty
	f.Add([]byte{0x00})                    // single zero byte
	f.Add([]byte{0xde, 0xad, 0xbe, 0xef})  // short key
	f.Add(bytes.Repeat([]byte{0xff}, 16))  // 16-byte key
	f.Add(bytes.Repeat([]byte{0x42}, 300)) // 300-byte key
	f.Add(bytes.Repeat([]byte{0x00}, 64))  // all-zero key
	f.Fuzz(func(t *testing.T, data []byte) {
		kb := crypto.NewKeyBytes(data)
		if kb.Len() != len(data) {
			t.Fatalf("Len=%d, want %d", kb.Len(), len(data))
		}
		got := kb.Bytes()
		if !bytes.Equal(got, data) {
			t.Fatalf("Bytes() mismatch")
		}
		// Mutating the returned slice must not corrupt the wrapper.
		if len(got) > 0 {
			got[0] ^= 0x01
			if !bytes.Equal(kb.Bytes(), data) {
				t.Fatalf("Bytes() returned aliased internal buffer")
			}
		}
		if kb.Hex() != hex.EncodeToString(data) {
			t.Fatalf("Hex mismatch")
		}

		wantZero := true
		for _, b := range data {
			if b != 0 {
				wantZero = false
				break
			}
		}
		if kb.IsZero() != wantZero {
			t.Fatalf("IsZero=%v, want %v", kb.IsZero(), wantZero)
		}

		kb2 := crypto.NewKeyBytes(data)
		kb2.Zero()
		if !kb2.IsZero() || kb2.Len() != len(data) {
			t.Fatalf("Zero() must clear bytes but preserve length")
		}

		// NewRandomKey with a fuzz-bounded length.
		rk, err := crypto.NewRandomKey(len(data) % 64)
		if err != nil {
			t.Fatalf("NewRandomKey(%d) failed: %v", len(data)%64, err)
		}
		if rk.Len() != len(data)%64 {
			t.Fatalf("NewRandomKey len=%d, want %d", rk.Len(), len(data)%64)
		}
	})
}

// FuzzParseMode — parsing must round-trip exactly for the two known modes and
// reject everything else cleanly. Mode.String() must never panic on any byte.
func FuzzParseMode(f *testing.F) {
	f.Add("aes")
	f.Add("sm")
	f.Add("")
	f.Add("unknown")
	f.Add("AES")
	f.Add(" sm")
	f.Add("aes\x00")
	f.Add(string(bytes.Repeat([]byte{'x'}, 300)))
	f.Fuzz(func(t *testing.T, s string) {
		m, err := crypto.ParseMode(s)
		if err == nil {
			if m.String() != s {
				t.Fatalf("ParseMode(%q) round-trip → %q", s, m.String())
			}
			if m != crypto.ModeAES && m != crypto.ModeSM4 {
				t.Fatalf("ParseMode(%q) returned invalid mode %v", s, m)
			}
		}
		for _, b := range []byte(s) {
			_ = crypto.Mode(b).String() // must never panic
		}
	})
}
