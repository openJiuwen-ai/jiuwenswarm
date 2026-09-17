#!/usr/bin/env bash

set -euo pipefail

CLIENT_IP="${CLIENT_IP:-}"
CLIENT_IPS=()
DEFAULT_TEAM_WORKSPACE="${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}"
EXPORT_DIR="${EXPORT_DIR:-${DEFAULT_TEAM_WORKSPACE}}"
MOUNT_POINT="${MOUNT_POINT:-${DEFAULT_TEAM_WORKSPACE}}"
EXPORT_DIRS=()
MOUNT_POINTS=()
FSID="${FSID:-1002}"
EXPORTS_FILE="/etc/exports.d/jiuwenswarm.exports"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --client-ip)
      CLIENT_IPS+=("$2")
      shift 2
      ;;
    --export-dir)
      EXPORT_DIRS+=("$2")
      shift 2
      ;;
    --mount-point)
      MOUNT_POINTS+=("$2")
      shift 2
      ;;
    --fsid)
      FSID="$2"
      shift 2
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  sudo bash scripts/nfs/setup_nfs_server.sh [options]

Options:
  --client-ip <ip>       Allowed NFS client IP. Repeat this option for multiple clients
  --export-dir <path>    Server export directory. Repeatable when paired with --mount-point
  --mount-point <path>   Local mount path. Repeatable and must match --export-dir count
  --fsid <id>            Base NFS filesystem id. Each export increments from this base. Default: 1002

Defaults:
  export/mount path: ${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo bash $0" >&2
  exit 1
fi

if [[ -n "${CLIENT_IP}" ]]; then
  CLIENT_IPS+=("${CLIENT_IP}")
fi

if [[ "${#CLIENT_IPS[@]}" -eq 0 ]]; then
  echo "At least one client IP is required. Pass --client-ip <private-ip> (repeatable) or export CLIENT_IP first." >&2
  exit 1
fi

if [[ "${#EXPORT_DIRS[@]}" -eq 0 ]] && [[ -n "${EXPORT_DIR}" ]]; then
  EXPORT_DIRS+=("${EXPORT_DIR}")
fi
if [[ "${#MOUNT_POINTS[@]}" -eq 0 ]] && [[ -n "${MOUNT_POINT}" ]]; then
  MOUNT_POINTS+=("${MOUNT_POINT}")
fi

if [[ "${#EXPORT_DIRS[@]}" -ne "${#MOUNT_POINTS[@]}" ]]; then
  echo "The number of --export-dir and --mount-point arguments must match." >&2
  exit 1
fi

if [[ "${#EXPORT_DIRS[@]}" -eq 0 ]]; then
  echo "At least one export mapping is required." >&2
  exit 1
fi

install_nfs_server() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-kernel-server
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y nfs-utils
  elif command -v yum >/dev/null 2>&1; then
    yum install -y nfs-utils
  else
    echo "Unsupported package manager. Install NFS server packages manually." >&2
    exit 1
  fi
}

enable_nfs_service() {
  if systemctl list-unit-files | grep -q '^nfs-kernel-server\.service'; then
    systemctl enable --now nfs-kernel-server
  else
    systemctl enable --now nfs-server
  fi
}

ensure_fstab_line() {
  local line="$1"
  local file="$2"
  if ! grep -Fqx "$line" "$file" 2>/dev/null; then
    printf '%s\n' "$line" >> "$file"
  fi
}

echo "[1/6] Installing NFS server packages"
install_nfs_server

echo "[2/6] Preparing shared directories"
for export_dir in "${EXPORT_DIRS[@]}"; do
  mkdir -p "${export_dir}"
  chmod 755 "${export_dir}"
done

echo "[3/6] Writing export rule to ${EXPORTS_FILE}"
mkdir -p /etc/exports.d
{
  for idx in "${!EXPORT_DIRS[@]}"; do
    export_dir="${EXPORT_DIRS[$idx]}"
    export_fsid=$((FSID + idx))
    first=1
    for ip in "${CLIENT_IPS[@]}"; do
      if [[ "${first}" -eq 1 ]]; then
        printf "%s %s(rw,sync,no_subtree_check,no_root_squash,fsid=%s)\n" "${export_dir}" "${ip}" "${export_fsid}"
        first=0
      else
        printf "%s %s(rw,sync,no_subtree_check,no_root_squash)\n" "${export_dir}" "${ip}"
      fi
    done
  done
} > "${EXPORTS_FILE}"

echo "[4/6] Reloading exports"
exportfs -rav

echo "[5/6] Enabling NFS service"
enable_nfs_service

echo "[6/6] Creating local bind mounts"
for idx in "${!EXPORT_DIRS[@]}"; do
  export_dir="${EXPORT_DIRS[$idx]}"
  mount_point="${MOUNT_POINTS[$idx]}"
  mkdir -p "${mount_point}"
  if [[ "${export_dir}" != "${mount_point}" ]]; then
    mountpoint -q "${mount_point}" || mount --bind "${export_dir}" "${mount_point}"
    ensure_fstab_line "${export_dir} ${mount_point} none bind 0 0" /etc/fstab
  fi
done

cat <<EOF

NFS server is ready.

Server node:
  clients    : ${CLIENT_IPS[*]}
  fsid base  : ${FSID}

Export mappings:
$(for idx in "${!EXPORT_DIRS[@]}"; do printf "  [%s] %s -> %s (fsid=%s)\n" "$((idx + 1))" "${EXPORT_DIRS[$idx]}" "${MOUNT_POINTS[$idx]}" "$((FSID + idx))"; done)

Next step on the client node:
  sudo bash scripts/nfs/setup_nfs_client.sh --server-ip <server-private-ip>

If a firewall is enabled on this server, open the required NFS ports manually.
EOF
