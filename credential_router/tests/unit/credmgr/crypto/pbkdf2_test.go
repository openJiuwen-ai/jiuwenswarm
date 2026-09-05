package crypto_test

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"testing"

	"github.com/tjfoc/gmsm/sm3"

	"credential_router/internal/credmgr/crypto"
)

// Known-answer test vectors derived from:
//   password = []byte("password")
//   salt     = bytes.Repeat([]byte{0x01}, 16)
//   iter     = 10000 (PBKDF2Iterations)
//   keyLen   = 16 (KEKSize)
//
// SHA-256: 92f1997ea77b70e4db72150a551d2723
// SM3:     91cf0bce52af1a8a6b1cd05f81d4a722

var (
	testPassword = []byte("password")
	testSalt     = bytes.Repeat([]byte{0x01}, crypto.SaltSize)
)

func TestPBKDF2KnownAnswerSHA256(t *testing.T) {
	kb, err := crypto.PBKDF2HMAC(testPassword, testSalt, sha256.New)
	if err != nil {
		t.Fatalf("PBKDF2HMAC failed: %v", err)
	}
	want := []byte{0x92, 0xf1, 0x99, 0x7e, 0xa7, 0x7b, 0x70, 0xe4, 0xdb, 0x72, 0x15, 0x0a, 0x55, 0x1d, 0x27, 0x23}
	got := kb.Bytes()
	if !bytes.Equal(got, want) {
		t.Errorf("PBKDF2-SHA256 key = %x, want %x", got, want)
	}
}

func TestPBKDF2KnownAnswerSM3(t *testing.T) {
	kb, err := crypto.PBKDF2HMAC(testPassword, testSalt, sm3.New)
	if err != nil {
		t.Fatalf("PBKDF2HMAC failed: %v", err)
	}
	want := []byte{0x91, 0xcf, 0x0b, 0xce, 0x52, 0xaf, 0x1a, 0x8a, 0x6b, 0x1c, 0xd0, 0x5f, 0x81, 0xd4, 0xa7, 0x22}
	got := kb.Bytes()
	if !bytes.Equal(got, want) {
		t.Errorf("PBKDF2-SM3 key = %x, want %x", got, want)
	}
}

func TestPBKDF2WrongSaltLen15(t *testing.T) {
	salt := make([]byte, 15)
	_, err := crypto.PBKDF2HMAC(testPassword, salt, sha256.New)
	if err == nil {
		t.Fatal("PBKDF2HMAC with 15-byte salt: expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidSaltLen) {
		t.Errorf("expected errors.Is(err, ErrInvalidSaltLen), got %v", err)
	}
}

func TestPBKDF2WrongSaltLen17(t *testing.T) {
	salt := make([]byte, 17)
	_, err := crypto.PBKDF2HMAC(testPassword, salt, sha256.New)
	if err == nil {
		t.Fatal("PBKDF2HMAC with 17-byte salt: expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidSaltLen) {
		t.Errorf("expected errors.Is(err, ErrInvalidSaltLen), got %v", err)
	}
}

func TestPBKDF2EmptySalt(t *testing.T) {
	_, err := crypto.PBKDF2HMAC(testPassword, nil, sha256.New)
	if err == nil {
		t.Fatal("PBKDF2HMAC with nil salt: expected error")
	}
	if !errors.Is(err, crypto.ErrInvalidSaltLen) {
		t.Errorf("expected errors.Is(err, ErrInvalidSaltLen), got %v", err)
	}
}

func TestPBKDF2NilHashFunc(t *testing.T) {
	_, err := crypto.PBKDF2HMAC(testPassword, testSalt, nil)
	if err == nil {
		t.Fatal("PBKDF2HMAC with nil hashFunc: expected error")
	}
	if !errors.Is(err, crypto.ErrNilHashFunc) {
		t.Errorf("expected errors.Is(err, ErrNilHashFunc), got %v", err)
	}
}

func TestPBKDF2OutputLength(t *testing.T) {
	kb, err := crypto.PBKDF2HMAC(testPassword, testSalt, sha256.New)
	if err != nil {
		t.Fatalf("PBKDF2HMAC failed: %v", err)
	}
	if kb.Len() != crypto.KEKSize {
		t.Errorf("KeyBytes.Len() = %d, want %d", kb.Len(), crypto.KEKSize)
	}
}

func TestPBKDF2Deterministic(t *testing.T) {
	kb1, err := crypto.PBKDF2HMAC(testPassword, testSalt, sha256.New)
	if err != nil {
		t.Fatalf("first PBKDF2HMAC failed: %v", err)
	}
	kb2, err := crypto.PBKDF2HMAC(testPassword, testSalt, sha256.New)
	if err != nil {
		t.Fatalf("second PBKDF2HMAC failed: %v", err)
	}
	if !bytes.Equal(kb1.Bytes(), kb2.Bytes()) {
		t.Errorf("PBKDF2HMAC is not deterministic: got %x and %x", kb1.Bytes(), kb2.Bytes())
	}
}

func TestPBKDF2DifferentSalts(t *testing.T) {
	salt2 := bytes.Repeat([]byte{0x02}, crypto.SaltSize)
	kb1, err := crypto.PBKDF2HMAC(testPassword, testSalt, sha256.New)
	if err != nil {
		t.Fatalf("PBKDF2HMAC with salt1 failed: %v", err)
	}
	kb2, err := crypto.PBKDF2HMAC(testPassword, salt2, sha256.New)
	if err != nil {
		t.Fatalf("PBKDF2HMAC with salt2 failed: %v", err)
	}
	if bytes.Equal(kb1.Bytes(), kb2.Bytes()) {
		t.Errorf("different salts produced same key: %x", kb1.Bytes())
	}
}

func TestPBKDF2DifferentPasswords(t *testing.T) {
	password2 := []byte("P@ssw0rd!")
	kb1, err := crypto.PBKDF2HMAC(testPassword, testSalt, sha256.New)
	if err != nil {
		t.Fatalf("PBKDF2HMAC with password1 failed: %v", err)
	}
	kb2, err := crypto.PBKDF2HMAC(password2, testSalt, sha256.New)
	if err != nil {
		t.Fatalf("PBKDF2HMAC with password2 failed: %v", err)
	}
	if bytes.Equal(kb1.Bytes(), kb2.Bytes()) {
		t.Errorf("different passwords produced same key: %x", kb1.Bytes())
	}
}

func TestPBKDF2SM3Deterministic(t *testing.T) {
	kb1, err := crypto.PBKDF2HMAC(testPassword, testSalt, sm3.New)
	if err != nil {
		t.Fatalf("first PBKDF2HMAC(SM3) failed: %v", err)
	}
	kb2, err := crypto.PBKDF2HMAC(testPassword, testSalt, sm3.New)
	if err != nil {
		t.Fatalf("second PBKDF2HMAC(SM3) failed: %v", err)
	}
	if !bytes.Equal(kb1.Bytes(), kb2.Bytes()) {
		t.Errorf("PBKDF2HMAC(SM3) is not deterministic: got %x and %x", kb1.Bytes(), kb2.Bytes())
	}
}

func TestPBKDF2SM3AndSHA256Differ(t *testing.T) {
	kbSHA, err := crypto.PBKDF2HMAC(testPassword, testSalt, sha256.New)
	if err != nil {
		t.Fatalf("PBKDF2HMAC(SHA-256) failed: %v", err)
	}
	kbSM3, err := crypto.PBKDF2HMAC(testPassword, testSalt, sm3.New)
	if err != nil {
		t.Fatalf("PBKDF2HMAC(SM3) failed: %v", err)
	}
	if bytes.Equal(kbSHA.Bytes(), kbSM3.Bytes()) {
		t.Errorf("SHA-256 and SM3 produced identical keys: %x", kbSHA.Bytes())
	}
}

func TestPBKDF2WrongSaltLen15ErrorsIs(t *testing.T) {
	_, err := crypto.PBKDF2HMAC(testPassword, make([]byte, 15), sha256.New)
	if !errors.Is(err, crypto.ErrInvalidSaltLen) {
		t.Errorf("expected errors.Is(err, ErrInvalidSaltLen), got %v", err)
	}
}

func TestPBKDF2EmptySaltErrorsIs(t *testing.T) {
	_, err := crypto.PBKDF2HMAC(testPassword, []byte{}, sha256.New)
	if !errors.Is(err, crypto.ErrInvalidSaltLen) {
		t.Errorf("expected errors.Is(err, ErrInvalidSaltLen), got %v", err)
	}
}

func TestErrNilHashFuncIs(t *testing.T) {
	_, err := crypto.PBKDF2HMAC(testPassword, testSalt, nil)
	if !errors.Is(err, crypto.ErrNilHashFunc) {
		t.Errorf("expected errors.Is(err, ErrNilHashFunc), got %v", err)
	}
}
