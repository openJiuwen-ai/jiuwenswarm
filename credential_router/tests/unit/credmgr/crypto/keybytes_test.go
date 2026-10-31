package crypto_test

import (
	"testing"

	"credential_router/internal/credmgr/crypto"
)

func TestNewKeyBytesDeepCopy(t *testing.T) {
	src := []byte{0x01, 0x02, 0x03}
	kb := crypto.NewKeyBytes(src)
	src[0] = 0xff // mutate original
	if kb.Bytes()[0] != 0x01 {
		t.Errorf("NewKeyBytes did not deep copy: got %x, want 01", kb.Bytes()[0])
	}
}

func TestBytesReturnsCopy(t *testing.T) {
	src := []byte{0x01, 0x02, 0x03}
	kb := crypto.NewKeyBytes(src)
	b := kb.Bytes()
	b[0] = 0xff // mutate returned copy
	if kb.Bytes()[0] != 0x01 {
		t.Errorf("Bytes() did not return copy: got %x, want 01", kb.Bytes()[0])
	}
}

func TestZero(t *testing.T) {
	src := []byte{0x01, 0x02, 0x03}
	kb := crypto.NewKeyBytes(src)
	kb.Zero()
	if !kb.IsZero() {
		t.Errorf("Zero() did not clear all bytes")
	}
	for i, v := range kb.Bytes() {
		if v != 0 {
			t.Errorf("Bytes()[%d] = %x, want 00", i, v)
		}
	}
}

func TestIsZero(t *testing.T) {
	if crypto.NewKeyBytes([]byte{0x00, 0x00}).IsZero() != true {
		t.Errorf("IsZero() on zero bytes should be true")
	}
	if crypto.NewKeyBytes([]byte{0x00, 0x01}).IsZero() != false {
		t.Errorf("IsZero() on non-zero bytes should be false")
	}
}

func TestLen(t *testing.T) {
	tests := []struct {
		name string
		src  []byte
		want int
	}{
		{"empty", []byte{}, 0},
		{"16 bytes", make([]byte, 16), 16},
		{"32 bytes", make([]byte, 32), 32},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			kb := crypto.NewKeyBytes(tt.src)
			if got := kb.Len(); got != tt.want {
				t.Errorf("Len() = %d, want %d", got, tt.want)
			}
		})
	}
}

func TestNewRandomKey(t *testing.T) {
	k1, err := crypto.NewRandomKey(16)
	if err != nil {
		t.Fatalf("NewRandomKey(16) failed: %v", err)
	}
	k2, err := crypto.NewRandomKey(16)
	if err != nil {
		t.Fatalf("NewRandomKey(16) failed: %v", err)
	}
	if k1.Len() != 16 {
		t.Errorf("Len() = %d, want 16", k1.Len())
	}
	// Two random keys should differ with extremely high probability
	b1 := k1.Bytes()
	b2 := k2.Bytes()
	same := true
	for i := range b1 {
		if b1[i] != b2[i] {
			same = false
			break
		}
	}
	if same {
		t.Errorf("Two NewRandomKey(16) calls produced identical keys")
	}

	// Negative test: zero-length key
	k3, err := crypto.NewRandomKey(0)
	if err != nil {
		t.Fatalf("NewRandomKey(0) failed: %v", err)
	}
	if k3.Len() != 0 {
		t.Errorf("NewRandomKey(0) len = %d, want 0", k3.Len())
	}
}

func TestHex(t *testing.T) {
	kb := crypto.NewKeyBytes([]byte{0xde, 0xad, 0xbe, 0xef})
	want := "deadbeef"
	if got := kb.Hex(); got != want {
		t.Errorf("Hex() = %s, want %s", got, want)
	}
}

func TestZeroOnNil(t *testing.T) {
	var kb crypto.KeyBytes
	kb.Zero() // should not panic
	if kb.Len() != 0 {
		t.Errorf("zero-value KeyBytes Len() = %d, want 0", kb.Len())
	}
}
