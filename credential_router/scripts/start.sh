#!/usr/bin/env bash
# scripts/start.sh — credential-router install / start / stop
#
# Exit codes:
#   0  success
#   1  generic failure
#   2  invalid args / unknown flag
#   3  missing deps (go toolchain not installed)
#   4  user abort — overwrite prompt declined
#   5  already installed — secrets exist; use --force to overwrite
#   6  install failed — go build or systemd write failed
#   7  start failed — systemctl or binary self-init failed
#   8  stop failed — service would not stop
#
# Design notes (the binary handles all key-material generation via
# keystore.SelfInit; the script only provides directories):
#   * The binary generates S1 (s1.bin.1), crypto_mode, S2, and DEK on
#     first run via bootstrap.go. S2 and DEK never touch disk in
#     plaintext. crypto_mode is read from the config file (crypto_mode:
#     aes|sm4) on first run; the on-disk file is authoritative afterward.
#   * "already installed" runs an interactive overwrite prompt instead of
#     refusing outright; a declined prompt exits 4.
#   * "stop when not running" returns 0 (idempotent) rather than exiting 1.
#   * Default is foreground + pidfile. --systemd is opt-in to unit write +
#     systemctl. Linux-only; have_systemd() gates on PID-1 systemd, so
#     --systemd on a non-systemd host is a no-op and falls through to
#     pidfile. Windows unsupported. See AGENTS.md "部署 / Deployment".
#   * 'install --bin=PATH' skips `go build` and stages the supplied
#     binary into dist/bin/credential-router (no suffix) so subsequent
#     start/stop without --bin still find it. Use --bin/--config (or
#     $BIN/$CONFIG env) when running from a deployed tree where the
#     script is not colocated with the build source.

set -euo pipefail

# ── ROOT (parent of scripts/ dir = repo root) ─────────────────────────────────
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

GOOS_VAL="$(uname -s | tr '[:upper:]' '[:lower:]')"   # linux, darwin, ...
GOARCH_VAL="$(uname -m)"                                # x86_64, aarch64, arm64

# ── defaults ──────────────────────────────────────────────────────────────────
DATA_DIR="${ROOT}/data"
WITH_SYSTEMD=0
FORCE=0
BIN_OVERRIDE=""
CONFIG_OVERRIDE=""
PIDFILE="${ROOT}/run/credential-router.pid"

# ── arg parsing ───────────────────────────────────────────────────────────────
action=""
  for arg in "$@"; do
  case "$arg" in
    --systemd)        WITH_SYSTEMD=1 ;;
    --bin=*)          BIN_OVERRIDE="${arg#--bin=}" ;;
    --config=*)       CONFIG_OVERRIDE="${arg#--config=}" ;;
    --force)          FORCE=1 ;;
    -h|--help)        action="help" ;;
    -*) echo "error: unknown flag: $arg" >&2; exit 2 ;;
    *)
      if [[ -z "$action" || "$action" == "help" ]]; then action="$arg"
      else echo "error: unexpected positional arg: $arg" >&2; exit 2; fi
      ;;
  esac
done

# Env wins over flag so wrappers (e.g. agentos.sh) can override argv.
[[ -n "${BIN:-}"    ]] && BIN_OVERRIDE="$BIN"
[[ -n "${CONFIG:-}" ]] && CONFIG_OVERRIDE="$CONFIG"

# ── helpers ───────────────────────────────────────────────────────────────────

# systemd available on this host (and we are PID-1 systemd host)
have_systemd() {
  command -v systemctl >/dev/null 2>&1 || return 1
  [[ -d /run/systemd/system ]] || return 1
  # Verify systemd is actually PID-1. /run/systemd/system may exist as a
  # leftover directory on hosts where the systemd package is installed but
  # systemd is not the init system (WSL Ubuntu, chroots, containers,
  # distrobox, …); without this check start.sh tries to talk to a
  # systemctl that has no manager and fails with "Access denied as the
  # requested operation requires interactive authentication".
  local init_exe
  init_exe="$(readlink /proc/1/exe 2>/dev/null || true)"
  [[ "$init_exe" == *"/systemd" ]] || return 1
  return 0
}

# locate the credential-router binary
find_binary() {
  if [[ -n "$BIN_OVERRIDE" ]]; then
    local resolved
    if [[ "$BIN_OVERRIDE" != /* ]]; then
      resolved="$(cd -- "$BIN_OVERRIDE" 2>/dev/null && pwd)" || {
        echo "error: --bin=$BIN_OVERRIDE does not exist (cwd=$PWD)" >&2
        return 1
      }
    else
      resolved="$BIN_OVERRIDE"
    fi
    [[ -x "$resolved" ]] || {
      echo "error: --bin=$resolved is not executable (input '$BIN_OVERRIDE', cwd=$PWD)" >&2
      return 1
    }
    echo "$resolved"
    return 0
  fi
  local c
  for c in \
    "${ROOT}/dist/bin/credential-router_${GOOS_VAL}_${GOARCH_VAL}" \
    "${ROOT}/dist/bin/credential-router" \
    "/usr/local/bin/credential-router"; do
    [[ -x "$c" ]] && { echo "$c"; return 0; }
  done
  command -v credential-router 2>/dev/null && return 0
  cat >&2 <<EOF
error: credential-router binary not found. Tried:
  \$BIN / --bin (not set)
  ${ROOT}/dist/bin/credential-router_${GOOS_VAL}_${GOARCH_VAL}
  ${ROOT}/dist/bin/credential-router
  /usr/local/bin/credential-router
  \$(command -v credential-router)
Fix: build with  ./scripts/build.sh ${GOARCH_VAL}
     install with ./scripts/start.sh install
     or set --bin=/path/to/credential-router
EOF
  return 1
}

resolve_config() {
  if [[ -n "$CONFIG_OVERRIDE" ]]; then
    [[ -f "$CONFIG_OVERRIDE" ]] || { echo "error: --config=$CONFIG_OVERRIDE does not exist" >&2; return 1; }
    echo "$CONFIG_OVERRIDE"
    return 0
  fi
  if   [[ -f "${ROOT}/config.yaml" ]];         then echo "${ROOT}/config.yaml"
  elif [[ -f "${ROOT}/config.example.yaml" ]]; then echo "${ROOT}/config.example.yaml"
  else echo "${ROOT}/config.yaml"; fi
}

# Derive data_dir value from config (top-level only; ./data if missing).
derive_data_dir_from_config() {
  local cfg="$1" val=""
  [[ -f "$cfg" ]] || { echo "./data"; return; }
  val=$(grep -E '^data_dir:' "$cfg" 2>/dev/null | head -1) || true
  val="${val#data_dir:}"
  val="${val%$'\r'}"                                       # strip \r (CRLF safety)
  case "$val" in                                            # strip YAML inline comment FIRST
    \#*) val="" ;;                                          # `# x` → empty value
    *) val="${val%% #*}" ;;
  esac
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"
  if [[ ${#val} -ge 2 ]]; then                              # strip matching surrounding quotes
    local first="${val:0:1}" last="${val: -1}"
    if [[ ( "$first" == '"' || "$first" == "'" ) && "$first" == "$last" ]]; then
      val="${val:1:${#val}-2}"
      val="${val#"${val%%[![:space:]]*}"}"
      val="${val%"${val##*[![:space:]]}"}"
    fi
  fi
  echo "${val:-./data}"
}

# Interactive overwrite prompt: [y/N], 5s timeout, default N
prompt_yes_no() {
  local prompt="$1" default="${2:-N}" reply=""
  read -r -t 5 -p "$prompt [y/N]: " reply 2>/dev/null || reply=""
  reply="${reply:-$default}"
  case "${reply,,}" in y|yes) return 0 ;; *) return 1 ;; esac
}

# backup a file to data/backups/pre-overwrite-<ts>-<name> (non-force path only)
backup_file() {
  local src="$1" ts backup
  ts="$(date +%Y%m%d-%H%M%S)"
  mkdir -p "${DATA_DIR}/backups"
  backup="${DATA_DIR}/backups/pre-overwrite-${ts}-$(basename "$src")"
  mv "$src" "$backup"
  echo "backed up $src → $backup"
}

# write systemd unit via heredoc
write_systemd_unit() {
  local bin="$1" config unit
  config="$(resolve_config)"
  unit="/etc/systemd/system/credential-router.service"
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "error: systemctl not found; cannot write systemd unit" >&2
    exit 6
  fi
  if [[ ! -d /etc/systemd/system ]]; then
    echo "error: /etc/systemd/system not found (not a systemd host?)" >&2
    exit 6
  fi
  if ! touch "$unit" 2>/dev/null; then
    echo "error: cannot write $unit (need root?)" >&2
    exit 6
  fi
  if ! getent group credential-router >/dev/null 2>&1; then
    groupadd --system credential-router
  fi
  if ! id -u credential-router >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin -g credential-router credential-router
  fi
  chown -R credential-router:credential-router "$DATA_DIR"
  cat > "$unit" <<EOF
[Unit]
Description=Credential Router
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=credential-router
Group=credential-router
ExecStart=${bin} -config ${config}
Restart=on-failure
RestartSec=2s
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF
  echo "wrote $unit"
  systemctl daemon-reload 2>/dev/null || true
}

# ── actions ───────────────────────────────────────────────────────────────────

install_action() {
  echo "=== install (systemd) ==="

  if ! have_systemd; then
    echo "error: install requires a systemd host (/run/systemd/system missing or PID-1 not systemd)" >&2
    exit 7
  fi

  local config db_path installed_bin
  if ! config="$(resolve_config)"; then
    exit 7
  fi
  if ! installed_bin="$(find_binary)"; then
    echo "error: no binary found — build first: ./scripts/build.sh x86_64 (or arm64)" >&2
    exit 7
  fi
  DATA_DIR="$(derive_data_dir_from_config "$config")"
  SECRETS_DIR="${DATA_DIR}/secrets"
  db_path="${DATA_DIR}/credentials.db"

  if [[ "$FORCE" == "1" ]]; then
    backup_file "$db_path" 2>/dev/null || true
    rm -f "$db_path" "${db_path}-wal" "${db_path}-shm"
    rm -f "$SECRETS_DIR"/s1.bin.* "$SECRETS_DIR"/crypto_mode
    echo "purged existing data and secrets (fresh re-init)"
  elif [[ -f "$db_path" ]]; then
    echo "warning: $db_path already exists (existing key metadata)." >&2
    if ! prompt_yes_no "Re-initialize will invalidate existing keys. Backup DB and continue?"; then
      echo "aborted (declined). re-run with --force for fresh re-init." >&2
      exit 4
    fi
    backup_file "$db_path"
  fi

  mkdir -p "$DATA_DIR" "$SECRETS_DIR"
  chmod 700 "$SECRETS_DIR"
  chown -R credential-router:credential-router "$DATA_DIR" 2>/dev/null || \
    echo "warning: chown credential-router:credential-router failed (run as root, or unit's User= must match)" >&2

  write_systemd_unit "$installed_bin"
  systemctl daemon-reload 2>/dev/null || true

  echo "=== install complete ==="
  echo "data_dir    : $DATA_DIR"
  echo "secrets_dir : $SECRETS_DIR (binary writes s1.bin.1 + crypto_mode on first start)"
  echo "binary      : $installed_bin"
  echo "config      : $config"
  echo "unit        : /etc/systemd/system/credential-router.service"
  echo "next        : systemctl enable --now credential-router  (or: ./scripts/start.sh start --systemd)"
}

start_action() {
  local bin config
  if ! bin="$(find_binary)"; then
    exit 7
  fi
  if ! config="$(resolve_config)"; then
    exit 7
  fi
  if [[ "$WITH_SYSTEMD" == "1" ]]; then
    if ! have_systemd; then
      echo "error: --systemd requires a systemd host (drop the flag for foreground/pidfile mode)" >&2
      exit 7
    fi
    echo "starting via systemctl..."
    if ! systemctl start credential-router; then
      echo "error: systemctl start credential-router failed" >&2
      exit 7
    fi
    if ! systemctl is-active --wait credential-router >/dev/null 2>&1; then
      echo "error: credential-router failed to start (check: journalctl -u credential-router -n 50)" >&2
      systemctl status credential-router --no-pager --lines=10 >&2 2>/dev/null || true
      exit 7
    fi
    echo "started (systemd: credential-router)"
  else
    echo "starting in foreground (pidfile mode)..."
    mkdir -p "$(dirname "$PIDFILE")"
    echo $$ > "$PIDFILE"
    exec "$bin" -config "$config"
  fi
}

stop_action() {
  if [[ "$WITH_SYSTEMD" == "1" ]]; then
    if ! have_systemd; then
      echo "error: --systemd requires a systemd host (drop the flag for pidfile mode)" >&2
      exit 7
    fi
    if systemctl stop credential-router 2>/dev/null; then
      echo "stopped (systemd: credential-router)"
      exit 0
    fi
    echo "error: systemctl stop credential-router failed" >&2
    exit 8
  fi

  local pid_file="$PIDFILE"
  if [[ ! -f "$pid_file" ]]; then
    echo "stop: no pid file at $pid_file; nothing to stop"
    exit 0   # idempotent (see top-of-file NOTE)
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "$pid" || ! "$pid" =~ ^[0-9]+$ ]]; then
    echo "error: malformed pid file $pid_file (content: '$pid')" >&2
    rm -f "$pid_file"
    exit 8
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "stop: pid $pid not running; removing stale pid file"
    rm -f "$pid_file"
    exit 0
  fi

  echo "stop: sending SIGTERM to $pid"
  kill -TERM "$pid" 2>/dev/null || true
  local i
  for i in {1..10}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$pid_file"
      echo "stop: process exited"
      exit 0
    fi
    sleep 0.5
  done

  echo "stop: $pid did not exit after 5s; sending SIGKILL" >&2
  kill -KILL "$pid" 2>/dev/null || true
  rm -f "$pid_file"
  if kill -0 "$pid" 2>/dev/null; then
    echo "error: failed to stop pid $pid" >&2
    exit 8
  fi
  echo "stop: killed"
  exit 0
}

# ── usage / dispatch ──────────────────────────────────────────────────────────

usage() {
  cat >&2 <<'EOF'
usage: start.sh {install|start|stop} [options]

actions:
  install   write /etc/systemd/system/credential-router.service +
            chown data_dir. ALWAYS systemd. Errors on non-systemd hosts.
            Build binary first: ./scripts/build.sh x86_64 (or arm64).
  start     start the service: foreground exec without --systemd,
            systemctl with --systemd.
  stop      stop the service: pidfile + SIGTERM/SIGKILL without
            --systemd, systemctl with --systemd.

options:
  --systemd              (start/stop only) route through systemctl.
                          install ALWAYS writes the unit; this flag has
                          no effect on install. start/stop without it
                          always run foreground + pidfile even on
                          systemd hosts.
  --bin=PATH             override binary path (default: auto-discover
                          dist/bin/, /usr/local/bin, PATH). Also $BIN env.
                          Used by start/stop to locate the executable
                          and by install to set ExecStart in the unit.
  --config=PATH          override config path (default: ROOT/config.yaml
                          → ROOT/config.example.yaml). Also $CONFIG env.
  --force                install: backup + purge data_dir/credentials.db
                          + secrets/* before writing the systemd unit.
                          Does not touch backup_dir. Binary regenerates
                          key material on next start.
  -h, --help             show this help

Notes:
  * data_dir is NOT a script flag — set it in config.yaml.
  * build binary first via ./scripts/build.sh; install/start/stop do
    not build (start.sh has no go toolchain dependency).
EOF
}

case "${action:-}" in
  install) install_action ;;
  start)   start_action ;;
  stop)    stop_action ;;
  help)    usage; exit 0 ;;
  "")      echo "error: no action given" >&2; usage; exit 2 ;;
  *)       echo "error: unknown action: ${action}" >&2; usage; exit 2 ;;
esac
