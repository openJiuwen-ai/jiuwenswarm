package proxy_test

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"credential_router/internal/proxy/ssrf"
)

type countingResolver struct {
	mu    sync.Mutex
	calls map[string]int
	ips   map[string][]net.IP
	err   map[string]error
}

func newCountingResolver() *countingResolver {
	return &countingResolver{calls: map[string]int{}, ips: map[string][]net.IP{}, err: map[string]error{}}
}

func (f *countingResolver) LookupIP(_ context.Context, host string) ([]net.IP, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls[host]++
	if e, ok := f.err[host]; ok {
		return nil, e
	}
	if ips, ok := f.ips[host]; ok {
		return ips, nil
	}
	return nil, fmt.Errorf("countingResolver: no entry for %s", host)
}

func (f *countingResolver) setIPs(host string, ips ...net.IP) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.ips[host] = ips
}

func (f *countingResolver) callCount(host string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.calls[host]
}

func TestGuard_NilPolicy_PassesThrough(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	g := ssrf.NewSSRFGuardDialer(nil, 30*time.Second, time.Second)
	g.SetResolver(newCountingResolver())
	conn, err := g.DialContext(context.Background(), "tcp", srv.Listener.Addr().String())
	if err != nil {
		t.Fatalf("nil policy should pass through, got %v", err)
	}
	_ = conn.Close()
}

func TestGuard_BlocksRFC1918_LiteralIP(t *testing.T) {
	g := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(), 30*time.Second, time.Second)
	g.SetResolver(newCountingResolver())

	_, err := g.DialContext(context.Background(), "tcp", "127.0.0.1:80")
	if !errors.Is(err, ssrf.ErrSSRFBlocked) {
		t.Fatalf("expected ErrSSRFBlocked, got %v", err)
	}
}

func TestGuard_BlocksAllBlockedCIDRs(t *testing.T) {
	cases := []string{
		"10.0.0.1:80",
		"172.16.0.1:80",
		"192.168.1.1:80",
		"169.254.169.254:80",
		"0.0.0.0:80",
		"[::1]:80",
	}
	for _, addr := range cases {
		t.Run(addr, func(t *testing.T) {
			g := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(), 30*time.Second, time.Second)
			g.SetResolver(newCountingResolver())
			_, err := g.DialContext(context.Background(), "tcp", addr)
			if !errors.Is(err, ssrf.ErrSSRFBlocked) {
				t.Fatalf("expected ErrSSRFBlocked for %s, got %v", addr, err)
			}
		})
	}
}

func TestGuard_BlocksHostnameResolvingToPrivate(t *testing.T) {
	res := newCountingResolver()
	res.setIPs("evil.example", net.ParseIP("10.0.0.5"))

	g := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(), 30*time.Second, time.Second)
	g.SetResolver(res)

	_, err := g.DialContext(context.Background(), "tcp", "evil.example:443")
	if !errors.Is(err, ssrf.ErrSSRFBlocked) {
		t.Fatalf("expected ErrSSRFBlocked, got %v", err)
	}
}

func TestGuard_BlocksHostnameOnBlocklist(t *testing.T) {
	res := newCountingResolver()
	res.setIPs("localhost", net.ParseIP("127.0.0.1"))

	g := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(), 30*time.Second, time.Second)
	g.SetResolver(res)

	_, err := g.DialContext(context.Background(), "tcp", "localhost:80")
	if !errors.Is(err, ssrf.ErrSSRFBlocked) {
		t.Fatalf("expected ErrSSRFBlocked, got %v", err)
	}
}

func TestGuard_AllowedHostsWhitelist(t *testing.T) {
	res := newCountingResolver()
	res.setIPs("api.example.com", net.ParseIP("8.8.8.8"))
	res.setIPs("other.example.com", net.ParseIP("8.8.4.4"))

	policy := ssrf.DefaultPolicy()
	policy.AllowedHosts = []string{"api.example.com"}
	g := ssrf.NewSSRFGuardDialer(policy, 30*time.Second, time.Second)
	g.SetResolver(res)

	if _, err := g.ResolveAndCheck(context.Background(), "other.example.com"); !errors.Is(err, ssrf.ErrSSRFBlocked) {
		t.Fatalf("expected non-allowed host blocked, got %v", err)
	}
	if ips, err := g.ResolveAndCheck(context.Background(), "api.example.com"); err != nil || len(ips) == 0 {
		t.Fatalf("expected allowed host to resolve, got ips=%v err=%v", ips, err)
	}
}

func TestGuard_CacheHit(t *testing.T) {
	res := newCountingResolver()
	res.setIPs("cached.example", net.ParseIP("8.8.8.8"))

	g := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(), time.Minute, time.Second)
	g.SetResolver(res)

	if _, err := g.ResolveAndCheck(context.Background(), "cached.example"); err != nil {
		t.Fatalf("first resolve: %v", err)
	}
	if _, err := g.ResolveAndCheck(context.Background(), "cached.example"); err != nil {
		t.Fatalf("second resolve: %v", err)
	}
	if got := res.callCount("cached.example"); got != 1 {
		t.Errorf("resolver call count = %d, want 1 (second should be cached)", got)
	}
}

func TestGuard_CacheExpires(t *testing.T) {
	res := newCountingResolver()
	res.setIPs("expiring.example", net.ParseIP("8.8.8.8"))

	g := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(), 30*time.Millisecond, time.Second)
	g.SetResolver(res)

	if _, err := g.ResolveAndCheck(context.Background(), "expiring.example"); err != nil {
		t.Fatalf("first resolve: %v", err)
	}
	time.Sleep(60 * time.Millisecond)
	if _, err := g.ResolveAndCheck(context.Background(), "expiring.example"); err != nil {
		t.Fatalf("second resolve after expiry: %v", err)
	}
	if got := res.callCount("expiring.example"); got != 2 {
		t.Errorf("resolver call count = %d, want 2 (cache must have expired)", got)
	}
}

func TestGuard_CacheDisabled(t *testing.T) {
	res := newCountingResolver()
	res.setIPs("no-cache.example", net.ParseIP("8.8.8.8"))

	g := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(), 0, time.Second)
	g.SetResolver(res)

	if _, err := g.ResolveAndCheck(context.Background(), "no-cache.example"); err != nil {
		t.Fatalf("first: %v", err)
	}
	if _, err := g.ResolveAndCheck(context.Background(), "no-cache.example"); err != nil {
		t.Fatalf("second: %v", err)
	}
	if got := res.callCount("no-cache.example"); got != 2 {
		t.Errorf("resolver call count = %d, want 2 (cacheTTL=0 disables cache)", got)
	}
}

func TestGuard_ConcurrentDialSafety(t *testing.T) {
	res := newCountingResolver()
	res.setIPs("contended.example", net.ParseIP("8.8.8.8"))

	g := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(), time.Minute, time.Second)
	g.SetResolver(res)

	const goroutines = 32
	var wg sync.WaitGroup
	errs := make(chan error, goroutines)
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, err := g.ResolveAndCheck(context.Background(), "contended.example")
			errs <- err
		}()
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Errorf("concurrent resolve: %v", err)
		}
	}
}

func TestGuard_IntegrationHandler(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	t.Run("default-off passes through", func(t *testing.T) {
		g := ssrf.NewSSRFGuardDialer(nil, 30*time.Second, time.Second)
		g.SetResolver(newCountingResolver())
		conn, err := g.DialContext(context.Background(), "tcp", srv.Listener.Addr().String())
		if err != nil {
			t.Fatalf("default-off should pass through: %v", err)
		}
		_ = conn.Close()
	})

	t.Run("default-on blocks loopback httptest", func(t *testing.T) {
		g := ssrf.NewSSRFGuardDialer(ssrf.DefaultPolicy(), 30*time.Second, time.Second)
		g.SetResolver(newCountingResolver())
		_, err := g.DialContext(context.Background(), "tcp", srv.Listener.Addr().String())
		if !errors.Is(err, ssrf.ErrSSRFBlocked) {
			t.Fatalf("httptest is loopback, default policy must block: %v", err)
		}
	})
}
