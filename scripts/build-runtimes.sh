#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# Node 运行时的解析与绑定函数，build-macos.sh 与 build-electron-exe.sh 的
# 单一来源。两条 macOS 打包链路禁止在各自脚本内复制这些实现，否则会再次漂移
# （此前 Electron 打包漏绑 Node，导致依赖 node 的 Agent 技能如 ppt-creation
# 报"环境没有 node"）。契约测试 tests/unit_tests/test_desktop_electron_contract.py
# 钉住：共享模块 → 打包脚本调用 → 落地路径 → 冻结入口查找。
#
# 使用方必须先定义 PROJECT_ROOT，再 source 本文件。

# 任选 ≥ v18 的 LTS；v20/v22 为推荐版本。可用环境变量覆盖。
NODE_VERSION="${NODE_VERSION:-v22.11.0}"
BUNDLE_NODE="${BUNDLE_NODE:-1}"   # 0=跳过内置 node

# 返回构建机的 Node 架构名。M 系列(arm64) 优先；Intel 回退 x64。
resolve_node_arch() {
  case "$(uname -m)" in
    arm64) echo "arm64" ;;
    *)     echo "x64"   ;;
  esac
}

# 校验某个目录是一份「能真正跑起来」的 Node：既有 bin/node，又能执行 --version。
# 比 [[ -x ... ]] 强：能挡住下载损坏 / 架构不匹配 / 只解压了一半的情况。
node_runs() {
  local n="$1/bin/node"
  [[ -x "$n" ]] && "$n" --version >/dev/null 2>&1
}

# 解析 Node 运行时来源，优先级：$NODE_DIR > vendor/node > nodejs.org 官方下载
resolve_node_dir() {
  if [[ -n "${NODE_DIR:-}" ]] && node_runs "$NODE_DIR"; then
    echo "$NODE_DIR"; return
  fi
  local vendored="$PROJECT_ROOT/vendor/node"
  if node_runs "$vendored"; then
    echo "$vendored"; return
  fi
  local arch dir url
  arch="$(resolve_node_arch)"
  dir="$vendored"
  printf 'Downloading Node %s (%s) from nodejs.org...\n' "$NODE_VERSION" "$arch" >&2
  url="https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-darwin-${arch}.tar.gz"
  mkdir -p "$PROJECT_ROOT/vendor"

  # 先把 tarball 下载、解压到临时目录，校验 bin/node 可执行后，
  # 再 rm -rf 旧缓存并原子替换。任一步失败都不会破坏已有 vendor/node。
  local tmpdir staged tarball
  tmpdir="$(mktemp -d)"
  tarball="$tmpdir/node.tar.gz"
  staged="$tmpdir/node-${NODE_VERSION}-darwin-${arch}"

  if ! curl -fL "$url" -o "$tarball"; then
    rm -rf "$tmpdir"
    printf 'Error: failed to download %s\n' "$url" >&2
    return 1
  fi

  tar -xzf "$tarball" -C "$tmpdir"
  if ! node_runs "$staged"; then
    rm -rf "$tmpdir"
    printf 'Error: extracted tarball has no working bin/node (%s)\n' "$staged" >&2
    return 1
  fi

  rm -rf "$dir"          # 仅在替换物校验通过后才删旧缓存
  mv "$staged" "$dir"
  if ! node_runs "$dir"; then   # 跨卷 mv 可能只搬了一半，落地后再校验一次
    rm -rf "$dir" "$tmpdir"
    printf 'Error: node moved into %s but bin/node does not run\n' "$dir" >&2
    return 1
  fi
  rm -rf "$tmpdir"
  echo "$dir"
}

# 把 Node 运行时拷进 dest（build-macos.sh: .app 的 Contents/Resources/node-runtime；
# build-electron-exe.sh: Electron .app 的同名目录）。
copy_node_runtime() {
  local src="$1"
  local dest="$2"
  # 先确认来源 node 真能跑，否则 cp 会吐出晦涩的 'No such file or directory'。
  if ! node_runs "$src"; then
    printf 'Error: node source not usable — %s/bin/node missing or not runnable\n' "$src" >&2
    printf '       if this is the cached vendor/node, remove it and re-run to re-download.\n' >&2
    return 1
  fi
  rm -rf "$dest"
  mkdir -p "$dest"
  # cp -R 保留 npx/npm 符号链接（指向 ../lib/node_modules/...）与可执行位。
  cp -R "$src/." "$dest/"
  printf 'Bundled Node %s (%s) into app: %s\n' \
    "$( "$dest/bin/node" --version )" "$(resolve_node_arch)" "$dest"
}
