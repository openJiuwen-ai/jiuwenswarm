#!/bin/bash
# build-harmony-hap.sh — Manual HAP build script for JiuwenSwarm on HarmonyOS
#
# This script builds the .hap package WITHOUT DevEco Studio, using only CLI tools.
# It performs the complete 5-stage build pipeline:
#   Stage 1: Resource compilation (restool)
#   Stage 2: ETS/ArkTS compilation (es2abc via ets-loader)
#   Stage 3: Native C++ NAPI compilation (cmake+ninja)
#   Stage 4: HAP packing (ohos_packing_tool)
#   Stage 5: HAP signing (hap-sign-tool with debug certificates)
#
# Usage:
#   ./scripts/build-harmony-hap.sh
#   ./scripts/build-harmony-hap.sh --skip-ets    # Skip ETS compilation (debug only)
#   ./scripts/build-harmony-hap.sh --skip-sign   # Skip signing (produce unsigned HAP)
#
# Prerequisites:
#   - All CLI tools in /data/service/hnp/bin/
#   - OHOS SDK at /data/service/hnp/ohos-sdk.org/ohos-sdk_26.0.0.18/
#   - HNP package at harmony_facade/hnp/arm64-v8a/jiuwen.hnp
#   - build-harmony.sh already executed (frontend + HNP + rawfile prepared)

set -euo pipefail

# ─── Configuration ───
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
JIUWENSWARM_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
HARMONY_PROJECT="${JIUWENSWARM_REPO}/../harmony_facade"
BUILD_DIR="$HOME/yangzequ/harmony_hap_build"
SDK_ROOT="/data/service/hnp/ohos-sdk.org/ohos-sdk_26.0.0.18/ohos"
HNPCLI="/data/service/hnp/bin/hnpcli"
RESTOOL="/data/service/hnp/bin/restool"
ES2ABC="${SDK_ROOT}/ets/build-tools/ets-loader/bin/ark/build/bin/es2abc"
ETS_LOADER="${SDK_ROOT}/ets/build-tools/ets-loader"
CMAKE="/data/service/hnp/bin/cmake"
NINJA="/data/service/hnp/bin/ninja"
PACK_TOOL="/data/service/hnp/bin/ohos_packing_tool"
SIGN_TOOL="/data/service/hnp/bin/hap-sign-tool"
LLVM_ROOT="${SDK_ROOT}/native/llvm"
NATIVE_SYSROOT="${SDK_ROOT}/native/sysroot"
NODE="/data/service/hnp/bin/node"

BUNDLE_NAME="com.openjiuwen.harmony_facade"
MODULE_NAME="entry"

# Debug signing constants
DEBUG_KEY_ALIAS="jiuwenswarm-debug-key"
DEBUG_KEYSTORE_PWD="123456"
DEBUG_KEYSTORE="$BUILD_DIR/debug-keystore.p12"
DEBUG_APP_CERT="$BUILD_DIR/debug-app-cert.cer"
DEBUG_PROFILE="$BUILD_DIR/debug-profile.p7b"
SDK_P12="${SDK_ROOT}/toolchains/lib/OpenHarmony.p12"
SDK_PROFILE_PEM="${SDK_ROOT}/toolchains/lib/OpenHarmonyProfileDebug.pem"
SDK_APP_PEM="${SDK_ROOT}/toolchains/lib/OpenHarmonyApplication.pem"
SDK_PROFILE_TEMPLATE="${SDK_ROOT}/toolchains/lib/UnsgnedDebugProfileTemplate.json"

# Parse arguments
SKIP_ETS=false
SKIP_SIGN=false
SKIP_CPP=false
for arg in "$@"; do
  case "$arg" in
    --skip-ets)  SKIP_ETS=true ;;
    --skip-sign) SKIP_SIGN=true ;;
    --skip-cpp)  SKIP_CPP=true ;;
    --help)
      echo "Usage: $0 [--skip-ets] [--skip-sign] [--skip-cpp]"
      exit 0
      ;;
  esac
done

echo "============================================"
echo " JiuwenSwarm 鸿蒙 .hap 手动构建脚本"
echo "============================================"
echo ""
echo "项目: $HARMONY_PROJECT"
echo "构建目录: $BUILD_DIR"
echo "SDK: $SDK_ROOT"
echo ""

# ─── Clean and create build directory ───
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# ─── Helper: Convert JSON5 to JSON using Node.js ───
json5_to_json() {
  local srcfile="$1"
  local destfile="$2"
  # Use Node.js to parse JSON5 and convert to standard JSON
  # Write converter script to temp file to avoid bash variable conflicts
  local converter="$BUILD_DIR/json5_converter.js"
  cat > "$converter" << 'JSEOF'
const fs = require('fs');
const srcFile = process.argv[2];
const destFile = process.argv[3];
let content = fs.readFileSync(srcFile, 'utf8');
// Remove single-line comments (but not URLs like http://)
content = content.replace(/\/\/[^:]*$/gm, '');
// Remove multi-line comments
content = content.replace(/\/\*[\s\S]*?\*\//g, '');
// Remove trailing commas before } or ]
content = content.replace(/,\s*([\]}])/g, '$1');
// Try to parse as JSON, fallback to eval for JSON5
try {
  const obj = JSON.parse(content);
  fs.writeFileSync(destFile, JSON.stringify(obj, null, 2));
  console.log('JSON5 parsed as JSON successfully');
} catch(e) {
  try {
    // JSON5 allows unquoted keys, trailing commas, etc.
    const obj = (new Function('return ' + content))();
    fs.writeFileSync(destFile, JSON.stringify(obj, null, 2));
    console.log('JSON5 parsed via eval successfully');
  } catch(e2) {
    console.error('Failed to parse JSON5: ' + e2.message);
    process.exit(1);
  }
}
JSEOF
  node "$converter" "$srcfile" "$destfile" 2>&1 || {
    echo "ERROR: Failed to convert JSON5: $srcfile"
    exit 1
  }
}

# ═══════════════════════════════════════════════
# Stage 1: Resource Compilation (restool)
# ═══════════════════════════════════════════════
echo "=== Stage 1: Resource Compilation (restool) ==="

# 1a: Convert module.json5 to module.json
json5_to_json "$HARMONY_PROJECT/entry/src/main/module.json5" "$BUILD_DIR/module.json"
echo "✅ module.json5 → module.json"

# 1b: Convert app.json5 to app.json
json5_to_json "$HARMONY_PROJECT/AppScope/app.json5" "$BUILD_DIR/app.json"
echo "✅ app.json5 → app.json"

# 1c: Generate placeholder ResourceTable header for restool
mkdir -p "$BUILD_DIR/entry/resources"
# restool needs a ResourceTable.h path via -r flag. Create empty placeholder.
# The actual ResourceTable.h will be generated by restool itself.
RES_HEADER="$BUILD_DIR/ResourceTable.h"
touch "$RES_HEADER"

$RESTOOL \
-i "$HARMONY_PROJECT/entry/src/main/resources" \
-p "${BUNDLE_NAME}.${MODULE_NAME}" \
-j "$BUILD_DIR/module.json" \
-o "$BUILD_DIR/entry/resources" \
-r "$RES_HEADER" \
--ids "$BUILD_DIR/entry/resources/id_defined.json" \
-f \
2>&1 || {
echo "ERROR: restool failed for entry resources"
exit 1
}
echo "✅ Entry resources compiled"

# 1d: Compile app-level resources
mkdir -p "$BUILD_DIR/app/resources"
APP_RES_HEADER="$BUILD_DIR/app/ResourceTable.h"
touch "$APP_RES_HEADER"

$RESTOOL \
-i "$HARMONY_PROJECT/AppScope/resources" \
-p "${BUNDLE_NAME}" \
-o "$BUILD_DIR/app/resources" \
-r "$APP_RES_HEADER" \
--ids "$BUILD_DIR/app/resources/id_defined.json" \
-f \
2>&1 || {
echo "ERROR: restool failed for app resources"
exit 1
}
echo "✅ App-level resources compiled"

echo ""

# ═══════════════════════════════════════════════
# Stage 2: ETS/ArkTS Compilation
# ═══════════════════════════════════════════════
if [ "$SKIP_ETS" = "true" ]; then
  echo "=== Stage 2: ETS/ArkTS Compilation [SKIPPED] ==="
  echo "⚠️  Skipping ETS compilation. The HAP will NOT have working UI!"
  echo "   This is only for testing the build pipeline."
  echo ""
  # Create placeholder ets directory
  mkdir -p "$BUILD_DIR/entry/ets"
else
  echo "=== Stage 2: ETS/ArkTS Compilation ==="

  # Set up environment for ets-loader
  export aceModuleRoot="$HARMONY_PROJECT/entry/src/main/ets"
  export aceModuleBuild="$BUILD_DIR/entry/ets"
  export aceModuleJsonPath="$BUILD_DIR/module.json"
  export compileMode="esmodule"
  export aceCompileMode="moduleJson"
  export buildArkMode="debug"
  export cachePath="$BUILD_DIR/cache"
  export sdkInfo="ohos-sdk_26"
  export panda="$ES2ABC"
  export NODE_OPTIONS="--expose-gc"
  export projectRoot="$HARMONY_PROJECT"
  export pathToResource="$BUILD_DIR/entry/resources"
  export resourceTable="$BUILD_DIR/entry/resources/ResourceTable.index"

  mkdir -p "$BUILD_DIR/cache" "$BUILD_DIR/entry/ets"

  # Try to use ets-loader via rollup
  cd "$ETS_LOADER"

  # Check if node_modules are installed
  if [ ! -d "$ETS_LOADER/node_modules" ]; then
    echo "⚠️  ets-loader node_modules not found. Installing..."
    npm install 2>&1 || {
      echo "ERROR: Failed to install ets-loader dependencies"
      echo "Falling back to --skip-ets mode"
      SKIP_ETS=true
      mkdir -p "$BUILD_DIR/entry/ets"
    }
  fi

  if [ "$SKIP_ETS" = "false" ]; then
    node "$ETS_LOADER/main.js" 2>&1 || {
      echo "ERROR: ets-loader compilation failed"
      echo "Falling back to --skip-ets mode"
      SKIP_ETS=true
      mkdir -p "$BUILD_DIR/entry/ets"
    }
  fi

  if [ "$SKIP_ETS" = "false" ]; then
    echo "✅ ETS/ArkTS compiled to .abc bytecode"
  fi

  cd "$JIUWENSWARM_REPO"
  echo ""
fi

# ═══════════════════════════════════════════════
# Stage 3: Native C++ NAPI Compilation
# ═══════════════════════════════════════════════
if [ "$SKIP_CPP" = "true" ]; then
  echo "=== Stage 3: Native C++ NAPI Compilation [SKIPPED] ==="
  echo ""
else
  echo "=== Stage 3: Native C++ NAPI Compilation ==="

  # 3a: Generate OHOS cross-compilation toolchain file
  cat > "$BUILD_DIR/ohos-toolchain.cmake" << 'EOF_CMAKE'
set(CMAKE_SYSTEM_NAME OHOS)
set(CMAKE_SYSTEM_PROCESSOR aarch64)
set(CMAKE_C_COMPILER /data/service/hnp/ohos-sdk.org/ohos-sdk_26.0.0.18/ohos/native/llvm/bin/clang)
set(CMAKE_CXX_COMPILER /data/service/hnp/ohos-sdk.org/ohos-sdk_26.0.0.18/ohos/native/llvm/bin/clang++)
set(CMAKE_AR /data/service/hnp/ohos-sdk.org/ohos-sdk_26.0.0.18/ohos/native/llvm/bin/llvm-ar)
set(CMAKE_RANLIB /data/service/hnp/ohos-sdk.org/ohos-sdk_26.0.0.18/ohos/native/llvm/bin/llvm-ranlib)
set(CMAKE_LINKER /data/service/hnp/ohos-sdk.org/ohos-sdk_26.0.0.18/ohos/native/llvm/bin/ld.lld)
set(CMAKE_SYSROOT /data/service/hnp/ohos-sdk.org/ohos-sdk_26.0.0.18/ohos/native/sysroot)
set(CMAKE_FIND_ROOT_PATH ${CMAKE_SYSROOT})
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
# OHOS-specific flags
set(CMAKE_C_FLAGS "--target=aarch64-linux-ohos -fdata-sections -ffunction-sections -funwind-tables -fno-rtti" CACHE STRING "" FORCE)
set(CMAKE_CXX_FLAGS "${CMAKE_C_FLAGS} -fno-exceptions -frtti" CACHE STRING "" FORCE)
set(CMAKE_SHARED_LINKER_FLAGS "--target=aarch64-linux-ohos -Wl,--gc-sections -Wl,--build-id=sha1" CACHE STRING "" FORCE)
EOF_CMAKE

  echo "✅ OHOS toolchain file generated"

  # 3b: Run cmake
  mkdir -p "$BUILD_DIR/cpp-build"
  $CMAKE \
    -G Ninja \
    -DCMAKE_TOOLCHAIN_FILE="$BUILD_DIR/ohos-toolchain.cmake" \
    -DCMAKE_BUILD_TYPE=Debug \
    -S "$HARMONY_PROJECT/entry/src/main/cpp" \
    -B "$BUILD_DIR/cpp-build" \
    2>&1 || {
      echo "ERROR: cmake configuration failed"
      exit 1
    }
  echo "✅ cmake configured"

  # 3c: Build with ninja
  $NINJA -C "$BUILD_DIR/cpp-build" 2>&1 || {
    echo "ERROR: ninja build failed"
    exit 1
  }
  echo "✅ libjiuwenswarm_native.so built"

  # Verify output
  SO_FILE="$BUILD_DIR/cpp-build/libjiuwenswarm_native.so"
  if [ ! -f "$SO_FILE" ]; then
    echo "ERROR: .so file not found at expected path"
    find "$BUILD_DIR/cpp-build" -name "*.so" 2>/dev/null
    exit 1
  fi
  echo "   .so size: $(du -sh "$SO_FILE" | cut -f1)"
  echo ""
fi

# ═══════════════════════════════════════════════
# Stage 4: HAP Packing
# ═══════════════════════════════════════════════
echo "=== Stage 4: HAP Packing ==="

# Prepare the rawfile directory (copy harmony_entry.py)
RAWFILE_DIR="$BUILD_DIR/entry/resources/rawfile"
mkdir -p "$RAWFILE_DIR"

# Copy harmony_entry.py to rawfile
ENTRY_SCRIPT="$JIUWENSWARM_REPO/scripts/harmony_entry.py"
if [ -f "$ENTRY_SCRIPT" ]; then
  cp "$ENTRY_SCRIPT" "$RAWFILE_DIR/"
  echo "✅ harmony_entry.py copied to rawfile"
fi

# Copy HNP package if available
HNP_FILE="$HARMONY_PROJECT/hnp/arm64-v8a/jiuwen.hnp"
if [ -f "$HNP_FILE" ]; then
  echo "✅ HNP package available: $(du -sh "$HNP_FILE" | cut -f1)"
fi

# Prepare lib directory for the .so
LIB_DIR="$BUILD_DIR/libs/arm64-v8a"
mkdir -p "$LIB_DIR"
if [ "$SKIP_CPP" = "false" ]; then
  cp "$BUILD_DIR/cpp-build/libjiuwenswarm_native.so" "$LIB_DIR/"
  echo "✅ libjiuwenswarm_native.so copied to libs/"
fi

# Pack HAP using ohos_packing_tool
UNSIGNED_HAP="$BUILD_DIR/entry-default-unsigned.hap"

$PACK_TOOL pack \
  --mode hap \
  --json-path "$BUILD_DIR/module.json" \
  --ets-path "$BUILD_DIR/entry/ets" \
  --res-path "$BUILD_DIR/entry/resources" \
  --index-path "$BUILD_DIR/entry/resources/resources.index" \
  --lib-path "$LIB_DIR" \
  --out-path "$UNSIGNED_HAP"
  2>&1 || {
    echo "ERROR: HAP packing failed"
    echo "Trying alternative packing method (manual zip)..."
    
    # Alternative: manual zip packing
    # HAP is a zip file with specific structure
    cd "$BUILD_DIR"
    
    # Create HAP structure
    HAP_DIR="$BUILD_DIR/hap_contents"
    mkdir -p "$HAP_DIR"
    
    # Copy module.json
    cp "$BUILD_DIR/module.json" "$HAP_DIR/module.json"
    
    # Copy resources
    cp -a "$BUILD_DIR/entry/resources/"* "$HAP_DIR/" 2>/dev/null || true
    
    # Copy ets bytecode
    if [ -d "$BUILD_DIR/entry/ets" ]; then
      mkdir -p "$HAP_DIR/ets"
      cp -a "$BUILD_DIR/entry/ets/"* "$HAP_DIR/ets/" 2>/dev/null || true
    fi
    
    # Copy libs
    if [ -d "$LIB_DIR" ] && [ "$(ls -A "$LIB_DIR")" ]; then
      cp -a "$LIB_DIR" "$HAP_DIR/libs/" 2>/dev/null || true
    fi
    
    # Pack as zip (HAP format)
    cd "$HAP_DIR"
    zip -r "$UNSIGNED_HAP" . 2>&1 || {
      echo "ERROR: Manual zip packing failed"
      exit 1
    }
    
    echo "✅ HAP packed manually (zip method)"
  }

if [ -f "$UNSIGNED_HAP" ]; then
  echo "✅ Unsigned HAP created: $UNSIGNED_HAP"
  echo "   HAP size: $(du -sh "$UNSIGNED_HAP" | cut -f1)"
fi

echo ""

# ═══════════════════════════════════════════════
# Stage 5: HAP Signing
# ═══════════════════════════════════════════════
if [ "$SKIP_SIGN" = "true" ]; then
  echo "=== Stage 5: HAP Signing [SKIPPED] ==="
  echo "⚠️  Unsigned HAP: $UNSIGNED_HAP"
  echo "   The HAP cannot be installed without signing."
  echo ""
else
  echo "=== Stage 5: HAP Signing (debug) ==="

  SIGNED_HAP="$BUILD_DIR/entry-default-signed.hap"

  # 5a: Generate debug keystore
  $SIGN_TOOL generate-keypair \
    -keyAlias "$DEBUG_KEY_ALIAS" \
    -keyAlg ECC \
    -keySize NIST-P-256 \
    -keystoreFile "$DEBUG_KEYSTORE" \
    -keystorePwd "$DEBUG_KEYSTORE_PWD" \
    2>&1 || {
      echo "ERROR: Debug keystore generation failed"
      echo "Falling back to --skip-sign mode"
      SKIP_SIGN=true
    }

  if [ "$SKIP_SIGN" = "false" ]; then
    echo "✅ Debug keystore generated"

    # 5b: Generate debug app certificate
    $SIGN_TOOL generate-app-cert \
      -keyAlias "$DEBUG_KEY_ALIAS" \
      -issuerKeyAlias "oh-app-sign-debug-srv-ca-key-v1" \
      -issuer "C=CN,O=OpenHarmony,OU=OpenHarmony Community,CN=Application Debug Signature Service CA" \
      -subject "C=CN,O=openjiuwen,OU=OpenJiuwen,CN=JiuwenSwarm Debug" \
      -signAlg SHA256withECDSA \
      -keystoreFile "$SDK_P12" \
      -keystorePwd "123456" \
      -outFile "$DEBUG_APP_CERT" \
      -outForm cert \
      2>&1 || {
        echo "ERROR: Debug app certificate generation failed"
        echo "Falling back to --skip-sign mode"
        SKIP_SIGN=true
      }
  fi

  if [ "$SKIP_SIGN" = "false" ]; then
    echo "✅ Debug app certificate generated"

    # 5c: Create and sign debug profile
    # Modify the debug profile template for our bundle
    if [ -f "$SDK_PROFILE_TEMPLATE" ]; then
      # Copy and modify the template
      cp "$SDK_PROFILE_TEMPLATE" "$BUILD_DIR/UnsgnedDebugProfileTemplate-modified.json"
      # Replace bundle-name and add ACL permissions
      node -e "
        const fs = require('fs');
        const template = JSON.parse(fs.readFileSync('$BUILD_DIR/UnsgnedDebugProfileTemplate-modified.json', 'utf8'));
        template['bundle-name'] = '${BUNDLE_NAME}';
        // Add ACL permissions for native code execution
        if (template['acl']) {
          template['acl']['allow-apex'] = true;
        }
        fs.writeFileSync('$BUILD_DIR/UnsgnedDebugProfileTemplate-modified.json', JSON.stringify(template, null, 2));
      " 2>&1 || echo "⚠️ Profile template modification may have issues"
    else
      echo "⚠️ Debug profile template not found at: $SDK_PROFILE_TEMPLATE"
      echo "   Creating minimal profile..."
      # Create a minimal debug profile
      cat > "$BUILD_DIR/UnsgnedDebugProfileTemplate-modified.json" << 'EOF_PROFILE'
{
  "bundle-name": "com.openjiuwen.harmony_facade",
  "developer-id": "OpenHarmony",
  "development-cert": "",
  "distribution-certificate": "",
  "app-distribution-type": "debugging",
  "debug-info": {
    "device-ids": ["*"]
  },
  "feature-flags": {
    "allow-apex": true
  },
  "permissions": {
    "restricted-permissions": {}
  }
}
EOF_PROFILE
    fi

    $SIGN_TOOL sign-profile \
      -mode localSign \
      -keyAlias "oh-profile-key-v1" \
      -profileCertFile "$SDK_PROFILE_PEM" \
      -inFile "$BUILD_DIR/UnsgnedDebugProfileTemplate-modified.json" \
      -signAlg SHA256withECDSA \
      -keystoreFile "$SDK_P12" \
      -keystorePwd "123456" \
      -outFile "$DEBUG_PROFILE" \
      2>&1 || {
        echo "ERROR: Debug profile signing failed"
        echo "Falling back to --skip-sign mode"
        SKIP_SIGN=true
      }
  fi

  if [ "$SKIP_SIGN" = "false" ]; then
    echo "✅ Debug profile signed"

    # 5d: Sign the HAP
    $SIGN_TOOL sign-app \
      -mode localSign \
      -keyAlias "$DEBUG_KEY_ALIAS" \
      -appCertFile "$DEBUG_APP_CERT" \
      -profileFile "$DEBUG_PROFILE" \
      -inFile "$UNSIGNED_HAP" \
      -signAlg SHA256withECDSA \
      -keystoreFile "$DEBUG_KEYSTORE" \
      -keystorePwd "$DEBUG_KEYSTORE_PWD" \
      -outFile "$SIGNED_HAP" \
      -compatibleVersion 12 \
      2>&1 || {
        echo "ERROR: HAP signing failed"
        echo "Falling back to --skip-sign mode"
        SKIP_SIGN=true
      }
  fi

  if [ "$SKIP_SIGN" = "false" ]; then
    echo "✅ Signed HAP created: $SIGNED_HAP"
    echo "   HAP size: $(du -sh "$SIGNED_HAP" | cut -f1)"
  else
    echo "⚠️  Unsigned HAP only: $UNSIGNED_HAP"
    echo "   The HAP cannot be installed without signing."
  fi

  echo ""
fi

# ═══════════════════════════════════════════════
# Final Output
# ═══════════════════════════════════════════════
echo "============================================"
echo " 构建完成！"
echo "============================================"
echo ""

FINAL_HAP="$SIGNED_HAP"
if [ "$SKIP_SIGN" = "true" ]; then
  FINAL_HAP="$UNSIGNED_HAP"
fi

if [ -f "$FINAL_HAP" ]; then
  echo "✅ 构建产物: $FINAL_HAP"
  echo "   大小: $(du -sh "$FINAL_HAP" | cut -f1)"
  echo ""
  echo "安装方式:"
  echo "  方式 1 (hdc): hdc install $FINAL_HAP"
  echo "  方式 2 (手动): 将 .hap 复制到设备后通过 bm install 安装"
  echo ""
  if [ "$SKIP_ETS" = "true" ]; then
    echo "⚠️  注意: 此 HAP 缺少 ETS 编译产物，UI 将无法正常工作！"
    echo "   需要安装 DevEco Studio 进行完整 ETS 编译。"
  fi
else
  echo "❌ 构建失败 — 未生成 .hap 文件"
  echo "   请检查上方错误信息"
fi

echo ""
echo "构建目录: $BUILD_DIR"
echo "清理: rm -rf $BUILD_DIR"
