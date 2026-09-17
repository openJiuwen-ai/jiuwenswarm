#!/usr/bin/env bash

set -euo pipefail

SERVER_IP="${SERVER_IP:-}"
DEFAULT_TEAM_WORKSPACE="${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}"
EXPORT_DIR="${EXPORT_DIR:-${DEFAULT_TEAM_WORKSPACE}}"
MOUNT_POINT="${MOUNT_POINT:-${DEFAULT_TEAM_WORKSPACE}}"
MOUNT_POINTS=()
EXPORT_DIRS=()
CLEAN_ALL_SERVER_ENTRIES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-ip)
      SERVER_IP="$2"
      shift 2
      ;;
    --mount-point)
      MOUNT_POINTS+=("$2")
      shift 2
      ;;
    --export-dir)
      EXPORT_DIRS+=("$2")
      shift 2
      ;;
    --clean-all-server-entries)
      CLEAN_ALL_SERVER_ENTRIES=1
      shift
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  sudo bash scripts/nfs/teardown_nfs_client.sh [options]

Options:
  --server-ip <ip>              NFS server IP. Required unless --clean-all-server-entries is set.
  --mount-point <path>          Local mount path to unmount and remove from /etc/fstab. Repeatable.
  --export-dir <path>           Used with --mount-point to remove exact fstab entry. Repeatable.
  --clean-all-server-entries    Remove all /etc/fstab nfs4 lines that start with <server-ip>:

Defaults:
  mount/export path: ${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}
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

if [[ "${#MOUNT_POINTS[@]}" -eq 0 ]] && [[ "${CLEAN_ALL_SERVER_ENTRIES}" -eq 0 ]]; then
  MOUNT_POINTS+=("${MOUNT_POINT}")
fi
if [[ "${#EXPORT_DIRS[@]}" -eq 0 ]] && [[ "${CLEAN_ALL_SERVER_ENTRIES}" -eq 0 ]] && [[ -n "${EXPORT_DIR}" ]]; then
  EXPORT_DIRS+=("${EXPORT_DIR}")
fi

if [[ "${#MOUNT_POINTS[@]}" -eq 0 ]] && [[ "${CLEAN_ALL_SERVER_ENTRIES}" -eq 0 ]]; then
  echo "You must pass at least one --mount-point, or set --clean-all-server-entries." >&2
  exit 1
fi

if [[ -z "${SERVER_IP}" ]] && [[ "${CLEAN_ALL_SERVER_ENTRIES}" -eq 1 ]]; then
  echo "--server-ip is required when --clean-all-server-entries is set." >&2
  exit 1
fi

if [[ "${#EXPORT_DIRS[@]}" -gt 0 ]] && [[ "${#EXPORT_DIRS[@]}" -ne "${#MOUNT_POINTS[@]}" ]]; then
  echo "When --export-dir is provided, its count must match --mount-point count." >&2
  exit 1
fi

cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"

for mount_point in "${MOUNT_POINTS[@]}"; do
  umount "${mount_point}" 2>/dev/null || true
done

mount_points_env="$(printf '%s\n' "${MOUNT_POINTS[@]}")"
export_dirs_env="$(printf '%s\n' "${EXPORT_DIRS[@]}")"
export JIUWEN_NFS_SERVER_IP="${SERVER_IP}"
export JIUWEN_NFS_CLEAN_ALL="${CLEAN_ALL_SERVER_ENTRIES}"
export JIUWEN_NFS_MOUNT_POINTS="${mount_points_env}"
export JIUWEN_NFS_EXPORT_DIRS="${export_dirs_env}"

/usr/bin/python3 - <<PY
import os
from pathlib import Path

fstab = Path("/etc/fstab")
lines = fstab.read_text().splitlines()
out = []

server_ip = os.environ["JIUWEN_NFS_SERVER_IP"]
clean_all = int(os.environ["JIUWEN_NFS_CLEAN_ALL"])
mount_points = os.environ.get("JIUWEN_NFS_MOUNT_POINTS", "")
export_dirs = os.environ.get("JIUWEN_NFS_EXPORT_DIRS", "")

mp_list = [line for line in mount_points.splitlines() if line]
ed_list = [line for line in export_dirs.splitlines() if line]
exact_pairs = set(zip(ed_list, mp_list)) if ed_list else set()

for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        out.append(line)
        continue
    parts = stripped.split()
    if len(parts) < 4:
        out.append(line)
        continue
    source, target, fstype = parts[0], parts[1], parts[2]
    if fstype != "nfs4":
        out.append(line)
        continue
    if clean_all and source.startswith(f"{server_ip}:"):
        continue
    removed = False
    if target in mp_list:
        if exact_pairs:
            for export_dir, mount_point in exact_pairs:
                if source == f"{server_ip}:{export_dir}" and target == mount_point:
                    removed = True
                    break
        else:
            removed = True
    if removed:
        continue
    out.append(line)

fstab.write_text("\n".join(out) + ("\n" if out else ""))
print("Updated /etc/fstab")
PY

cat <<EOF

NFS client teardown completed.

Client side:
  server ip       : ${SERVER_IP:-<not provided>}
  mount points    : ${MOUNT_POINTS[*]:-<none>}
  export dirs     : ${EXPORT_DIRS[*]:-<none>}
  clean all lines : ${CLEAN_ALL_SERVER_ENTRIES}

EOF
