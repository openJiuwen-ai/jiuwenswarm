#!/usr/bin/env bash
set -euo >/dev/null 2>&1

# == read key-value pairs from file into array (for first start-up) ==
read_env_from_file() {
    local env_file="$1"
    local -n target_array="$2"

    if [ ! -f "${env_file}" ]; then
        error "Config file not found: ${env_file}"
    fi

    info "Loading config: ${env_file}"

    local key=""
    local value=""
    local buffer=""
    local in_quote=0

    while IFS= read -r line || [[ -n "$line" ]]; do
        trimmed_line="${line#"${line%%[![:space:]]*}"}"
        if [[ $in_quote -eq 0 && ($trimmed_line == \#* || -z "${trimmed_line}") ]]; then
            continue
        fi

        if (( in_quote )); then
            buffer+=$'\n'"$line"
        else
            buffer="$line"
        fi

        if [[ $in_quote -eq 0 && $buffer =~ ^([A-Za-z0-9_]+)=\"(.*) ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            
            if [[ $value =~ (.*)\"[[:space:]]*$ ]]; then
                target_array["$key"]="${BASH_REMATCH[1]}"
                in_quote=0
            else
                buffer="$value"
                in_quote=1
            fi
            continue
        fi

        if (( in_quote )); then
            if [[ $buffer =~ (.*)\"[[:space:]]*$ ]]; then
                target_array["$key"]="${BASH_REMATCH[1]}"
                in_quote=0
                key=""
                buffer=""
            fi
            continue
        fi

        if [[ $buffer =~ ^([A-Za-z0-9_]+)=(.*) ]]; then
            local k="${BASH_REMATCH[1]}"
            local v="${BASH_REMATCH[2]}"
            v="${v#\"}"
            v="${v%\"}"
            target_array["$k"]="$v"
        fi

        buffer=""
    done < "$env_file"

    success "Loaded config: ${env_file}"
}

# ===== Writes sorted key-value pairs to .env.<Instance ID> file =====
write_env_to_file() {
    local env_file=$1
    local -n source_array=$2

    info "Writing $2 to config file: ${env_file}"
    > "${env_file}"
    printf "%s\n" "${!source_array[@]}" | sort | while read -r key; do
        if [ -n "${key}" ]; then
            echo "${key}=${source_array[${key}]}" >> "${env_file}"
        fi
    done
    success "Generated config file : ${env_file}"
}
