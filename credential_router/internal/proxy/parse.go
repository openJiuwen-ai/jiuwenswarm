package proxy

import (
	"encoding/base64"
	"errors"
	"fmt"
	"net/url"
	"strings"
)

var (
	ErrMissingProxyPrefix = errors.New("path missing proxy prefix")
	ErrMissingAPIBase     = errors.New("missing api base")
	ErrBase64Decode       = errors.New("base64 decode failed")
	ErrEmptyAPIBase       = errors.New("empty api base")
	ErrInvalidAPIBase     = errors.New("invalid api base")
	ErrInvalidScheme      = errors.New("api base scheme must be http or https")
)

const proxyPathPrefix = "/proxy"

// ParseProxyPath splits a proxy request path into its upstream base and the
// remainder. One shape is supported:
//
//	A: /proxy/{api_base_b64}/{rest} — proxy_key comes from a header
//
// The first segment is always the api_base (form A); the proxy_key must be
// supplied via Authorization / X-Api-Key / X-Goog-Api-Key.
//
// api_base is decoded (unpadded base64url), normalized and validated.
// originalPath is the unmodified remainder after the api_base segment
// (leading "/" preserved, may be empty).
func ParseProxyPath(path string) (apiBase, originalPath string, err error) {
	if !strings.HasPrefix(path, proxyPathPrefix+"/") {
		return "", "", ErrMissingProxyPrefix
	}
	rest := strings.TrimPrefix(path, proxyPathPrefix+"/")
	if rest == "" {
		return "", "", ErrMissingAPIBase
	}

	encodedBase, remaining := splitFirstSegment(rest)
	if encodedBase == "" {
		return "", "", ErrMissingAPIBase
	}

	decoded, derr := decodeBase64URL(encodedBase)
	if derr != nil {
		return "", "", fmt.Errorf("%w: %v", ErrBase64Decode, derr)
	}

	apiBase = normalizeURLBase(decoded)
	if apiBase == "" {
		return "", "", ErrEmptyAPIBase
	}
	if verr := validateURLBase(apiBase); verr != nil {
		return "", "", verr
	}

	return apiBase, remaining, nil
}

// splitFirstSegment returns the first path segment of rest and everything
// after it. The tail keeps its leading "/" so callers can reassemble the
// original sub-path without ambiguity.
func splitFirstSegment(rest string) (seg, tail string) {
	if i := strings.Index(rest, "/"); i >= 0 {
		return rest[:i], rest[i:]
	}
	return rest, ""
}

func BuildFullURL(apiBase, originalPath string) (string, error) {
	base := normalizeURLBase(apiBase)
	if err := validateURLBase(base); err != nil {
		return "", err
	}
	if originalPath == "" || originalPath == "/" {
		return base, nil
	}
	if !strings.HasPrefix(originalPath, "/") {
		return "", fmt.Errorf("%w: invalid original path", ErrInvalidAPIBase)
	}
	full := base + originalPath
	if _, err := url.Parse(full); err != nil {
		return "", fmt.Errorf("%w: %v", ErrInvalidAPIBase, err)
	}
	return full, nil
}

func validateURLBase(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return fmt.Errorf("%w: %v", ErrInvalidAPIBase, err)
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme != "http" && scheme != "https" {
		return ErrInvalidScheme
	}
	if u.Host == "" {
		return fmt.Errorf("%w: missing host", ErrInvalidAPIBase)
	}
	return nil
}

func normalizeURLBase(raw string) string {
	return NormalizeAPIBase(raw)
}

func decodeBase64URL(encoded string) (string, error) {
	padded := encoded
	switch len(encoded) % 4 {
	case 2:
		padded += "=="
	case 3:
		padded += "="
	}
	data, err := base64.URLEncoding.DecodeString(padded)
	if err != nil {
		data, err = base64.RawURLEncoding.DecodeString(encoded)
		if err != nil {
			return "", err
		}
	}
	return string(data), nil
}

// EncodeBase64URL encodes raw bytes as unpadded base64url. Admin handlers use
// it to build proxy_address URLs from an api_base.
func EncodeBase64URL(raw string) string {
	return base64.RawURLEncoding.EncodeToString([]byte(raw))
}
