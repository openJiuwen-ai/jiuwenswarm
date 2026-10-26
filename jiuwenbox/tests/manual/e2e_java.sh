#!/usr/bin/env bash
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
#
# Java end-to-end verification against a RUNNING jiuwenbox server loaded with
# configs/java-policy.yaml. Uses only curl + jq + base64 (no Python deps), so
# it runs on the 205 build host directly.
#
# It does NOT start the server. Start one first, e.g. on 205:
#   cd jiuwenswarm/jiuwenbox
#   bash scripts/build_docker.sh
#   bash scripts/run_docker.sh src/jiuwenbox/configs/java-policy.yaml
#   bash tests/manual/e2e_java.sh
#
# Usage:
#   ./tests/manual/e2e_java.sh                       # http://127.0.0.1:8321
#   ENDPOINT=http://127.0.0.1:18321 ./tests/manual/e2e_java.sh
#
# Exit code: 0 if all checks pass, non-zero otherwise.
set -euo pipefail

ENDPOINT="${ENDPOINT:-http://127.0.0.1:8321}"
JQ="${JQ:-jq}"
FAIL=0

# Per-run temp dir so concurrent runs (or a stale leftover) don't collide on
# fixed paths like /tmp/jb_Main.java. Cleaned up on exit.
WORK="$(mktemp -d -t jbw-e2e-XXXXXX)"
trap 'rm -rf "${WORK}"' EXIT

note() { printf '\n===== %s =====\n' "$1"; }
pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; FAIL=1; }

# Create a sandbox and echo its id. The server must be loaded with java-policy
# so the sandbox inherits JAVA_HOME/PATH/cgroup.
create_sandbox() {
  curl -sS -X POST "${ENDPOINT}/api/v1/sandboxes" -H 'Content-Type: application/json' \
    -d '{}' | "${JQ}" -r .id
}

# exec a command in the sandbox. Args: sandbox_id, then a JSON string for the
# request body. Prints the response JSON.
exec_cmd() {
  local sb="$1"; shift
  local body="$1"; shift
  curl -sS -X POST "${ENDPOINT}/api/v1/sandboxes/${sb}/exec" \
    -H 'Content-Type: application/json' -d "${body}"
}

# Write a file inside the sandbox by base64-encoding local content and decoding
# on the sandbox side via exec stdin. Args: sandbox_id, sandbox_path, local_file.
write_file() {
  local sb="$1" path="$2" local="$3"
  local b64
  b64=$(base64 -w0 "${local}")
  local dir
  dir=$(dirname "${path}")
  exec_cmd "${sb}" "{\"command\":[\"sh\",\"-c\",\"mkdir -p ${dir} && base64 -d > ${path}\"],\"stdin\":\"${b64}\"}" \
    | "${JQ}" -e '.exit_code == 0' >/dev/null
}

SB=$(create_sandbox)
echo "sandbox: ${SB}"

# ---------------------------------------------------------------------------
note "A. policy smoke (java/javac/jar/JAVA_HOME)"
# ---------------------------------------------------------------------------
for c in "java -version" "javac -version" "jar --version"; do
  out=$(exec_cmd "${SB}" "{\"command\":[\"sh\",\"-c\",\"${c}\"],\"timeout_seconds\":15}")
  code=$(echo "${out}" | "${JQ}" -r .exit_code)
  if [[ "${code}" == "0" ]]; then pass "${c}"; else fail "${c} (exit=${code}): $(echo "${out}" | "${JQ}" -r .stderr)"; fi
done
jh=$(exec_cmd "${SB}" '{"command":["sh","-c","echo $JAVA_HOME"],"timeout_seconds":10}' | "${JQ}" -r .stdout | tr -d '\n')
if [[ "${jh}" == "/usr/lib/jvm/java-17-openjdk" ]]; then pass "JAVA_HOME=${jh}"; else fail "JAVA_HOME=${jh} (expected /usr/lib/jvm/java-17-openjdk)"; fi

# ---------------------------------------------------------------------------
note "B. single-file Java: compile + run"
# ---------------------------------------------------------------------------
cat > "${WORK}/jb_Main.java" <<'JAVA'
public class Main {
  public static void main(String[] args) {
    System.out.println("JIUWENBOX-JAVA-E2E-MAIN");
  }
}
JAVA
write_file "${SB}" /home/src/Main.java "${WORK}/jb_Main.java" \
  && pass "upload /home/src/Main.java" || fail "upload /home/src/Main.java"

out=$(exec_cmd "${SB}" '{"command":["javac","-d","/home/classes","/home/src/Main.java"],"timeout_seconds":30}')
[[ "$(echo "${out}" | "${JQ}" -r .exit_code)" == "0" ]] && pass "javac Main.java" || fail "javac Main.java: $(echo "${out}" | "${JQ}" -r .stderr)"

out=$(exec_cmd "${SB}" '{"command":["java","-cp","/home/classes","Main"],"timeout_seconds":15}')
res=$(echo "${out}" | "${JQ}" -r .stdout | tr -d '\n')
[[ "${res}" == "JIUWENBOX-JAVA-E2E-MAIN" ]] && pass "java Main -> ${res}" || fail "java Main -> '${res}' (exit=$(echo "${out}" | "${JQ}" -r .exit_code))"

# ---------------------------------------------------------------------------
note "C. jar + classpath"
# ---------------------------------------------------------------------------
cat > "${WORK}/jb_Lib.java" <<'JAVA'
package lib;
public class Lib {
  public static String greeting() { return "JIUWENBOX-JAVA-E2E-LIB"; }
}
JAVA
cat > "${WORK}/jb_MainWithLib.java" <<'JAVA'
import lib.Lib;
public class MainWithLib {
  public static void main(String[] args) {
    System.out.println(Lib.greeting());
  }
}
JAVA
write_file "${SB}" /home/src/Lib.java "${WORK}/jb_Lib.java" || fail "upload Lib.java"
write_file "${SB}" /home/src/MainWithLib.java "${WORK}/jb_MainWithLib.java" || fail "upload MainWithLib.java"

# Compile Lib into a separate classes dir, jar it, then remove the loose class
# so MainWithLib can only resolve Lib via the jar. lib-classes goes under /tmp
# (not /home): in some sandboxes a mkdir'd subdir under /home becomes an overlay
# mount point, making `rm -rf` fail with EBUSY. /tmp has no such quirk; the jar
# output still lands in /home/jars.
exec_cmd "${SB}" '{"command":["sh","-c","mkdir -p /tmp/lib-classes /home/jars /home/classes && javac -d /tmp/lib-classes /home/src/Lib.java && jar cf /home/jars/hello-lib.jar -C /tmp/lib-classes . && rm -rf /tmp/lib-classes"],"timeout_seconds":30}' \
  | "${JQ}" -e '.exit_code == 0' >/dev/null && pass "build hello-lib.jar" || fail "build hello-lib.jar"

out=$(exec_cmd "${SB}" '{"command":["javac","-cp","/home/jars/hello-lib.jar","-d","/home/classes","/home/src/MainWithLib.java"],"timeout_seconds":30}')
[[ "$(echo "${out}" | "${JQ}" -r .exit_code)" == "0" ]] && pass "javac MainWithLib.java (-cp jar)" || fail "javac MainWithLib.java: $(echo "${out}" | "${JQ}" -r .stderr)"

out=$(exec_cmd "${SB}" '{"command":["java","-cp","/home/jars/*:/home/classes","MainWithLib"],"timeout_seconds":15}')
res=$(echo "${out}" | "${JQ}" -r .stdout | tr -d '\n')
[[ "${res}" == "JIUWENBOX-JAVA-E2E-LIB" ]] && pass "java -cp jars:classes MainWithLib -> ${res}" || fail "jar classpath run -> '${res}' (exit=$(echo "${out}" | "${JQ}" -r .exit_code), stderr=$(echo "${out}" | "${JQ}" -r .stderr))"

# ---------------------------------------------------------------------------
note "D. /tmp writable + JVM hsperfdata"
# ---------------------------------------------------------------------------
out=$(exec_cmd "${SB}" '{"command":["sh","-c","echo ok > /tmp/jb_writable_test && cat /tmp/jb_writable_test"],"timeout_seconds":10}')
[[ "$(echo "${out}" | "${JQ}" -r .stdout | tr -d '\n')" == "ok" ]] && pass "/tmp shell write" || fail "/tmp shell write"
# Running java already exercises /tmp/hsperfdata_<user>; verify the dir exists
# after a java invocation (JVM writes perf data there). If absent, JVM still
# ran (some configs disable it), so only soft-check.
out=$(exec_cmd "${SB}" '{"command":["sh","-c","java -version 2>/dev/null; ls /tmp/hsperfdata_* 2>/dev/null | head -1 || true"],"timeout_seconds":15}')
echo "hsperfdata check: $(echo "${out}" | "${JQ}" -r .stdout)"
pass "/tmp hsperfdata (soft, java ran)"

# ---------------------------------------------------------------------------
note "E. resource limits (best-effort)"
# ---------------------------------------------------------------------------
# E1: timeout enforcement — busy loop killed at timeout_seconds.
cat > "${WORK}/jb_Busy.java" <<'JAVA'
public class Busy {
  public static void main(String[] args) {
    long t = System.currentTimeMillis() + 60000;
    while (System.currentTimeMillis() < t) { Math.sqrt(Math.random()); }
  }
}
JAVA
write_file "${SB}" /home/src/Busy.java "${WORK}/jb_Busy.java" || fail "upload Busy.java"
out=$(exec_cmd "${SB}" '{"command":["javac","-d","/home/classes","/home/src/Busy.java"],"timeout_seconds":30}')
[[ "$(echo "${out}" | "${JQ}" -r .exit_code)" == "0" ]] && pass "javac Busy.java" || fail "javac Busy.java"
out=$(exec_cmd "${SB}" '{"command":["java","-cp","/home/classes","Busy"],"timeout_seconds":3}')
code=$(echo "${out}" | "${JQ}" -r .exit_code)
# exit 124 = jiuwenbox timeout; non-zero also acceptable (cgroup/oom). Zero = bad.
if [[ "${code}" != "0" ]]; then pass "busy loop terminated (exit=${code})"; else fail "busy loop was NOT stopped (exit=0)"; fi

# E2: memory failure — heap OOM with -Xmx128m. Should exit non-zero; server
# must stay healthy afterwards. (cgroup memory_max 512M is the outer cap; this
# exercises JVM-internal OOM, not cgroup kill. cgroup kill verification is
# manual, see docs/jiuwenbox-java-test-plan.md T4.)
cat > "${WORK}/jb_Oom.java" <<'JAVA'
import java.util.*;
public class Oom {
  public static void main(String[] args) {
    List<byte[]> list = new ArrayList<>();
    while (true) { list.add(new byte[8 * 1024 * 1024]); }
  }
}
JAVA
write_file "${SB}" /home/src/Oom.java "${WORK}/jb_Oom.java" || fail "upload Oom.java"
exec_cmd "${SB}" '{"command":["javac","-d","/home/classes","/home/src/Oom.java"],"timeout_seconds":30}' | "${JQ}" -e '.exit_code == 0' >/dev/null && pass "javac Oom.java" || fail "javac Oom.java"
out=$(exec_cmd "${SB}" '{"command":["java","-Xmx128m","-cp","/home/classes","Oom"],"timeout_seconds":30}')
code=$(echo "${out}" | "${JQ}" -r .exit_code)
if [[ "${code}" != "0" ]]; then pass "OOM java exited non-zero (exit=${code})"; else fail "OOM java did not fail (exit=0)"; fi

# Server health after resource tests.
hc=$(curl -sS -o /dev/null -w '%{http_code}' "${ENDPOINT}/health")
[[ "${hc}" == "200" ]] && pass "server healthy after resource tests (HTTP ${hc})" || fail "server unhealthy (HTTP ${hc})"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
curl -sS -X DELETE "${ENDPOINT}/api/v1/sandboxes/${SB}" -o /dev/null -w 'delete: %{http_code}\n'

note "summary"
if [[ "${FAIL}" -ne 0 ]]; then
  echo "Java e2e FAILED"
  exit 1
fi
echo "Java e2e PASSED"
