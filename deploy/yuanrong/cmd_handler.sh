#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# ===== Execute command with failure control:   ====
# ===== exit on error unless arg2 is 'false' (arg2 optional)  ====
exec_cmd() {
    local fail_quit="true"
    if [[ "${@: -1}" == "false" ]]; then
        fail_quit="false"
        set -- "${@:1:$#-1}"
    fi
    info "Executing: $*"

    if [[ "${fail_quit}" == "false" ]]; then
        "$@" || warning "Command failed (ignored)"
    else
        "$@" || error "Command failed"
    fi
}