#!/usr/bin/env bash

set -euo pipefail

SERVER_IP="${SERVER_IP:-}"
DEFAULT_TEAM_WORKSPACE="${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}"
EXPORT_DIR="${EXPORT_DIR:-${DEFAULT_TEAM_WORKSPACE}}"
MOUNT_POINT="${MOUNT_POINT:-${DEFAULT_TEAM_WORKSPACE}}"
EXPORT_DIRS=()
MOUNT_POINTS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-ip)
      SERVER_IP="$2"
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
    -h|--help)
      cat <<'EOF'
Usage:
  sudo bash scripts/nfs/setup_nfs_client.sh [options]

Options:
  --server-ip <ip>       NFS server IP. Required unless SERVER_IP is set
  --export-dir <path>    Server export directory. Repeatable when paired with --mount-point
  --mount-point <path>   Local mount path. Repeatable and must match --export-dir count

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

if [[ -z "${SERVER_IP}" ]]; then
  echo "SERVER_IP is required. Pass --server-ip <private-ip> or export SERVER_IP first." >&2
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

install_nfs_client() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-common
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y nfs-utils
  elif command -v yum >/dev/null 2>&1; then
    yum install -y nfs-utils
  else
    echo "Unsupported package manager. Install NFS client packages manually." >&2
    exit 1
  fi
}

ensure_fstab_line() {
  local line="$1"
  local file="$2"
  if ! grep -Fqx "$line" "$file" 2>/dev/null; then
    printf '%s\n' "$line" >> "$file"
  fi
}

backup_existing_mount_point() {
  local mount_point="$1"
  if mountpoint -q "${mount_point}"; then
    return
  fi

  if [[ -d "${mount_point}" ]] && [[ -n "$(find "${mount_point}" -mindepth 1 -maxdepth 1 2>/dev/null)" ]]; then
    local backup_dir="${mount_point}.pre_nfs_backup_$(date +%Y%m%d_%H%M%S)"
    echo "Backing up existing local workspace to ${backup_dir}"
    mv "${mount_point}" "${backup_dir}"
    mkdir -p "${mount_point}"
  fi
}

echo "[1/4] Installing NFS client packages"
install_nfs_client

echo "[2/4] Creating mount points and preparing backups"
for mount_point in "${MOUNT_POINTS[@]}"; do
  mkdir -p "${mount_point}"
  backup_existing_mount_point "${mount_point}"
done

echo "[3/4] Mounting exports"
for idx in "${!EXPORT_DIRS[@]}"; do
  export_dir="${EXPORT_DIRS[$idx]}"
  mount_point="${MOUNT_POINTS[$idx]}"
  mountpoint -q "${mount_point}" || mount -t nfs4 -o vers=4.1 "${SERVER_IP}:${export_dir}" "${mount_point}"
done

echo "[4/4] Persisting mounts to /etc/fstab"
for idx in "${!EXPORT_DIRS[@]}"; do
  export_dir="${EXPORT_DIRS[$idx]}"
  mount_point="${MOUNT_POINTS[$idx]}"
  fstab_line="${SERVER_IP}:${export_dir} ${mount_point} nfs4 vers=4.1,_netdev,defaults 0 0"
  ensure_fstab_line "${fstab_line}" /etc/fstab
done

cat <<EOF

NFS client is ready.

Client node:
  server     : ${SERVER_IP}
$(for idx in "${!EXPORT_DIRS[@]}"; do printf "  [%s] %s -> %s\n" "$((idx + 1))" "${EXPORT_DIRS[$idx]}" "${MOUNT_POINTS[$idx]}"; done)

Quick verification:
  touch <mount-point>/nfs_client_probe.txt
  ls -la <mount-point>
EOF
