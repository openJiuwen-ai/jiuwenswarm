package ssrf

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"sync"
	"time"

)

// ErrSSRFBlocked is wrapped by the guard dialer's returned error when a
// dial target resolves to (or is literal) a blocked IP. Callers can use
// errors.Is to distinguish SSRF rejections from network failures.
var ErrSSRFBlocked = errors.New("proxy: ssrf guard blocked dial")

// SSRFGuardDialer wraps a net.Dialer with a per-host DNS resolution cache
// and an IP allow/block check. It is installed
// on http.Transport.DialContext when platform.SSRFConfig.DialCheck is true.
//
// The dialer resolves the hostname fresh on every cache miss, checks each
// address against URLPolicy.IsBlockedIP, and refuses to connect
// when any address is in a blocked range. Resolved addresses are cached
// for platform.SSRFConfig.CacheTTL; set to 0 to disable caching.
//
// Cache design notes:
//   - Cache only stores *permitted* resolutions. A blocked dial is never
//     cached, so a temporary bad record never poisons subsequent calls.
//   - Concurrent dials to the same host race benignly: both resolve, both
//     check the policy, both store. The last write wins; the value is the
//     same set of IPs either way.
//   - Cache TTL creates a bounded TOCTOU window. Operators who
//     need zero window should set SSRFConfig.CacheTTL to 0.
type SSRFGuardDialer struct {
	inner    *net.Dialer
	policy   *URLPolicy
	cacheTTL time.Duration

	// resolver is used for DNS lookups. nil → net.DefaultResolver.
	// Tests inject a custom resolver to avoid depending on real DNS.
	resolver Resolver

	cacheMu sync.Mutex
	cache   map[string]cachedResolve
}

type cachedResolve struct {
	ips    []net.IP
	expiry time.Time
}

type Resolver interface {
	LookupIP(ctx context.Context, host string) ([]net.IP, error)
}

type defaultResolver struct{}

func (defaultResolver) LookupIP(ctx context.Context, host string) ([]net.IP, error) {
	return net.DefaultResolver.LookupIP(ctx, "ip", host)
}

func NewSSRFGuardDialer(policy *URLPolicy, cacheTTL, dialTimeout time.Duration) *SSRFGuardDialer {
	return &SSRFGuardDialer{
		inner:    &net.Dialer{Timeout: dialTimeout},
		policy:   policy,
		cacheTTL: cacheTTL,
		resolver: defaultResolver{},
		cache:    make(map[string]cachedResolve),
	}
}

// SetResolver overrides the DNS resolver used by DialContext. Intended for
// test injection; calling after dials have started is a data race on the
// resolver field.
func (d *SSRFGuardDialer) SetResolver(r Resolver) {
	d.resolver = r
}

// DialContext implements net.Dialer-compatible dial. The policy, when
// non-nil, gates every dial. A nil policy is a no-op pass-through (the
// default config has SSRFConfig.DialCheck=false, so this code path is not
// installed at all in that case).
func (d *SSRFGuardDialer) DialContext(ctx context.Context, network, addr string) (net.Conn, error) {
	if d.policy == nil {
		return d.inner.DialContext(ctx, network, addr)
	}
	host, port, err := net.SplitHostPort(addr)
	if err != nil {
		return nil, fmt.Errorf("ssrf: parse addr %q: %w", addr, err)
	}
	ips, err := d.ResolveAndCheck(ctx, host)
	if err != nil {
		return nil, err
	}
	var firstErr error
	for _, ip := range ips {
		c, derr := d.inner.DialContext(ctx, network, net.JoinHostPort(ip.String(), port))
		if derr == nil {
			return c, nil
		}
		firstErr = derr
	}
	return nil, fmt.Errorf("ssrf: dial %s: %w", addr, firstErr)
}

func (d *SSRFGuardDialer) ResolveAndCheck(ctx context.Context, host string) ([]net.IP, error) {
	if d.cacheTTL > 0 {
		d.cacheMu.Lock()
		if c, ok := d.cache[host]; ok && time.Now().Before(c.expiry) {
			d.cacheMu.Unlock()
			return c.ips, nil
		}
		d.cacheMu.Unlock()
	}

	ips, err := d.check(ctx, host)
	if err != nil {
		return nil, err
	}

	if d.cacheTTL > 0 {
		d.cacheMu.Lock()
		d.cache[host] = cachedResolve{ips: ips, expiry: time.Now().Add(d.cacheTTL)}
		d.cacheMu.Unlock()
	}
	return ips, nil
}

// check performs the policy gate. Returns the IPs the dialer should
// attempt, or an error. On literal IPs no DNS is performed.
func (d *SSRFGuardDialer) check(ctx context.Context, host string) ([]net.IP, error) {
	if ip := net.ParseIP(host); ip != nil {
		if d.policy.IsBlockedIP(ip) {
			return nil, fmt.Errorf("%w: %s is in a blocked range", ErrSSRFBlocked, ip.String())
		}
		return []net.IP{ip}, nil
	}

	lower := strings.ToLower(host)
	for _, h := range d.policy.BlockedHosts {
		if lower == strings.ToLower(h) {
			return nil, fmt.Errorf("%w: host %q is on the blocklist", ErrSSRFBlocked, host)
		}
	}

	if len(d.policy.AllowedHosts) > 0 {
		if !containsFold(d.policy.AllowedHosts, host) {
			return nil, fmt.Errorf("%w: host %q is not in the allowed list", ErrSSRFBlocked, host)
		}
	}

	ips, err := d.resolver.LookupIP(ctx, host)
	if err != nil {
		return nil, fmt.Errorf("ssrf: resolve %s: %w", host, err)
	}
	if len(ips) == 0 {
		return nil, fmt.Errorf("%w: host %q did not resolve to any address", ErrSSRFBlocked, host)
	}
	for _, ip := range ips {
		if d.policy.IsBlockedIP(ip) {
			return nil, fmt.Errorf("%w: %s resolves to blocked address %s", ErrSSRFBlocked, host, ip.String())
		}
	}
	return ips, nil
}

func containsFold(list []string, s string) bool {
	for _, x := range list {
		if strings.EqualFold(x, s) {
			return true
		}
	}
	return false
}
