// Fuzz tests for internal/proxy.
//
// Quick:  go test -run=^$ -fuzz=^Fuzz -fuzztime=10s ./tests/unit/proxy/...
// Full:   go test -run=^$ -fuzz=^Fuzz -fuzztime=60s ./tests/unit/proxy/...

package proxy_test

import (
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"credential_router/internal/credmgr/cache"
	"credential_router/internal/platform"
	"credential_router/internal/proxy"
)

const (
	// fuzzSafePathChars are URL-safe path characters (no '%' — an invalid
	// escape would make the raw request unparseable, which is a harness
	// limitation, not production behavior).
	fuzzSafePathChars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~:/?&=@+,;$'()*!#[]"
)

func fuzzCharPick(data []byte, salt byte, i int, chars string) byte {
	if len(data) == 0 {
		return chars[i%len(chars)]
	}
	return chars[int(data[(int(salt)+i)%len(data)])%len(chars)]
}

// fuzzPathTail builds a URL path suffix from data.
func fuzzPathTail(data []byte, salt byte) string {
	n := 8
	if len(data) > 0 {
		n = int(data[int(salt)%len(data)]) % 24
	}
	if n == 0 {
		return ""
	}
	out := make([]byte, n)
	for i := range out {
		out[i] = fuzzCharPick(data, salt, i*2, fuzzSafePathChars)
	}
	return "/" + string(out)
}

func fuzzMethod(data []byte, salt byte) string {
	methods := []string{"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
	if len(data) == 0 {
		return "GET"
	}
	return methods[int(data[int(salt)%len(data)])%len(methods)]
}

func fuzzRemoteAddr(data []byte, salt byte) string {
	if len(data) == 0 {
		return "127.0.0.1:12345"
	}
	switch int(data[int(salt)%len(data)]) % 4 {
	case 0:
		return "127.0.0.1:12345"
	case 1:
		return "[::1]:8080"
	case 2:
		return "invalid"
	default:
		return fmt.Sprintf("10.0.%d.%d:%d",
			int(data[(int(salt)+1)%len(data)])%256,
			int(data[(int(salt)+2)%len(data)])%256,
			1024+int(data[(int(salt)+3)%len(data)])%64000)
	}
}

// fuzzRawPath mangles arbitrary bytes into a parseable (but adversarial)
// request path, keeping the raw length and entropy while avoiding harness
// panics from control characters / spaces / invalid percent escapes.
func fuzzRawPath(data []byte) string {
	if len(data) == 0 {
		return "/"
	}
	out := make([]byte, 0, len(data))
	for _, b := range data {
		c := b
		// Control chars, space, DEL, and '%' (a lone/invalid percent escape
		// would make the raw request unparseable) are replaced.
		if c < 0x20 || c == 0x7f || c == ' ' || c == '%' {
			c = '-'
		}
		out = append(out, c)
	}
	s := string(out)
	if !strings.HasPrefix(s, "/") {
		s = "/" + s
	}
	return s
}

// fuzzProxyRequest builds a request with a fuzzed method, RemoteAddr, and a
// large random header (large-header coverage). Auth is header-based via the
// fixed testProxyKey so every request resolves through the credential lookup.
func fuzzProxyRequest(h *proxyFuzzHarness, method, path string, data []byte, salt byte) *http.Request {
	req := httptest.NewRequest(method, path, nil)
	req.RemoteAddr = fuzzRemoteAddr(data, salt)
	req.Header.Set("Authorization", "Bearer "+testProxyKey)
	if len(data) > 0 {
		n := 1 + int(data[int(salt)%len(data)])%4096
		req.Header.Set("X-Fuzz-Big", strings.Repeat("x", n))
	}
	return req
}

type proxyFuzzHarness struct {
	handler  *proxy.Handler
	upstream *httptest.Server
	mv       *mockVault
}

func newProxyFuzzUpstream(t testing.TB) *httptest.Server {
	t.Helper()
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "pong")
	}))
	t.Cleanup(up.Close)
	return up
}

func newProxyFuzzHarness(t testing.TB, getter cache.Getter) *proxyFuzzHarness {
	t.Helper()
	up := newProxyFuzzUpstream(t)
	cfg := platform.Default()
	cfg.UpstreamTimeoutMs = 1000
	cfg.Server.MaxResponseBytes = 1 << 20
	mv := &mockVault{}
	var h *proxy.Handler
	var err error
	if getter != nil {
		h, err = proxy.NewHandler(cfg, getter)
	} else {
		h, err = proxy.NewHandler(cfg, mv)
	}
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	return &proxyFuzzHarness{handler: h, upstream: up, mv: mv}
}

// FuzzProxyServeHTTP — full forward path: path parsing, URL rewriting, auth
// injection, and a real loopback upstream round-trip. Every structured input
// must resolve to a 200 with the upstream body (never a crash, never a
// deadlock), proving malformed-tolerant handling end to end.
func FuzzProxyServeHTTP(f *testing.F) {
	h := newProxyFuzzHarness(f, nil)
	enc := encodeURLBase64Safe(h.upstream.URL)
	f.Add([]byte("/proxy/" + enc + "/chat/completions"))
	f.Add([]byte("/proxy/" + enc + "/"))
	f.Add([]byte("/proxy/" + enc + "/x?y=z"))
	f.Add([]byte("/proxy/" + enc))
	f.Add([]byte(""))
	f.Add([]byte("/proxy/" + enc + "/" + strings.Repeat("tail-", 20)))
	f.Add([]byte("/proxy/" + enc + "/big"))
	f.Add([]byte("/proxy/" + enc + "/中文"))
	f.Fuzz(func(t *testing.T, data []byte) {
		method := fuzzMethod(data, 0)
		tail := fuzzPathTail(data, 2)
		path := "/proxy/" + encodeURLBase64Safe(h.upstream.URL) + tail

		req := fuzzProxyRequest(h, method, path, data, 3)
		rr := httptest.NewRecorder()
		h.handler.ServeHTTP(rr, req)

		if rr.Code != http.StatusOK {
			t.Fatalf("method=%s path=%q status=%d body=%q",
				method, path, rr.Code, rr.Body.String())
		}
		if method == "GET" && rr.Body.String() != "pong" {
			t.Fatalf("path=%q body=%q, want %q", path, rr.Body.String(), "pong")
		}
	})
}

// FuzzProxyRawPath — malformed/random request robustness. Arbitrary bytes as
// a request path (with random method / RemoteAddr / big headers) must never
// panic or deadlock; every response is a valid status with a body.
func FuzzProxyRawPath(f *testing.F) {
	h := newProxyFuzzHarness(f, nil)
	f.Add([]byte("/no-prefix"))
	f.Add([]byte("/proxy/default/notbase64!!/x"))
	f.Add([]byte("/proxy/PROD/!!!!/x"))
	f.Add([]byte("//"))
	f.Add([]byte(""))
	f.Add([]byte("/proxy"))
	f.Add([]byte("/health"))
	f.Add([]byte("\x00\x01\x02"))
	f.Add([]byte(strings.Repeat("/proxy/a/", 30)))
	f.Add([]byte("GET /\r\n\r\n"))
	f.Fuzz(func(t *testing.T, data []byte) {
		path := fuzzRawPath(data)
		method := fuzzMethod(data, 0)
		req := fuzzProxyRequest(h, method, path, data, 1)
		rr := httptest.NewRecorder()
		h.handler.ServeHTTP(rr, req)

		if rr.Code == 0 || rr.Code >= 600 {
			t.Fatalf("path=%q status=%d", path, rr.Code)
		}
		if rr.Body.Len() == 0 && method != "HEAD" {
			t.Fatalf("path=%q status=%d empty body", path, rr.Code)
		}
	})
}

// FuzzProxyCachePath — credential resolution through the cache. The cache is
// warmed for the (user, url, keyTag) tuple, then the request must resolve via
// a pure cache hit: 200, no deadlock, and the underlying getter never called.
func FuzzProxyCachePath(f *testing.F) {
	up := newProxyFuzzUpstream(f)

	cc := cache.NewInMemoryCredentialCache(cache.Config{MaxEntries: 1000, TombstoneTTL: time.Hour})
	mv := &mockVault{}
	cg := cache.NewCachedCredentialGetter(cc, mv)

	cfg := platform.Default()
	cfg.UpstreamTimeoutMs = 1000
	cfg.Server.MaxResponseBytes = 1 << 20
	h, err := proxy.NewHandler(cfg, cg)
	if err != nil {
		f.Fatalf("NewHandler: %v", err)
	}

	enc := encodeURLBase64Safe(up.URL)
	f.Add([]byte("/proxy/" + enc + "/chat"))
	f.Add([]byte("/proxy/" + enc + "/"))
	f.Add([]byte("/proxy/" + enc + "/x?q=1"))
	f.Add([]byte(""))
	f.Add([]byte("/proxy/" + enc + "/long-tail-path"))
	f.Fuzz(func(t *testing.T, data []byte) {
		// Warm the cache so this request resolves on the cache-hit path.
		if _, _, werr := cg.GetCredentialByProxyKey(testProxyKey); werr != nil {
			t.Fatalf("warm cache: %v", werr)
		}
		mv.calls.Store(0)

		method := fuzzMethod(data, 0)
		tail := fuzzPathTail(data, 2)
		path := "/proxy/" + encodeURLBase64Safe(up.URL) + tail
		req := fuzzProxyRequest(&proxyFuzzHarness{handler: h, upstream: up, mv: mv}, method, path, data, 3)
		rr := httptest.NewRecorder()
		h.ServeHTTP(rr, req)

		if rr.Code != http.StatusOK {
			t.Fatalf("cache-hit path: status=%d body=%q", rr.Code, rr.Body.String())
		}
		if got := mv.calls.Load(); got != 0 {
			t.Fatalf("cache-hit path called underlying getter %d times (cache miss fallthrough)", got)
		}
	})
}

// FuzzProxyConcurrent — concurrent request safety. Four goroutines hammer the
// same handler with distinct derived paths per input; any panic, non-200
// status, or deadlock is a failure. Combined with `-race`, this also detects
// data races across concurrent credential resolution and forwarding.
func FuzzProxyConcurrent(f *testing.F) {
	h := newProxyFuzzHarness(f, nil)
	enc := encodeURLBase64Safe(h.upstream.URL)
	f.Add([]byte("/proxy/" + enc + "/chat"))
	f.Add([]byte("/proxy/" + enc + "/x"))
	f.Add([]byte(""))
	f.Add([]byte("/proxy/" + enc + "/path?q=1"))
	f.Add([]byte(strings.Repeat("x", 256)))
	f.Fuzz(func(t *testing.T, data []byte) {
		const workers = 4
		var wg sync.WaitGroup
		errCh := make(chan error, workers)
		for i := 0; i < workers; i++ {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				defer func() {
					if r := recover(); r != nil {
						errCh <- fmt.Errorf("panic in worker %d: %v", i, r)
					}
				}()
				method := fuzzMethod(data, byte(i))
				tail := fuzzPathTail(data, byte(i*5+2))
				path := "/proxy/" + encodeURLBase64Safe(h.upstream.URL) + tail
				req := fuzzProxyRequest(h, method, path, data, byte(i*7+3))
				rr := httptest.NewRecorder()
				h.handler.ServeHTTP(rr, req)
				if rr.Code != http.StatusOK {
					errCh <- fmt.Errorf("worker %d: status=%d path=%q", i, rr.Code, path)
				}
			}(i)
		}
		wg.Wait()
		close(errCh)
		for e := range errCh {
			t.Fatalf("concurrent request failure: %v", e)
		}
	})
}
