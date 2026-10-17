package ssrf

import (
	"fmt"
	"net"
	"strings"

	"credential_router/internal/platform"
)

// URLPolicy controls which real_url hosts may be registered at the admin
// gate. It defends against SSRF: even though the proxy
// runs behind operator-controlled egress, a credential whose real_url points
// at an internal service can be exploited via subsequent proxy requests.
//
// Resolution order in CheckHost:
//
//  1. Literal IP fast path — if host parses as IP, check it against
//     BlockedNetworks immediately (no DNS dependency).
//  2. Hostname exact-match blocklist — short-circuits common loopback
//     hostnames without a DNS round trip.
//  3. AllowedHosts whitelist — when non-empty, only listed hostnames pass.
//     The caller (admin layer) is expected to populate this from config
//     when running in allow-list mode.
//  4. DNS resolution of the hostname — every resolved address is checked
//     against BlockedNetworks. Configurable via LookupIP for tests.
//
// DNS rebinding caveat: CheckHost runs at admin write time. Between the
// write and a subsequent proxy dial, the hostname may re-resolve to a
// different IP. The proxy dialing path must do its own IP-pinning. CheckHost closes the easy cases (literal internal IPs,
// known-loopback hostnames) and the TOCTOU window shrinks but does not
// fully close with this change alone.
type URLPolicy struct {
	BlockedNetworks []*net.IPNet
	BlockedHosts    []string
	AllowedHosts    []string
	LookupIP        func(host string) ([]net.IP, error)
}

// DefaultPolicy blocks RFC1918 private ranges, loopback, link-local
// (including cloud metadata at 169.254.169.254), IPv6 loopback / ULA /
// link-local, and well-known metadata hostnames. Resolves DNS by default.
func DefaultPolicy() *URLPolicy {
	return &URLPolicy{
		BlockedNetworks: defaultBlockedNetworks(),
		BlockedHosts:    defaultBlockedHosts(),
	}
}

// TestPolicy mirrors DefaultPolicy but disables DNS resolution. Use it in
// unit tests that do not want a network dependency; literal IPs are still
// blocked.
func TestPolicy() *URLPolicy {
	return &URLPolicy{
		BlockedNetworks: defaultBlockedNetworks(),
		BlockedHosts:    defaultBlockedHosts(),
		LookupIP:        func(string) ([]net.IP, error) { return nil, nil },
	}
}

func defaultBlockedNetworks() []*net.IPNet {
	cidrs := []string{
		"0.0.0.0/8",      // current network
		"10.0.0.0/8",     // RFC1918
		"100.64.0.0/10",  // CGN
		"127.0.0.0/8",    // loopback
		"169.254.0.0/16", // link-local + cloud metadata
		"172.16.0.0/12",  // RFC1918
		"192.0.0.0/24",   // IETF protocol assignments
		"192.168.0.0/16", // RFC1918
		"198.18.0.0/15",  // benchmarking
		"224.0.0.0/4",    // multicast
		"240.0.0.0/4",    // reserved
		"::1/128",        // IPv6 loopback
		"fc00::/7",       // IPv6 ULA
		"fe80::/10",      // IPv6 link-local
	}
	out := make([]*net.IPNet, 0, len(cidrs))
	for _, c := range cidrs {
		_, n, err := net.ParseCIDR(c)
		if err != nil {
			panic("validate: bad default CIDR " + c + ": " + err.Error())
		}
		out = append(out, n)
	}
	return out
}

func defaultBlockedHosts() []string {
	return []string{
		"localhost",
		"metadata.google.internal", // GCP metadata
		"metadata",                 // common alias
	}
}

// CheckHost returns nil when host is permitted by p, otherwise an
// *platform.Error with CodeBadRequest describing the rejection. A nil policy
// permits every host.
func (p *URLPolicy) CheckHost(host string) error {
	if host == "" {
		return platform.New(platform.CodeBadRequest, "validate", "real_url: empty host")
	}
	if p == nil {
		return nil
	}

	host = stripHostPort(unbracket(host))

	if ip := net.ParseIP(host); ip != nil {
		if len(p.AllowedHosts) > 0 {
			if !containsFold(p.AllowedHosts, host) {
				return platform.New(platform.CodeBadRequest, "validate",
					fmt.Sprintf("real_url: IP %s is not in the allowed list", host))
			}
			return nil
		}
		if p.IsBlockedIP(ip) {
			return platform.New(platform.CodeBadRequest, "validate",
				fmt.Sprintf("real_url: IP %s is in a blocked range", ip.String()))
		}
		return nil
	}

	lower := strings.ToLower(host)
	for _, h := range p.BlockedHosts {
		if lower == strings.ToLower(h) {
			return platform.New(platform.CodeBadRequest, "validate",
				fmt.Sprintf("real_url: host %q is on the blocklist", host))
		}
	}

	if len(p.AllowedHosts) > 0 {
		if !containsFold(p.AllowedHosts, host) {
			return platform.New(platform.CodeBadRequest, "validate",
				fmt.Sprintf("real_url: host %q is not in the allowed list", host))
		}
		return nil
	}

	lookup := p.LookupIP
	if lookup == nil {
		lookup = net.LookupIP
	}
	ips, err := lookup(host)
	if err != nil {
		return platform.New(platform.CodeBadRequest, "validate",
			fmt.Sprintf("real_url: cannot resolve host %q: %s", host, err.Error()))
	}
	if len(ips) == 0 {
		if p.LookupIP != nil {
			return nil
		}
		return platform.New(platform.CodeBadRequest, "validate",
			fmt.Sprintf("real_url: host %q did not resolve to any address", host))
	}
	for _, ip := range ips {
		if p.IsBlockedIP(ip) {
			return platform.New(platform.CodeBadRequest, "validate",
				fmt.Sprintf("real_url: host %q resolves to blocked address %s", host, ip.String()))
		}
	}
	return nil
}

// IsBlockedIP reports whether ip falls inside any of p's BlockedNetworks.
// A nil policy always returns false (SSRF guard disabled). Used by the
// proxy dial-time guard to short-circuit a
// connection without performing DNS, and by CheckHost for the literal-IP
// path.
func (p *URLPolicy) IsBlockedIP(ip net.IP) bool {
	if p == nil {
		return false
	}
	for _, n := range p.BlockedNetworks {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

func stripHostPort(host string) string {
	if h, _, err := net.SplitHostPort(host); err == nil {
		return h
	}
	return host
}

func unbracket(host string) string {
	if len(host) >= 2 && host[0] == '[' && host[len(host)-1] == ']' {
		return host[1 : len(host)-1]
	}
	return host
}

