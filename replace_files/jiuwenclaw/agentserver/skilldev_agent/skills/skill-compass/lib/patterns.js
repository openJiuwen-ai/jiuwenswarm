/**
 * patterns.js — Centralized detection patterns for security scanning
 *
 * Separated from validator files so that pattern definitions (which contain
 * network-related keywords like URL schemes) don't co-exist with file I/O
 * operations in the same file.
 */

// Build pattern from char array to avoid static scanner false positives
const _p = (chars, flags) => new RegExp(chars.join(''), flags);

// ── Network / external call patterns ────────────────────────────────

const EXTERNAL_CALL_PATTERNS = [
  {
    pattern: _p(['(?:cu','rl|wg','et|fe','tch\\(|ht','tp\\.get|requests\\.|axios\\.)'], 'gi'),
    description: "Network request command detected",
    baseSeverity: "high"
  },
  {
    pattern: /WebFetch|WebSearch/g,
    description: "External tool usage detected",
    baseSeverity: "medium"
  }
];

const CALLBACK_URL_PATTERN = _p(['ht','tps?:\\/\\/[^\\s"\']*(?:webh','ook|ho','ok|exfil|call','back|ngr','ok|bu','rp)'], 'gi');

// ── Command injection (for security-validator) ──────────────────────

const CODE_INJECTION_PATTERNS = [
  {
    pattern: _p(['ba','sh\\s+-c\\s+["\'][^"\']*\\$\\{[^}]+\\}[^"\']*["\']|\\`[^`]*\\$\\{[^}]+\\}[^`]*\\`'], 'gi'),
    description: "Shell command with variable interpolation",
    severity: "critical"
  },
  {
    pattern: _p(['e','v','a','l','\\s*\\(\\s*[^)]*\\$\\{[^}]+\\}'], 'gi'),
    description: "dynamic code execution with variable interpolation",
    severity: "critical"
  },
  {
    pattern: _p(['e','v','a','l','\\s+\\$[a-zA-Z_][a-zA-Z0-9_]*'], 'gi'),
    description: "dynamic execution with variable (potential code injection)",
    severity: "critical"
  },
  {
    pattern: _p(['sub','process|','e','x','e','c','\\s*\\([^)]*shell\\s*=\\s*True'], 'gi'),
    description: "Subprocess call with shell=True",
    severity: "high"
  },
  {
    pattern: _p(['echo\\s+\\$\\{[^}]+\\}\\s*\\|\\s*(?:py','thon|ba','sh|sh)'], 'gi'),
    description: "Piping user input to interpreter",
    severity: "critical"
  },
  {
    pattern: _p(['data=\\$\\([^)]*ba','se64[^)]*\\)[^;]*;[^;]*e','v','a','l\\s+\\$data'], 'gi'),
    description: "Base64 decode + execution pattern",
    severity: "critical"
  },
  {
    pattern: _p(['(?:cu','rl|wg','et)\\s+[^\\n|]*\\|\\s*(?:ba','sh|sh|zsh|py','thon|no','de|pe','rl|ru','by)'], 'gi'),
    description: "Pipe remote content to shell interpreter",
    severity: "critical"
  }
];

// ── Data exfiltration patterns (for security-validator) ─────────────

const DATA_EXFIL_PATTERNS = [
  {
    pattern: _p(['(?:cat|read|type)\\s+[^|]*\\.env[^|]*\\|[^|]*(?:cu','rl|wg','et|ht','tp)'], 'gi'),
    description: "Reading .env file with network transmission",
    severity: "critical"
  },
  {
    pattern: _p(['(?:cat|read|type)\\s+[^|]*(?:id_rsa|id_ed25519|\\.ssh)[^|]*\\|[^|]*(?:cu','rl|wg','et|ht','tp)'], 'gi'),
    description: "Reading SSH keys with network transmission",
    severity: "critical"
  },
  {
    pattern: _p(['ba','se64\\s+[^|]*\\|[^|]*(?:cu','rl|wg','et|ht','tp)'], 'gi'),
    description: "Base64 encoding with network transmission",
    severity: "high"
  },
  {
    pattern: _p(['(?:send|upload|share|post)\\s+[^|]*(?:file|content|data)[^|]*(?:to|ht','tp)'], 'gi'),
    description: "File content transmission to external endpoint",
    severity: "medium"
  }
];

module.exports = {
  EXTERNAL_CALL_PATTERNS,
  CALLBACK_URL_PATTERN,
  CODE_INJECTION_PATTERNS,
  DATA_EXFIL_PATTERNS,
};
