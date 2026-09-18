package cache_test

import (
	"bytes"
	"context"
	"errors"
	"log"
	"strings"
	"testing"

	"credential_router/internal/credmgr/cache"
)

// TestWriteThroughFailureLogRedactsKey verifies that when WriteThrough
// exhausts its 3 retries the resulting log line + wrapped error message
// do NOT contain the full proxy_key. Regression guard for the leak
// where cache.WriteThrough embedded `key=%s` directly, and where the
// returned error (later logged via slog.Error in admin/server.go) did
// the same.
func TestWriteThroughFailureLogRedactsKey(t *testing.T) {
	fullKey := "cr_pk_" + strings.Repeat("a", 43) // 49 chars: production format

	var buf bytes.Buffer
	prevOut := log.Default().Writer()
	prevFlags := log.Default().Flags()
	log.Default().SetOutput(&buf)
	log.Default().SetFlags(0)
	t.Cleanup(func() {
		log.Default().SetOutput(prevOut)
		log.Default().SetFlags(prevFlags)
	})

	c := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 100, TombstoneTTL: 0})
	err := c.WriteThrough(context.Background(), fullKey, func() error { return errors.New("persistent") })
	if err == nil {
		t.Fatal("WriteThrough should return error after all attempts fail")
	}

	out := buf.String()
	if !strings.Contains(out, "cr_pk_aa") {
		t.Errorf("log output should contain redacted prefix 'cr_pk_aa', got: %q", out)
	}
	if strings.Contains(out, fullKey) {
		t.Errorf("log output leaked full proxy_key: %q", out)
	}

	if !strings.Contains(err.Error(), "cr_pk_aa") {
		t.Errorf("returned error should contain redacted prefix 'cr_pk_aa', got: %q", err.Error())
	}
	if strings.Contains(err.Error(), fullKey) {
		t.Errorf("returned error leaked full proxy_key: %q", err.Error())
	}
}
