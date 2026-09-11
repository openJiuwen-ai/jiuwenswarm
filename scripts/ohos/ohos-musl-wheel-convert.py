#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convert foreign musllinux wheels into loadable HarmonyOS wheels.

HarmonyOS (musl + OHOS kernel) refuses to mmap-exec unsigned foreign
binaries and its dynamic loader does not search the interpreter's own
dependency chain when relocating dlopen'ed extensions. This script makes
a musllinux_*_aarch64 wheel installable & loadable on HarmonyOS by:

1. Renaming extension modules to this interpreter's suffix
   (``*.cpython-312-aarch64-linux-musl.so`` -> ``*.cpython-312.so``).
2. Congruence pre-fix: OHOS ``binary-sign-tool`` re-lays segment file
   offsets from the section table (compacting inter-segment gaps), which
   can break ``p_offset == p_vaddr (mod page)`` and segfault the loader.
   We extend the previous segment's last section so the signer's landing
   spot equals the segment's original (congruent) offset.
3. DT_NEEDED += libpython3.12.so.1.0 for CPython extension modules:
   foreign musl extensions assume the interpreter exports Py_* symbols
   globally; on OHOS they must link libpython explicitly. The string is
   embedded over the (expendable) GNU build-id note and a spare dynamic
   entry (DT_FINI / DT_FLAGS_1 / DT_FLAGS) is converted to DT_NEEDED.
4. Self-signing every .so with ``binary-sign-tool -selfSign 1``.
5. Rewheeling with ``harmonyos_aarch64`` platform tags and a regenerated
   RECORD.

Usage:
    python ohos-musl-wheel-convert.py INPUT.whl [-o OUTPUT.whl]
        [--sign-tool /data/service/hnp/bin/binary-sign-tool]
        [--libpython libpython3.12.so.1.0]

Foreign pure-Python wheels work unmodified; only the .so files matter.
Not supported: glibc (manylinux) wheels - their symbol sets/ABI differ.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

PAGE = 4096
MUSL_SO_RE = re.compile(r"^(.*\.cpython-\d+)-[a-z0-9_]+-linux-musl\.so$")
DEFAULT_SIGN_TOOL = "/data/service/hnp/bin/binary-sign-tool"


def die(msg: str) -> "None":
    print(f"[convert] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg: str) -> None:
    print(f"[convert] {msg}")


# ---------------------------------------------------------------- ELF utils
class Elf:
    def __init__(self, path: str):
        self.path = path
        with open(path, "rb") as f:
            self.data = bytearray(f.read())
        if self.data[:4] != b"\x7fELF" or self.data[4] != 2:
            raise ValueError("not an ELF64 file")
        e_phoff = struct.unpack_from("<Q", self.data, 0x20)[0]
        e_shoff = struct.unpack_from("<Q", self.data, 0x28)[0]
        self.e_phoff = e_phoff
        self.e_shoff = e_shoff
        self.e_phentsize = struct.unpack_from("<H", self.data, 0x36)[0]
        self.e_phnum = struct.unpack_from("<H", self.data, 0x38)[0]
        self.e_shentsize = struct.unpack_from("<H", self.data, 0x3A)[0]
        self.e_shnum = struct.unpack_from("<H", self.data, 0x3C)[0]

    def phdrs(self):
        out = []
        for i in range(self.e_phnum):
            o = self.e_phoff + i * self.e_phentsize
            p_type, p_flags = struct.unpack_from("<II", self.data, o)
            p_offset, p_vaddr, _pa, p_filesz, p_memsz, p_align = struct.unpack_from(
                "<QQQQQQ", self.data, o + 8
            )
            out.append(
                {
                    "idx": i,
                    "hdr_off": o,
                    "type": p_type,
                    "flags": p_flags,
                    "offset": p_offset,
                    "vaddr": p_vaddr,
                    "filesz": p_filesz,
                    "memsz": p_memsz,
                    "align": p_align,
                }
            )
        return out

    def loads(self):
        return sorted(
            (p for p in self.phdrs() if p["type"] == 1), key=lambda p: p["offset"]
        )

    def sections(self):
        out = []
        for i in range(self.e_shnum):
            o = self.e_shoff + i * self.e_shentsize
            sh_offset = struct.unpack_from("<Q", self.data, o + 0x18)[0]
            sh_size = struct.unpack_from("<Q", self.data, o + 0x20)[0]
            out.append({"idx": i, "hdr_off": o, "offset": sh_offset, "size": sh_size})
        return out

    def find_dynamic(self):
        for i in range(self.e_shnum):
            o = self.e_shoff + i * self.e_shentsize
            if struct.unpack_from("<I", self.data, o + 4)[0] == 6:  # SHT_DYNAMIC
                return struct.unpack_from("<Q", self.data, o + 0x18)[0], struct.unpack_from(
                    "<Q", self.data, o + 0x20
                )[0]
        return None, None

    def write(self, path: str | None = None):
        with open(path or self.path, "wb") as f:
            f.write(self.data)


# ------------------------------------------------------------- congruence
def fix_congruence(elf: Elf) -> list[str]:
    """Ensure the OHOS signer's compacted landing keeps page congruence.

    For every adjacent PT_LOAD pair (A, B): if align8(A's section-derived
    end) != B.p_offset, the signer will move B to align8(A_end). We extend
    A's last file-backed section (absorbing zero padding in the gap) so the
    landing becomes exactly B.p_offset, which the upstream layout already
    guarantees congruent with B.p_vaddr.
    """
    fixes = []
    loads = elf.loads()
    secs = elf.sections()
    for a, b in zip(loads, loads[1:]):
        a_end = a["offset"] + a["filesz"]
        landing = (a_end + 7) & ~7
        if landing == b["offset"]:
            continue  # contiguous: signer cannot move it
        # last section inside A
        in_a = [s for s in secs if a["offset"] <= s["offset"] < a_end and s["size"]]
        if not in_a:
            continue
        last = max(in_a, key=lambda s: s["offset"] + s["size"])
        last_end = last["offset"] + last["size"]
        landing = (last_end + 7) & ~7
        if landing == b["offset"]:
            continue
        if landing > b["offset"]:
            info(
                f"  !! {os.path.basename(elf.path)}: LOAD pair "
                f"({a['offset']:#x},{b['offset']:#x}) overlaps; leaving as-is"
            )
            continue
        # extend last section so align8(new_end) == b.offset
        delta = b["offset"] - landing
        gap = bytes(elf.data[last_end : b["offset"]])
        if gap != b"\x00" * len(gap):
            info(
                f"  !! {os.path.basename(elf.path)}: gap not zeros "
                f"({len(gap)}B @ {last_end:#x}); skipping"
            )
            continue
        struct.pack_into(
            "<Q", elf.data, last["hdr_off"] + 0x20, last["size"] + delta
        )
        # grow A's p_filesz/p_memsz to cover
        new_filesz = b["offset"] - a["offset"]
        struct.pack_into(
            "<QQ", elf.data, a["hdr_off"] + 0x20, new_filesz, max(a["memsz"], new_filesz)
        )
        fixes.append(
            f"extended section #{last['idx']} by {delta:#x} "
            f"(LOAD@{a['offset']:#x} -> landing {b['offset']:#x})"
        )
    return fixes


# ------------------------------------------------------- DT_NEEDED surgery
SACRIFICE_ORDER = (13, 0x6FFFFFFB, 30, 29)  # DT_FINI, DT_FLAGS_1, DT_FLAGS, DT_RPATH
# Linker-cruft weak undefined symbols whose names are never looked up at
# runtime; we reuse their .dynstr space for the libpython DT_NEEDED string.
PREFERRED_VICTIMS = (
    b"_ITM_deregisterTMCloneTable",
    b"_ITM_registerTMCloneTable",
    b"__gmon_start__",
)


def _locate_dynstr(elf: Elf):
    """Return (file_offset, size) of .dynstr via SHT_DYNSYM's sh_link."""
    for i in range(elf.e_shnum):
        o = elf.e_shoff + i * elf.e_shentsize
        if struct.unpack_from("<I", elf.data, o + 4)[0] != 11:  # SHT_DYNSYM
            continue
        sh_link = struct.unpack_from("<I", elf.data, o + 0x28)[0]
        lo = elf.e_shoff + sh_link * elf.e_shentsize
        return (
            struct.unpack_from("<Q", elf.data, lo + 0x18)[0],
            struct.unpack_from("<Q", elf.data, lo + 0x20)[0],
        )
    return None, None


def has_needed(elf: Elf, libname: str) -> bool:
    """True when DT_NEEDED already lists ``libname`` (idempotency guard)."""
    dyn_off, dyn_sz = elf.find_dynamic()
    str_off, _sz = _locate_dynstr(elf)
    if dyn_off is None or str_off is None:
        return False
    j = 0
    while j < dyn_sz:
        tag, val = struct.unpack_from("<qQ", elf.data, dyn_off + j)
        if tag == 0:
            break
        if tag == 1:
            end = elf.data.index(b"\x00", str_off + val)
            if bytes(elf.data[str_off + val : end]) == libname.encode():
                return True
        j += 16
    return False


def add_needed(elf: Elf, libname: str) -> str:
    """Append DT_NEEDED=<libname>.

    Strategy A (robust): overwrite the .dynstr name of an expendable weak
    undefined symbol (linker cruft) with ``libname`` and convert a spare
    dynamic entry (DT_FINI / DT_FLAGS_1 / ...) to DT_NEEDED pointing at it.
    The resulting offset is a normal in-range strtab offset, which the
    OHOS loader accepts. Strategy B (fallback): write the string over the
    GNU build-id note and use a wraparound strtab offset (may be rejected
    by the OHOS loader - kept only for files without usable victims).
    """
    need = libname.encode() + b"\x00"
    if has_needed(elf, libname):
        return f"DT_NEEDED={libname} already present (skipped)"
    dyn_off, dyn_sz = elf.find_dynamic()
    if dyn_off is None:
        return ""
    # --- locate dynsym/dynstr via section headers ---
    dynsym = dynstr = None
    for i in range(elf.e_shnum):
        o = elf.e_shoff + i * elf.e_shentsize
        sh_type = struct.unpack_from("<I", elf.data, o + 4)[0]
        sh_offset = struct.unpack_from("<Q", elf.data, o + 0x18)[0]
        sh_size = struct.unpack_from("<Q", elf.data, o + 0x20)[0]
        if sh_type == 11:  # SHT_DYNSYM
            sh_link = struct.unpack_from("<I", elf.data, o + 0x28)[0]
            dynsym = (sh_offset, sh_size)
            lo = elf.e_shoff + sh_link * elf.e_shentsize
            dynstr = (
                struct.unpack_from("<Q", elf.data, lo + 0x18)[0],
                struct.unpack_from("<Q", elf.data, lo + 0x20)[0],
            )
            break
    if dynsym is None or dynstr is None:
        return ""
    sym_off, sym_sz = dynsym
    str_off, str_sz = dynstr
    str_va = None
    sacrifice = None
    j = 0
    while j < dyn_sz:
        tag, val = struct.unpack_from("<qQ", elf.data, dyn_off + j)
        if tag == 0:
            break
        if tag == 5:
            str_va = val
        if sacrifice is None and tag in SACRIFICE_ORDER:
            sacrifice = (j, tag)
        j += 16
    if sacrifice is None:
        return ""
    # --- strategy A: find a victim weak/undefined symbol with a long name ---
    victim = None  # (st_name, old_name_len)
    entsz = 24
    n_syms = sym_sz // entsz
    for want in PREFERRED_VICTIMS:
        for k in range(n_syms):
            o = sym_off + k * entsz
            st_name, st_info, _st_other, st_shndx = struct.unpack_from(
                "<IBBH", elf.data, o
            )
            if st_shndx != 0 or (st_info >> 4) != 2:  # undefined + weak
                continue
            end = elf.data.index(b"\x00", str_off + st_name)
            nm = bytes(elf.data[str_off + st_name : end])
            if nm == want and len(nm) + 1 >= len(need):
                victim = (st_name, len(nm) + 1)
                break
        if victim:
            break
    if victim is None:  # any weak undefined with a long-enough name
        for k in range(n_syms):
            o = sym_off + k * entsz
            st_name, st_info, _st_other, st_shndx = struct.unpack_from(
                "<IBBH", elf.data, o
            )
            if st_shndx != 0 or (st_info >> 4) != 2:
                continue
            end = elf.data.index(b"\x00", str_off + st_name)
            if end - (str_off + st_name) + 1 >= len(need):
                victim = (st_name, end - (str_off + st_name) + 1)
                break
    j, old_tag = sacrifice
    if victim is not None:
        st_name, old_len = victim
        elf.data[str_off + st_name : str_off + st_name + len(need)] = need
        struct.pack_into("<qQ", elf.data, dyn_off + j, 1, st_name)
        tagname = {
            13: "DT_FINI",
            0x6FFFFFFB: "DT_FLAGS_1",
            30: "DT_FLAGS",
            29: "DT_RPATH",
        }
        return f"DT_NEEDED={libname} (strtab@{st_name:#x} via weak sym, overwrote {tagname.get(old_tag, hex(old_tag))})"
    # --- strategy B: build-id note + wraparound offset ---
    note = next((p for p in elf.phdrs() if p["type"] == 4), None)
    if note is None or note["filesz"] < len(need) or str_va is None:
        return "DT_NEEDED: skipped (no slot)"
    elf.data[note["offset"] : note["offset"] + len(need)] = need
    str_off_wrapped = (note["vaddr"] - str_va) & 0xFFFFFFFFFFFFFFFF
    struct.pack_into("<qQ", elf.data, dyn_off + j, 1, str_off_wrapped)
    return f"DT_NEEDED={libname} (note+wraparound @ {hex(str_off_wrapped)})"


# ---------------------------------------------------- DT_NEEDED renaming
def rename_needed(elf: Elf, old: str, new: str) -> int:
    """Rewrite DT_NEEDED entries named ``old`` to ``new`` (must be shorter).

    Some musl build environments (Alpine/cibuildwheel hashed sonames) record
    names like ``libstdc++-1f1a71be.so.6.0.33``; we retarget them to plain
    names so a normally-named library satisfies them.
    """
    if len(new) > len(old):
        raise ValueError(f"rename target longer than source: {old} -> {new}")
    dyn_off, dyn_sz = elf.find_dynamic()
    str_off, _str_sz = _locate_dynstr(elf)
    if dyn_off is None or str_off is None:
        return 0
    old_b, new_b = old.encode(), (new + "\0").encode()
    n = 0
    j = 0
    while j < dyn_sz:
        tag, val = struct.unpack_from("<qQ", elf.data, dyn_off + j)
        if tag == 0:
            break
        if tag == 1:
            end = elf.data.index(b"\x00", str_off + val)
            name = bytes(elf.data[str_off + val : end])
            if name == old_b:
                elf.data[str_off + val : str_off + val + len(new_b)] = new_b
                n += 1
        j += 16
    return n


# ----------------------------------------------------------------- signing
# Signing is sub-second in practice; this is only a hang guard (e.g. the
# tool deadlocking or waiting for interactive input).
SIGN_TIMEOUT = 60  # seconds


def sign(path: str, sign_tool: str) -> bool:
    out = path + ".signed"
    try:
        r = subprocess.run(
            [
                sign_tool, "sign", "-selfSign", "1",
                "-inFile", path, "-outFile", out,
                "-signAlg", "SHA256withECDSA",
                "-keyAlias", "default",
            ],
            capture_output=True, text=True, timeout=SIGN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        if os.path.exists(out):
            os.remove(out)  # partial output left by the killed tool
        info(f"  !! sign timed out for {path} (>{SIGN_TIMEOUT}s)")
        return False
    except OSError as exc:
        info(f"  !! sign could not run {sign_tool}: {exc}")
        return False
    if r.returncode != 0 or not os.path.exists(out):
        info(f"  !! sign failed for {path}: {r.stdout.strip()} {r.stderr.strip()}")
        return False
    os.replace(out, path)
    return True


def verify_signed_layout(path: str) -> bool:
    """Post-check: every LOAD must satisfy p_offset == p_vaddr (mod page)."""
    try:
        elf = Elf(path)
    except ValueError:
        return True  # not ELF; nothing to check
    for p in elf.loads():
        if p["offset"] % PAGE != p["vaddr"] % PAGE:
            return False
    return True


# -------------------------------------------------------------- wheel build
def new_platform_tag(old_tag: str) -> str:
    parts = old_tag.split("-")
    # replace platform part of the final component
    last = parts[-1]
    last = re.sub(r"musllinux_\d+_\d+_aarch64", "harmonyos_aarch64", last)
    parts[-1] = last
    return "-".join(parts)


def record_hash(path: str) -> str:
    with open(path, "rb") as f:
        d = f.read()
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(d).digest()).rstrip(b"=").decode()


def convert_wheel(src: str, dst: str, sign_tool: str, libpython: str, renames=()) -> None:
    name = os.path.basename(src)
    m = re.match(r"^(?P<name>.+)-(?P<ver>[^-]+)-(?P<tag>[^-]+-[^-]+-[^-]+)\.whl$", name)
    if not m:
        die(f"cannot parse wheel filename: {name}")
    new_tag = new_platform_tag(m.group("tag"))
    out_name = f"{m.group('name')}-{m.group('ver')}-{new_tag}.whl"
    out_path = dst or os.path.join(os.path.dirname(os.path.abspath(src)), out_name)

    tmp = tempfile.mkdtemp(prefix="musl-convert-")
    try:
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
        dist_info = None
        for d in os.listdir(tmp):
            if d.endswith(".dist-info"):
                dist_info = os.path.join(tmp, d)
        if not dist_info:
            die("wheel has no dist-info")

        so_files = []
        for root, _dirs, files in os.walk(tmp):
            for fn in files:
                # match foo.so, foo.so.6.0.33 (auditwheel bundles use versioned
                # sonames like libstdc++-1f1a71be.so.6.0.33 in <pkg>.libs/)
                if re.search(r"\.so(\.\d+)*$", fn):
                    so_files.append(os.path.join(root, fn))
        info(f"{len(so_files)} shared objects to process")
        n_ok = 0
        for so in sorted(so_files):
            rel = os.path.relpath(so, tmp)
            # 1. rename musl extension suffix
            mm = MUSL_SO_RE.match(fn := os.path.basename(so))
            if mm:
                new = os.path.join(os.path.dirname(so), mm.group(1) + ".so")
                os.rename(so, new)
                so = new
                rel = os.path.relpath(so, tmp)
            elf = Elf(so)
            # 2. congruence pre-fix
            fixes = fix_congruence(elf)
            # 2b. rename hashed DT_NEEDED entries
            for old, new in renames:
                n = rename_needed(elf, old, new)
                if n:
                    fixes.append(f"renamed {old} -> {new} ({n} refs)")
            # 3. DT_NEEDED libpython for extension modules
            if re.search(r"\.cpython-\d+\.so$", os.path.basename(so)):
                fixes.append(add_needed(elf, libpython) or "DT_NEEDED: skipped (no slot)")
            elf.write()
            # 4. sign (in place)
            if not sign(so, sign_tool):
                die(f"signing failed: {rel}")
            if not verify_signed_layout(so):
                die(f"post-sign congruence check failed: {rel}")
            n_ok += 1
            if fixes:
                for f in fixes:
                    if f:
                        info(f"  {rel}: {f}")
        info(f"processed {n_ok}/{len(so_files)} .so files")

        # 5. retag WHEEL + rename dist-info if tag in name
        # (WHEEL/RECORD are UTF-8 text per PEP 427; be explicit about it so
        # the conversion does not depend on the platform default encoding)
        wheel_p = os.path.join(dist_info, "WHEEL")
        with open(wheel_p, encoding="utf-8") as f:
            wm = f.read()
        wm = re.sub(r"Tag: .*musllinux_\d+_\d+_aarch64", f"Tag: {new_tag}", wm)
        with open(wheel_p, "w", encoding="utf-8") as f:
            f.write(wm)

        # 6. regenerate RECORD
        record = os.path.join(dist_info, "RECORD")
        lines = []
        all_files = []
        for root, _dirs, files in os.walk(tmp):
            for fn in files:
                p = os.path.join(root, fn)
                r = os.path.relpath(p, tmp).replace(os.sep, "/")
                all_files.append((r, p))
        all_files.sort()
        for r, p in all_files:
            if r.endswith("RECORD"):
                lines.append(f"{r},,")
            else:
                lines.append(f"{r},{record_hash(p)},{os.path.getsize(p)}")
        with open(record, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        # 7. rezip
        if os.path.exists(out_path):
            os.remove(out_path)
        order = [r for r, _ in all_files if not r.endswith("RECORD")]
        order += [r for r, _ in all_files if r.endswith("RECORD")]
        by_rel = {r: p for r, p in all_files}
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for r in order:
                zf.write(by_rel[r], r)
        info(f"written: {out_path}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def convert_single_so(path: str, sign_tool: str, renames=()) -> None:
    """Congruence-fix, optionally rename DT_NEEDED, and self-sign one .so."""
    elf = Elf(path)
    fixes = fix_congruence(elf)
    for old, new in renames:
        n = rename_needed(elf, old, new)
        if n:
            fixes.append(f"renamed {old} -> {new} ({n} refs)")
    elf.write()
    if not sign(path, sign_tool):
        die(f"signing failed: {path}")
    if not verify_signed_layout(path):
        die(f"post-sign congruence check failed: {path}")
    info(f"signed: {path}")
    for f in fixes:
        if f:
            info(f"  {f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wheel", nargs="?")
    ap.add_argument("--so", help="process a single shared object (sign in place)")
    ap.add_argument("-o", "--output")
    ap.add_argument("--sign-tool", default=DEFAULT_SIGN_TOOL)
    ap.add_argument("--libpython", default="libpython3.12.so.1.0")
    ap.add_argument(
        "--rename-needed",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="rewrite DT_NEEDED entries from OLD to NEW (NEW must be shorter)",
    )
    args = ap.parse_args()
    if not os.path.exists(args.sign_tool):
        die(f"sign tool not found: {args.sign_tool}")
    renames = []
    for spec in args.rename_needed:
        if "=" not in spec:
            die(f"--rename-needed expects OLD=NEW, got: {spec}")
        old, new = spec.split("=", 1)
        renames.append((old, new))
    if args.so:
        if args.wheel:
            die("pass either a wheel or --so, not both")
        convert_single_so(args.so, args.sign_tool, renames)
        return
    if not args.wheel:
        die("no input: pass a wheel path or --so FILE")
    convert_wheel(args.wheel, args.output, args.sign_tool, args.libpython, renames)


if __name__ == "__main__":
    main()
