/*
 * Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
 *
 * Windows user-mode soft delete. Loaded once into an agent or sandbox
 * process; the CreateProcessInternalW hook then loads it into children.
 * MSYS/Cygwin images such as Git Bash are not injected: any extra DLL
 * makes their fork wait forever. They are started under a debug loop
 * that loads this DLL into Windows descendants before main runs.
 * Console hosts (conhost.exe) are left alone: injecting them crashes
 * the host and the parent exits with STATUS_DLL_INIT_FAILED.
 *
 * Delete calls are turned into a recycle-bin move, or a same-volume rename
 * into the archive directory. A sandbox process turns a relative name into a
 * full path first, then asks box-server to recycle it as the logged-in user.
 * box-server does not share this process's current directory. The file stays
 * when that request fails. The original delete is not called after a
 * successful soft delete. A move that fails leaves the file in place and
 * returns STATUS_ACCESS_DENIED.
 */
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0A00
#endif
#define WIN32_LEAN_AND_MEAN
#define _CRT_SECURE_NO_WARNINGS

#include <windows.h>
#include <shellapi.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#if !defined(_MSC_VER)
static void soft_wcopy(wchar_t *dst, size_t cap, const wchar_t *src) {
    size_t i = 0;
    if (dst == NULL || cap == 0) {
        return;
    }
    if (src == NULL) {
        dst[0] = L'\0';
        return;
    }
    while (i + 1 < cap && src[i] != L'\0') {
        dst[i] = src[i];
        i++;
    }
    dst[i] = L'\0';
}

static void soft_wcat(wchar_t *dst, size_t cap, const wchar_t *src) {
    size_t used = 0;
    if (dst == NULL || cap == 0) {
        return;
    }
    while (used < cap && dst[used] != L'\0') {
        used++;
    }
    if (used >= cap) {
        dst[cap - 1] = L'\0';
        return;
    }
    soft_wcopy(dst + used, cap - used, src);
}

static int soft_swprintf(wchar_t *dst, size_t cap, const wchar_t *fmt, ...) {
    va_list args;
    int wrote;
    va_start(args, fmt);
    wrote = vswprintf(dst, cap, fmt, args);
    va_end(args);
    if (wrote < 0 && dst != NULL && cap > 0) {
        dst[0] = L'\0';
    }
    return wrote;
}

#define wcscpy_s(dst, cap, src) soft_wcopy((dst), (cap), (src))
#define wcsncpy_s(dst, cap, src, trunc) soft_wcopy((dst), (cap), (src))
#define wcscat_s(dst, cap, src) soft_wcat((dst), (cap), (src))
#define swprintf_s soft_swprintf
#endif

static int memory_ok(const void *ptr, size_t bytes, int writable) {
    const uint8_t *cursor = (const uint8_t *)ptr;
    const uint8_t *end;
    if (ptr == NULL || bytes == 0) {
        return 0;
    }
    end = cursor + bytes;
    while (cursor < end) {
        MEMORY_BASIC_INFORMATION info;
        DWORD protect;
        uintptr_t region_end;
        if (VirtualQuery(cursor, &info, sizeof info) == 0 || info.State != MEM_COMMIT) {
            return 0;
        }
        if ((info.Protect & PAGE_GUARD) != 0) {
            return 0;
        }
        protect = info.Protect & 0xFF;
        if (writable) {
            if (protect != PAGE_READWRITE && protect != PAGE_WRITECOPY
                && protect != PAGE_EXECUTE_READWRITE && protect != PAGE_EXECUTE_WRITECOPY) {
                return 0;
            }
        } else if (protect != PAGE_READONLY && protect != PAGE_READWRITE && protect != PAGE_WRITECOPY
                   && protect != PAGE_EXECUTE_READ && protect != PAGE_EXECUTE_READWRITE
                   && protect != PAGE_EXECUTE_WRITECOPY) {
            return 0;
        }
        region_end = (uintptr_t)info.BaseAddress + info.RegionSize;
        if (region_end <= (uintptr_t)cursor) {
            return 0;
        }
        cursor = (const uint8_t *)region_end;
    }
    return 1;
}

#define STATUS_SUCCESS ((NTSTATUS)0x00000000L)
#define STATUS_ACCESS_DENIED ((NTSTATUS)0xC0000022L)
#define STATUS_OBJECT_NAME_NOT_FOUND ((NTSTATUS)0xC0000034L)
#define FILE_DISPOSITION_INFORMATION 13UL
#define FILE_DISPOSITION_INFORMATION_EX 64UL
#define FILE_RENAME_INFORMATION 10UL
#define FILE_RENAME_INFORMATION_EX 65UL
#define FILE_RENAME_REPLACE_IF_EXISTS 0x00000001UL

#define ENV_ENABLED L"JIUWENBOX_SOFT_DELETE"
#define ENV_ARCHIVE L"JIUWENBOX_SOFT_DELETE_ARCHIVE"
#define ENV_DLL L"JIUWENBOX_SOFT_DELETE_DLL"
#define ENV_RECYCLE_PORT L"JIUWENBOX_SOFT_DELETE_RECYCLE_PORT"
#define ENV_RECYCLE_TOKEN L"JIUWENBOX_SOFT_DELETE_RECYCLE_TOKEN"
#define RECYCLE_TOKEN_MAX 256u
#define RECYCLE_PATH_MAX (48u * 1024u)
#define PENDING_MAX 1024
#define PATH_CAP 32768
#define INFO_COPY_MAX (20u + (PATH_CAP * (ULONG)sizeof(wchar_t)))
#define STOLEN_MAX 32
#define HOOK_MAX 12

typedef LONG NTSTATUS;
typedef ULONG ACCESS_MASK;

typedef struct _UNICODE_STRING_ {
    USHORT Length;
    USHORT MaximumLength;
    PWSTR Buffer;
} UNICODE_STRING_;

typedef struct _OBJECT_ATTRIBUTES_ {
    ULONG Length;
    HANDLE RootDirectory;
    UNICODE_STRING_ *ObjectName;
    ULONG Attributes;
    PVOID SecurityDescriptor;
    PVOID SecurityQualityOfService;
} OBJECT_ATTRIBUTES_;

typedef struct _IO_STATUS_BLOCK_ {
    union {
        NTSTATUS Status;
        PVOID Pointer;
    } StatusUnion;
    ULONG_PTR Information;
} IO_STATUS_BLOCK_;

typedef struct _Hook {
    void *target;
    void *relay;
    void *trampoline;
    uint8_t saved[STOLEN_MAX];
    int stolen;
    int installed;
} Hook;

typedef NTSTATUS(NTAPI *FnNtClose)(HANDLE);
typedef NTSTATUS(NTAPI *FnNtDeleteFile)(OBJECT_ATTRIBUTES_ *);
typedef NTSTATUS(NTAPI *FnNtSetInformationFile)(
    HANDLE, IO_STATUS_BLOCK_ *, PVOID, ULONG, ULONG);
typedef NTSTATUS(NTAPI *FnNtCreateFile)(
    HANDLE *, ACCESS_MASK, OBJECT_ATTRIBUTES_ *, IO_STATUS_BLOCK_ *,
    LARGE_INTEGER *, ULONG, ULONG, ULONG, ULONG, PVOID, ULONG);
typedef NTSTATUS(NTAPI *FnNtOpenFile)(
    HANDLE *, ACCESS_MASK, OBJECT_ATTRIBUTES_ *, IO_STATUS_BLOCK_ *,
    ULONG, ULONG);
typedef BOOL(WINAPI *FnCreateProcessInternalW)(
    HANDLE, LPCWSTR, LPWSTR, LPSECURITY_ATTRIBUTES, LPSECURITY_ATTRIBUTES,
    BOOL, DWORD, LPVOID, LPCWSTR, LPSTARTUPINFOW, LPPROCESS_INFORMATION,
    HANDLE *);

static HMODULE g_module;
static CRITICAL_SECTION g_lock;
static int g_lock_ready;
#if defined(__GNUC__)
#define SOFT_TLS __thread
#define SOFT_EXPORT __attribute__((dllexport))
#else
#define SOFT_TLS __declspec(thread)
#define SOFT_EXPORT __declspec(dllexport)
#endif
static SOFT_TLS int t_bypass;

static Hook g_hooks[HOOK_MAX];
static int g_hook_count;
static FnNtClose g_nt_close;
static FnNtDeleteFile g_nt_delete_file;
static FnNtSetInformationFile g_nt_set_info;
static FnNtCreateFile g_nt_create_file;
static FnNtOpenFile g_nt_open_file;
static FnCreateProcessInternalW g_create_process;
static BOOL(WINAPI *g_delete_file)(LPCWSTR);
static BOOL(WINAPI *g_remove_directory)(LPCWSTR);

static HANDLE g_pending_handle[PENDING_MAX];
static wchar_t *g_pending_path[PENDING_MAX];

static void log_line(const wchar_t *text);

/* -------------------------------------------------------------------------- */
/* x64 length decoder. Enough for ntdll syscall stubs and typical prologues. */
/* A relative instruction returns -2 so the caller refuses to steal it.      */
/* -------------------------------------------------------------------------- */

static int modrm_len(const uint8_t *code, int avail, int *reg_out, int *rip_rel) {
    int mod;
    int rm;
    int n;
    if (avail < 1) {
        return -1;
    }
    mod = code[0] >> 6;
    if (reg_out != NULL) {
        *reg_out = (code[0] >> 3) & 7;
    }
    rm = code[0] & 7;
    n = 1;
    if (rip_rel != NULL) {
        *rip_rel = 0;
    }
    if (mod != 3 && rm == 4) {
        int base;
        if (avail < 2) {
            return -1;
        }
        base = code[1] & 7;
        n = 2;
        if (mod == 0 && base == 5) {
            n += 4;
        }
    } else if (mod == 0 && rm == 5) {
        n += 4;
        if (rip_rel != NULL) {
            *rip_rel = 1;
        }
    }
    if (mod == 1) {
        n += 1;
    } else if (mod == 2) {
        n += 4;
    }
    if (n > avail) {
        return -1;
    }
    return n;
}

static int decode_one(const uint8_t *code, int avail, int *is_rel) {
    int i = 0;
    int rex_w = 0;
    int operand16 = 0;
    uint8_t op;
    int reg = 0;
    int rip = 0;
    int mlen;
    *is_rel = 0;
    if (avail < 1) {
        return -1;
    }
    for (;;) {
        uint8_t pref;
        if (i >= avail) {
            return -1;
        }
        pref = code[i];
        if (pref == 0x66 || pref == 0x67 || pref == 0xF0 || pref == 0xF2 || pref == 0xF3
            || pref == 0x2E || pref == 0x36 || pref == 0x3E || pref == 0x26
            || pref == 0x64 || pref == 0x65 || (pref >= 0x40 && pref <= 0x4F)) {
            if (pref == 0x66) {
                operand16 = 1;
            }
            if (pref >= 0x40 && pref <= 0x4F && (pref & 0x08) != 0) {
                rex_w = 1;
            }
            i++;
            continue;
        }
        break;
    }
    op = code[i++];
    if (op >= 0x50 && op <= 0x5F) {
        return i;
    }
    if (op == 0x90 || op == 0xC3 || op == 0xCC) {
        return i;
    }
    if (op == 0xCD) {
        return (i + 1 <= avail) ? i + 1 : -1;
    }
    if (op == 0xE8 || op == 0xE9) {
        *is_rel = 1;
        return -2;
    }
    if (op == 0xEB || (op >= 0x70 && op <= 0x7F)) {
        *is_rel = 1;
        return -2;
    }
    if (op >= 0xB8 && op <= 0xBF) {
        int imm = rex_w ? 8 : 4;
        return (i + imm <= avail) ? i + imm : -1;
    }
    if (op == 0x0F) {
        uint8_t op2;
        if (i >= avail) {
            return -1;
        }
        op2 = code[i++];
        if (op2 == 0x05) {
            return i;
        }
        if (op2 == 0x1F) {
            mlen = modrm_len(code + i, avail - i, NULL, &rip);
            if (mlen < 0) {
                return -1;
            }
            if (rip) {
                *is_rel = 1;
                return -2;
            }
            return i + mlen;
        }
        if (op2 >= 0x80 && op2 <= 0x8F) {
            *is_rel = 1;
            return -2;
        }
        return -1;
    }

    if (op == 0x83 || op == 0x80 || op == 0xC0 || op == 0xC1 || op == 0x6B) {
        mlen = modrm_len(code + i, avail - i, NULL, &rip);
        if (mlen < 0) {
            return -1;
        }
        if (rip) {
            *is_rel = 1;
            return -2;
        }
        if (i + mlen + 1 > avail) {
            return -1;
        }
        return i + mlen + 1;
    }
    if (op == 0x81 || op == 0x69 || op == 0xC7) {
        int imm = operand16 ? 2 : 4;
        mlen = modrm_len(code + i, avail - i, NULL, &rip);
        if (mlen < 0) {
            return -1;
        }
        if (rip) {
            *is_rel = 1;
            return -2;
        }
        if (i + mlen + imm > avail) {
            return -1;
        }
        return i + mlen + imm;
    }
    if (op == 0xC6) {
        mlen = modrm_len(code + i, avail - i, NULL, &rip);
        if (mlen < 0) {
            return -1;
        }
        if (rip) {
            *is_rel = 1;
            return -2;
        }
        if (i + mlen + 1 > avail) {
            return -1;
        }
        return i + mlen + 1;
    }
    if (op == 0xF6 || op == 0xF7) {
        int imm;
        mlen = modrm_len(code + i, avail - i, &reg, &rip);
        if (mlen < 0) {
            return -1;
        }
        if (rip) {
            *is_rel = 1;
            return -2;
        }
        imm = 0;
        if (reg <= 1) {
            imm = (op == 0xF6) ? 1 : (operand16 ? 2 : 4);
        }
        if (i + mlen + imm > avail) {
            return -1;
        }
        return i + mlen + imm;
    }
    if (op == 0x01 || op == 0x03 || op == 0x09 || op == 0x0B || op == 0x21 || op == 0x23
        || op == 0x29 || op == 0x2B || op == 0x31 || op == 0x33 || op == 0x39 || op == 0x3B
        || op == 0x85 || op == 0x87 || op == 0x89 || op == 0x8B || op == 0x8D || op == 0x8F
        || op == 0x63 || op == 0xFF) {
        mlen = modrm_len(code + i, avail - i, NULL, &rip);
        if (mlen < 0) {
            return -1;
        }
        if (rip) {
            *is_rel = 1;
            return -2;
        }
        return i + mlen;
    }
    return -1;
}

static int steal_length(const uint8_t *code, int avail) {
    int total = 0;
    while (total < 5) {
        int rel = 0;
        int n = decode_one(code + total, avail - total, &rel);
        if (n <= 0 || rel) {
            return -1;
        }
        total += n;
        if (total > STOLEN_MAX) {
            return -1;
        }
    }
    return total;
}

static void *alloc_near(void *target, SIZE_T size) {
    SYSTEM_INFO info;
    uintptr_t gran;
    uintptr_t origin;
    uintptr_t low;
    uintptr_t high;
    int dir;
    GetSystemInfo(&info);
    gran = info.dwAllocationGranularity;
    if (gran == 0) {
        gran = 65536;
    }
    origin = (uintptr_t)target & ~(gran - 1);
    low = (uintptr_t)info.lpMinimumApplicationAddress;
    high = (uintptr_t)info.lpMaximumApplicationAddress;
    if (origin > 0x7FFF0000ULL && origin - 0x7FFF0000ULL > low) {
        low = origin - 0x7FFF0000ULL;
    }
    if (origin + 0x7FFF0000ULL < high) {
        high = origin + 0x7FFF0000ULL;
    }
    for (dir = 0; dir < 2; dir++) {
        uintptr_t addr = origin;
        int step;
        for (step = 0; step < 4096; step++) {
            MEMORY_BASIC_INFORMATION mbi;
            if (dir == 0) {
                if (addr <= low + gran) {
                    break;
                }
                addr -= gran;
            } else if (addr >= high - gran) {
                break;
            } else {
                addr += gran;
            }
            if (VirtualQuery((void *)addr, &mbi, sizeof mbi) == 0) {
                continue;
            }
            if (mbi.State != MEM_FREE) {
                continue;
            }
            {
                void *mem = VirtualAlloc(
                    (void *)addr, size, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
                if (mem != NULL) {
                    return mem;
                }
            }
        }
    }
    return NULL;
}

static int rel32_ok(void *from_next, void *dest, int32_t *rel_out) {
    int64_t rel = (int64_t)(uintptr_t)dest - (int64_t)(uintptr_t)from_next;
    if (rel > INT32_MAX || rel < INT32_MIN) {
        return 0;
    }
    *rel_out = (int32_t)rel;
    return 1;
}

static int write_rel_jmp(uint8_t *at, void *dest) {
    int32_t rel = 0;
    if (!rel32_ok(at + 5, dest, &rel)) {
        return 0;
    }
    at[0] = 0xE9;
    memcpy(at + 1, &rel, sizeof rel);
    return 1;
}

static int install_hook(void *target, void *hook_fn) {
    Hook *hook;
    int stolen;
    uint8_t *page;
    uint8_t *relay;
    DWORD old_prot = 0;
    int32_t rel = 0;
    void *jmp_dest;
    if (g_hook_count >= HOOK_MAX || target == NULL || hook_fn == NULL) {
        return 0;
    }
    stolen = steal_length((const uint8_t *)target, 48);
    if (stolen < 5) {
        return 0;
    }
    page = (uint8_t *)alloc_near(target, 4096);
    if (page == NULL) {
        return 0;
    }
    hook = &g_hooks[g_hook_count];
    memset(hook, 0, sizeof *hook);
    hook->target = target;
    hook->trampoline = page;
    hook->stolen = stolen;
    memcpy(hook->saved, target, (size_t)stolen);
    memcpy(page, target, (size_t)stolen);
    if (!rel32_ok(page + stolen + 5, (uint8_t *)target + stolen, &rel)) {
        VirtualFree(page, 0, MEM_RELEASE);
        return 0;
    }
    page[stolen] = 0xE9;
    memcpy(page + stolen + 1, &rel, sizeof rel);

    relay = page + 64;
    if (rel32_ok((uint8_t *)target + 5, hook_fn, &rel)) {
        jmp_dest = hook_fn;
        hook->relay = NULL;
    } else {
        /* Absolute jump lives next to the target; the 5-byte patch stays in range. */
        uint64_t abs_dest = (uint64_t)(uintptr_t)hook_fn;
        relay[0] = 0xFF;
        relay[1] = 0x25;
        relay[2] = 0x00;
        relay[3] = 0x00;
        relay[4] = 0x00;
        relay[5] = 0x00;
        memcpy(relay + 6, &abs_dest, sizeof abs_dest);
        jmp_dest = relay;
        hook->relay = relay;
    }
    if (!VirtualProtect(target, (SIZE_T)stolen, PAGE_EXECUTE_READWRITE, &old_prot)) {
        VirtualFree(page, 0, MEM_RELEASE);
        return 0;
    }
    if (!write_rel_jmp((uint8_t *)target, jmp_dest)) {
        VirtualProtect(target, (SIZE_T)stolen, old_prot, &old_prot);
        VirtualFree(page, 0, MEM_RELEASE);
        return 0;
    }
    VirtualProtect(target, (SIZE_T)stolen, old_prot, &old_prot);
    FlushInstructionCache(GetCurrentProcess(), target, (SIZE_T)stolen);
    FlushInstructionCache(GetCurrentProcess(), page, 128);
    hook->installed = 1;
    g_hook_count++;
    return 1;
}

static void remove_hooks(void) {
    int i;
    for (i = g_hook_count - 1; i >= 0; i--) {
        Hook *hook = &g_hooks[i];
        DWORD old_prot = 0;
        if (!hook->installed) {
            continue;
        }
        if (VirtualProtect(hook->target, (SIZE_T)hook->stolen, PAGE_EXECUTE_READWRITE, &old_prot)) {
            memcpy(hook->target, hook->saved, (size_t)hook->stolen);
            VirtualProtect(hook->target, (SIZE_T)hook->stolen, old_prot, &old_prot);
            FlushInstructionCache(GetCurrentProcess(), hook->target, (SIZE_T)hook->stolen);
        }
        if (hook->trampoline != NULL) {
            VirtualFree(hook->trampoline, 0, MEM_RELEASE);
            hook->trampoline = NULL;
        }
        hook->installed = 0;
    }
    g_hook_count = 0;
}

/* -------------------------------------------------------------------------- */
/* Paths and the actual move.                                                 */
/* -------------------------------------------------------------------------- */

static wchar_t *heap_wcs_dup(const wchar_t *text) {
    size_t n;
    wchar_t *copy;
    if (text == NULL) {
        return NULL;
    }
    n = wcslen(text);
    copy = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, (n + 1) * sizeof(wchar_t));
    if (copy == NULL) {
        return NULL;
    }
    memcpy(copy, text, (n + 1) * sizeof(wchar_t));
    return copy;
}

static void to_dos_path(const wchar_t *in, wchar_t *out, size_t cch) {
    const wchar_t *src = in;
    if (in == NULL || cch == 0) {
        return;
    }
    out[0] = L'\0';
    if (wcsncmp(in, L"\\\\?\\UNC\\", 8) == 0) {
        if (cch < 3) {
            return;
        }
        out[0] = L'\\';
        out[1] = L'\\';
        src = in + 8;
        wcsncpy_s(out + 2, cch - 2, src, _TRUNCATE);
        return;
    }
    if (wcsncmp(in, L"\\??\\UNC\\", 8) == 0) {
        if (cch < 3) {
            return;
        }
        out[0] = L'\\';
        out[1] = L'\\';
        wcsncpy_s(out + 2, cch - 2, in + 8, _TRUNCATE);
        return;
    }
    if (wcsncmp(in, L"\\\\?\\", 4) == 0) {
        src = in + 4;
    } else if (wcsncmp(in, L"\\??\\", 4) == 0) {
        src = in + 4;
    } else if (wcsncmp(in, L"\\DosDevices\\", 12) == 0) {
        src = in + 12;
    }
    wcsncpy_s(out, cch, src, _TRUNCATE);
}

/* NtQueryObject name is \Device\HarddiskVolumeN\... . Map it back to C:\... */
static int device_path_to_dos(const wchar_t *nt_path, wchar_t *out, size_t cch) {
    wchar_t drive[3];
    wchar_t target[512];
    wchar_t letter;
    size_t prefix;
    size_t rest;
    drive[1] = L':';
    drive[2] = L'\0';
    for (letter = L'C'; letter <= L'Z'; letter++) {
        drive[0] = letter;
        if (QueryDosDeviceW(drive, target, 512) == 0) {
            continue;
        }
        prefix = wcslen(target);
        if (prefix == 0 || _wcsnicmp(nt_path, target, prefix) != 0) {
            continue;
        }
        if (nt_path[prefix] != L'\\' && nt_path[prefix] != L'\0') {
            continue;
        }
        rest = wcslen(nt_path + prefix);
        if (2 + rest + 1 > cch) {
            return 0;
        }
        out[0] = letter;
        out[1] = L':';
        wcsncpy_s(out + 2, cch - 2, nt_path + prefix, _TRUNCATE);
        return 1;
    }
    for (letter = L'A'; letter <= L'B'; letter++) {
        drive[0] = letter;
        if (QueryDosDeviceW(drive, target, 512) == 0) {
            continue;
        }
        prefix = wcslen(target);
        if (prefix == 0 || _wcsnicmp(nt_path, target, prefix) != 0) {
            continue;
        }
        if (nt_path[prefix] != L'\\' && nt_path[prefix] != L'\0') {
            continue;
        }
        rest = wcslen(nt_path + prefix);
        if (2 + rest + 1 > cch) {
            return 0;
        }
        out[0] = letter;
        out[1] = L':';
        wcsncpy_s(out + 2, cch - 2, nt_path + prefix, _TRUNCATE);
        return 1;
    }
    return 0;
}

/* PowerShell opens the file with DELETE access only. GetFinalPathNameByHandle
 * then fails, and a delete would be refused before the recycle broker is asked. */
static int path_from_object_name(HANDLE handle, wchar_t *out, size_t cch) {
    typedef struct {
        USHORT Length;
        USHORT MaximumLength;
        wchar_t *Buffer;
    } SoftUnicodeString;
    typedef struct {
        SoftUnicodeString Name;
    } SoftObjectName;
    typedef NTSTATUS(NTAPI *FnNtQueryObject)(HANDLE, ULONG, PVOID, ULONG, ULONG *);
    static FnNtQueryObject query;
    uint8_t *buf;
    ULONG needed = 0;
    NTSTATUS status;
    SoftObjectName *info;
    wchar_t *name;
    size_t chars;
    int ok = 0;
    if (query == NULL) {
        query = (FnNtQueryObject)(void *)GetProcAddress(GetModuleHandleW(L"ntdll.dll"), "NtQueryObject");
        if (query == NULL) {
            return 0;
        }
    }
    buf = (uint8_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, 65536);
    if (buf == NULL) {
        return 0;
    }
    status = query(handle, 1, buf, 65536, &needed);
    if (status < 0) {
        HeapFree(GetProcessHeap(), 0, buf);
        return 0;
    }
    info = (SoftObjectName *)buf;
    name = info->Name.Buffer;
    chars = (size_t)info->Name.Length / sizeof(wchar_t);
    if (name == NULL || chars == 0 || chars >= PATH_CAP
        || (uint8_t *)name < buf
        || (uint8_t *)(name + chars) > buf + 65536) {
        HeapFree(GetProcessHeap(), 0, buf);
        return 0;
    }
    {
        wchar_t *text = (wchar_t *)HeapAlloc(
            GetProcessHeap(), 0, (chars + 1) * sizeof(wchar_t));
        if (text == NULL) {
            HeapFree(GetProcessHeap(), 0, buf);
            return 0;
        }
        memcpy(text, name, chars * sizeof(wchar_t));
        text[chars] = L'\0';
        if (wcsncmp(text, L"\\??\\", 4) == 0 || wcsncmp(text, L"\\\\?\\", 4) == 0
            || wcsncmp(text, L"\\DosDevices\\", 12) == 0) {
            to_dos_path(text, out, cch);
            ok = out[0] != L'\0';
        } else if (wcsncmp(text, L"\\Device\\", 8) == 0) {
            ok = device_path_to_dos(text, out, cch);
        }
        HeapFree(GetProcessHeap(), 0, text);
    }
    HeapFree(GetProcessHeap(), 0, buf);
    return ok;
}

static void path_from_handle(HANDLE handle, wchar_t *out, size_t cch) {
    DWORD n;
    wchar_t *raw;
    out[0] = L'\0';
    raw = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
    if (raw == NULL) {
        return;
    }
    n = GetFinalPathNameByHandleW(handle, raw, PATH_CAP, 0);
    if (n > 0 && n < PATH_CAP) {
        to_dos_path(raw, out, cch);
    }
    HeapFree(GetProcessHeap(), 0, raw);
    if (out[0] == L'\0') {
        path_from_object_name(handle, out, cch);
    }
}

static int join_root_name(HANDLE root, const wchar_t *name, wchar_t *out, size_t cch) {
    wchar_t root_path[PATH_CAP];
    size_t n;
    out[0] = L'\0';
    if (name == NULL || name[0] == L'\0') {
        return 0;
    }
    if (name[0] == L'\\' || (name[0] != L'\0' && name[1] == L':')) {
        to_dos_path(name, out, cch);
        return out[0] != L'\0';
    }
    if (root == NULL || root == INVALID_HANDLE_VALUE) {
        to_dos_path(name, out, cch);
        return out[0] != L'\0';
    }
    path_from_handle(root, root_path, PATH_CAP);
    if (root_path[0] == L'\0') {
        return 0;
    }
    n = wcslen(root_path);
    if (n + 1 + wcslen(name) + 1 >= cch) {
        return 0;
    }
    wcscpy_s(out, cch, root_path);
    if (n > 0 && out[n - 1] != L'\\' && out[n - 1] != L'/') {
        wcscat_s(out, cch, L"\\");
    }
    wcscat_s(out, cch, name);
    return 1;
}

static int path_from_object(OBJECT_ATTRIBUTES_ *oa, wchar_t *out, size_t cch) {
    wchar_t *name = NULL;
    int ok = 0;
    USHORT nbytes;
    out[0] = L'\0';
    if (oa == NULL || !memory_ok(oa, sizeof(*oa), 0)) {
        return 0;
    }
    if (oa->ObjectName == NULL || oa->ObjectName->Buffer == NULL || oa->ObjectName->Length == 0) {
        if (oa->RootDirectory != NULL) {
            path_from_handle(oa->RootDirectory, out, cch);
            ok = out[0] != L'\0';
        }
    } else if (!memory_ok(oa->ObjectName, sizeof(*oa->ObjectName), 0)) {
        ok = 0;
    } else {
        nbytes = oa->ObjectName->Length;
        if (nbytes > 0 && (nbytes % sizeof(wchar_t)) == 0 && (size_t)nbytes < PATH_CAP * sizeof(wchar_t)
            && memory_ok(oa->ObjectName->Buffer, nbytes, 0)) {
            name = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, (SIZE_T)nbytes + sizeof(wchar_t));
            if (name != NULL) {
                memcpy(name, oa->ObjectName->Buffer, nbytes);
                name[nbytes / sizeof(wchar_t)] = L'\0';
                ok = join_root_name(oa->RootDirectory, name, out, cch);
            }
        }
    }
    if (name != NULL) {
        HeapFree(GetProcessHeap(), 0, name);
    }
    return ok;
}

static int path_exists(const wchar_t *path) {
    DWORD attr = GetFileAttributesW(path);
    return attr != INVALID_FILE_ATTRIBUTES;
}

static void read_archive_dir(wchar_t *out, size_t cch) {
    DWORD n = GetEnvironmentVariableW(ENV_ARCHIVE, out, (DWORD)cch);
    if (n == 0 || n >= cch) {
        /* No cwd fallback. A missing setting must not create .trash beside the command. */
        out[0] = L'\0';
    }
}

static int normalize_dos_path(const wchar_t *in, wchar_t *out, size_t cch) {
    DWORD n;
    if (in == NULL || in[0] == L'\0' || out == NULL || cch == 0) {
        return 0;
    }
    n = GetFullPathNameW(in, (DWORD)cch, out, NULL);
    return n > 0 && n < cch;
}

/* X:\$Recycle.Bin only. A workspace folder with the same name stays soft-deleted. */
static int is_volume_recycle_bin(const wchar_t *full) {
    const wchar_t *cursor = full;
    if (cursor == NULL) {
        return 0;
    }
    if (wcsncmp(cursor, L"\\\\?\\", 4) == 0) {
        cursor += 4;
    }
    if (cursor[0] == L'\0' || cursor[1] != L':' || (cursor[2] != L'\\' && cursor[2] != L'/')) {
        return 0;
    }
    cursor += 3;
    if (_wcsnicmp(cursor, L"$Recycle.Bin", 12) != 0) {
        return 0;
    }
    return cursor[12] == L'\0' || cursor[12] == L'\\' || cursor[12] == L'/';
}

static int is_under_dir(const wchar_t *path, const wchar_t *dir) {
    size_t n;
    if (path == NULL || dir == NULL || dir[0] == L'\0') {
        return 0;
    }
    n = wcslen(dir);
    while (n > 0 && (dir[n - 1] == L'\\' || dir[n - 1] == L'/')) {
        n--;
    }
    if (n == 0 || _wcsnicmp(path, dir, n) != 0) {
        return 0;
    }
    return path[n] == L'\0' || path[n] == L'\\' || path[n] == L'/';
}

static int is_skipped_path(const wchar_t *dos_path) {
    wchar_t *full;
    wchar_t *archive;
    int under;
    if (dos_path == NULL || dos_path[0] == L'\0') {
        return 1;
    }
    full = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
    if (full == NULL || !normalize_dos_path(dos_path, full, PATH_CAP)) {
        HeapFree(GetProcessHeap(), 0, full);
        return 0;
    }
    if (is_volume_recycle_bin(full)) {
        HeapFree(GetProcessHeap(), 0, full);
        return 1;
    }
    archive = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
    if (archive == NULL) {
        HeapFree(GetProcessHeap(), 0, full);
        return 0;
    }
    read_archive_dir(archive, PATH_CAP);
    under = is_under_dir(full, archive);
    HeapFree(GetProcessHeap(), 0, archive);
    HeapFree(GetProcessHeap(), 0, full);
    return under;
}

static int mkdir_p(const wchar_t *path) {
    wchar_t buf[PATH_CAP];
    size_t i;
    size_t n;
    if (path == NULL || path[0] == L'\0') {
        return 0;
    }
    wcsncpy_s(buf, PATH_CAP, path, _TRUNCATE);
    n = wcslen(buf);
    if (n >= 2 && buf[1] == L':') {
        i = 2;
    } else {
        i = 0;
    }
    for (; i < n; i++) {
        if (buf[i] != L'\\' && buf[i] != L'/') {
            continue;
        }
        if (i == 0) {
            continue;
        }
        buf[i] = L'\0';
        if (buf[0] != L'\0' && !path_exists(buf)) {
            if (!CreateDirectoryW(buf, NULL) && GetLastError() != ERROR_ALREADY_EXISTS) {
                return 0;
            }
        }
        buf[i] = L'\\';
    }
    if (!path_exists(buf)) {
        if (!CreateDirectoryW(buf, NULL) && GetLastError() != ERROR_ALREADY_EXISTS) {
            return 0;
        }
    }
    return 1;
}

typedef struct DirNode_ {
    struct DirNode_ *next;
    wchar_t *path;
} DirNode_;

static void free_dir_list(DirNode_ *node) {
    while (node != NULL) {
        DirNode_ *next = node->next;
        HeapFree(GetProcessHeap(), 0, node->path);
        HeapFree(GetProcessHeap(), 0, node);
        node = next;
    }
}

static DirNode_ *dir_node(const wchar_t *path) {
    DirNode_ *node = (DirNode_ *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, sizeof(DirNode_));
    if (node == NULL) {
        return NULL;
    }
    node->path = heap_wcs_dup(path);
    if (node->path == NULL) {
        HeapFree(GetProcessHeap(), 0, node);
        return NULL;
    }
    return node;
}

static int tree_size(const wchar_t *path, ULONGLONG *total, int *count) {
    DWORD attr = GetFileAttributesW(path);
    DirNode_ *queue;
    if (attr == INVALID_FILE_ATTRIBUTES) {
        return 0;
    }
    if ((attr & FILE_ATTRIBUTE_REPARSE_POINT) != 0) {
        return 1;
    }
    if ((attr & FILE_ATTRIBUTE_DIRECTORY) == 0) {
        WIN32_FILE_ATTRIBUTE_DATA data;
        if (!GetFileAttributesExW(path, GetFileExInfoStandard, &data)) {
            return 0;
        }
        *total += ((ULONGLONG)data.nFileSizeHigh << 32) | data.nFileSizeLow;
        return 1;
    }
    queue = dir_node(path);
    if (queue == NULL) {
        return 0;
    }
    while (queue != NULL) {
        DirNode_ *cur = queue;
        wchar_t *pattern;
        HANDLE find;
        WIN32_FIND_DATAW fd;
        queue = cur->next;
        pattern = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, PATH_CAP * sizeof(wchar_t));
        if (pattern == NULL || swprintf_s(pattern, PATH_CAP, L"%s\\*", cur->path) < 0) {
            HeapFree(GetProcessHeap(), 0, pattern);
            HeapFree(GetProcessHeap(), 0, cur->path);
            HeapFree(GetProcessHeap(), 0, cur);
            free_dir_list(queue);
            return 0;
        }
        find = FindFirstFileExW(
            pattern, FindExInfoBasic, &fd, FindExSearchNameMatch, NULL, FIND_FIRST_EX_LARGE_FETCH);
        HeapFree(GetProcessHeap(), 0, pattern);
        if (find == INVALID_HANDLE_VALUE) {
            HeapFree(GetProcessHeap(), 0, cur->path);
            HeapFree(GetProcessHeap(), 0, cur);
            free_dir_list(queue);
            return 0;
        }
        do {
            wchar_t *child;
            size_t need;
            if (wcscmp(fd.cFileName, L".") == 0 || wcscmp(fd.cFileName, L"..") == 0) {
                continue;
            }
            (*count)++;
            if (*count > 100000) {
                FindClose(find);
                HeapFree(GetProcessHeap(), 0, cur->path);
                HeapFree(GetProcessHeap(), 0, cur);
                free_dir_list(queue);
                return 0;
            }
            /* A junction inside the tree must not be walked by the recycle API. */
            if ((fd.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT) != 0) {
                FindClose(find);
                HeapFree(GetProcessHeap(), 0, cur->path);
                HeapFree(GetProcessHeap(), 0, cur);
                free_dir_list(queue);
                return 0;
            }
            need = wcslen(cur->path) + 1 + wcslen(fd.cFileName) + 1;
            child = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, need * sizeof(wchar_t));
            if (child == NULL || swprintf_s(child, need, L"%s\\%s", cur->path, fd.cFileName) < 0) {
                HeapFree(GetProcessHeap(), 0, child);
                FindClose(find);
                HeapFree(GetProcessHeap(), 0, cur->path);
                HeapFree(GetProcessHeap(), 0, cur);
                free_dir_list(queue);
                return 0;
            }
            if ((fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0) {
                DirNode_ *node = dir_node(child);
                HeapFree(GetProcessHeap(), 0, child);
                if (node == NULL) {
                    FindClose(find);
                    HeapFree(GetProcessHeap(), 0, cur->path);
                    HeapFree(GetProcessHeap(), 0, cur);
                    free_dir_list(queue);
                    return 0;
                }
                node->next = queue;
                queue = node;
            } else {
                *total += ((ULONGLONG)fd.nFileSizeHigh << 32) | fd.nFileSizeLow;
                HeapFree(GetProcessHeap(), 0, child);
            }
        } while (FindNextFileW(find, &fd));
        {
            DWORD find_error = GetLastError();
            FindClose(find);
            if (find_error != ERROR_NO_MORE_FILES) {
                HeapFree(GetProcessHeap(), 0, cur->path);
                HeapFree(GetProcessHeap(), 0, cur);
                free_dir_list(queue);
                return 0;
            }
        }
        HeapFree(GetProcessHeap(), 0, cur->path);
        HeapFree(GetProcessHeap(), 0, cur);
    }
    return 1;
}

static int reg_dword(HKEY key, const wchar_t *name, DWORD *value) {
    DWORD size = sizeof(DWORD);
    DWORD type = 0;
    if (RegQueryValueExW(key, name, NULL, &type, (LPBYTE)value, &size) != ERROR_SUCCESS) {
        return 0;
    }
    return type == REG_DWORD;
}

static int volume_recycle_subkey(
    const wchar_t *dos_path, wchar_t *vol_root, size_t root_cch, wchar_t *subkey, size_t sub_cch) {
    wchar_t vol_name[MAX_PATH];
    const wchar_t *brace;
    size_t len;
    vol_root[0] = L'\0';
    vol_name[0] = L'\0';
    if (!GetVolumePathNameW(dos_path, vol_root, (DWORD)root_cch)) {
        return 0;
    }
    if (!GetVolumeNameForVolumeMountPointW(vol_root, vol_name, MAX_PATH)) {
        return 0;
    }
    /* vol_name is \\?\Volume{guid}\ ; BitBucket stores Volume\{guid}. */
    brace = wcschr(vol_name, L'{');
    if (brace == NULL || swprintf_s(subkey, sub_cch, L"Volume\\%s", brace) < 0) {
        return 0;
    }
    len = wcslen(subkey);
    while (len > 0 && subkey[len - 1] == L'\\') {
        subkey[--len] = L'\0';
    }
    return subkey[0] != L'\0';
}

static int recycle_allowed(const wchar_t *dos_path, ULONGLONG bytes) {
    HKEY root = NULL;
    HKEY volume = NULL;
    DWORD nuke = 0;
    DWORD max_mb = 0;
    int have_max = 0;
    wchar_t vol_root[MAX_PATH];
    wchar_t subkey[160];
    ULONGLONG budget;
    ULARGE_INTEGER total_bytes;
    ULARGE_INTEGER free_bytes;
    if (RegOpenKeyExW(
            HKEY_CURRENT_USER,
            L"Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\BitBucket",
            0, KEY_READ, &root) != ERROR_SUCCESS) {
        return 0;
    }
    if (reg_dword(root, L"NukeOnDelete", &nuke) && nuke != 0) {
        RegCloseKey(root);
        return 0;
    }
    /* Volume policy missing: do not call SHFileOperationW. It permanent-deletes when the bin is off. */
    if (!volume_recycle_subkey(dos_path, vol_root, MAX_PATH, subkey, 160)
        || RegOpenKeyExW(root, subkey, 0, KEY_READ, &volume) != ERROR_SUCCESS) {
        RegCloseKey(root);
        return 0;
    }
    if (reg_dword(volume, L"NukeOnDelete", &nuke) && nuke != 0) {
        RegCloseKey(volume);
        RegCloseKey(root);
        return 0;
    }
    if (reg_dword(volume, L"MaxCapacity", &max_mb)) {
        have_max = 1;
    }
    RegCloseKey(volume);
    RegCloseKey(root);
    if (have_max) {
        budget = (ULONGLONG)max_mb * 1024ULL * 1024ULL;
    } else {
        total_bytes.QuadPart = 0;
        if (!GetDiskFreeSpaceExW(vol_root[0] ? vol_root : NULL, &free_bytes, &total_bytes, NULL)
            || total_bytes.QuadPart == 0) {
            budget = 64ULL * 1024ULL * 1024ULL;
        } else {
            budget = total_bytes.QuadPart / 100ULL;
            if (budget > 512ULL * 1024ULL * 1024ULL) {
                budget = 512ULL * 1024ULL * 1024ULL;
            }
        }
    }
    return bytes < budget;
}

static int send_to_recycle(const wchar_t *dos_path) {
    wchar_t *from;
    SHFILEOPSTRUCTW op;
    int rc;
    size_t n;
    if (wcslen(dos_path) >= MAX_PATH) {
        return 0;
    }
    from = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, (wcslen(dos_path) + 2) * sizeof(wchar_t));
    if (from == NULL) {
        return 0;
    }
    wcscpy_s(from, wcslen(dos_path) + 2, dos_path);
    n = wcslen(from);
    from[n] = L'\0';
    from[n + 1] = L'\0';
    memset(&op, 0, sizeof op);
    op.wFunc = FO_DELETE;
    op.pFrom = from;
    op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR);
    rc = SHFileOperationW(&op);
    HeapFree(GetProcessHeap(), 0, from);
    if (rc != 0 || op.fAnyOperationsAborted) {
        return 0;
    }
    if (path_exists(dos_path)) {
        return 0;
    }
    return 1;
}

static void sanitize_name(const wchar_t *path, wchar_t *out, size_t cch) {
    const wchar_t *base = path;
    const wchar_t *scan;
    size_t i = 0;
    for (scan = path; *scan != L'\0'; scan++) {
        if (*scan == L'\\' || *scan == L'/') {
            base = scan + 1;
        }
    }
    for (; *base != L'\0' && i + 1 < cch && i < 80; base++) {
        wchar_t ch = *base;
        if (ch == L'\\' || ch == L'/' || ch == L':' || ch == L'*' || ch == L'?' || ch == L'"'
            || ch == L'<' || ch == L'>' || ch == L'|' || ch < 32) {
            ch = L'_';
        }
        out[i++] = ch;
    }
    if (i == 0) {
        wcscpy_s(out, cch, L"file");
        return;
    }
    out[i] = L'\0';
}

static int same_volume(const wchar_t *left, const wchar_t *right) {
    wchar_t a[MAX_PATH];
    wchar_t b[MAX_PATH];
    if (!GetVolumePathNameW(left, a, MAX_PATH) || !GetVolumePathNameW(right, b, MAX_PATH)) {
        return 0;
    }
    return _wcsicmp(a, b) == 0;
}

static int move_to_archive(const wchar_t *dos_path) {
    wchar_t archive[PATH_CAP];
    wchar_t name[96];
    wchar_t dest[PATH_CAP];
    SYSTEMTIME st;
    int i;
    read_archive_dir(archive, PATH_CAP);
    if (archive[0] == L'\0') {
        return 0;
    }
    if (!same_volume(dos_path, archive)) {
        log_line(L"archive skipped: different volume");
        return 0;
    }
    if (!mkdir_p(archive)) {
        return 0;
    }
    sanitize_name(dos_path, name, 96);
    GetLocalTime(&st);
    /* Outer name only. A directory rename leaves names inside it unchanged. */
    for (i = 0; i < 1000; i++) {
        int wrote;
        if (i == 0) {
            wrote = swprintf_s(
                dest, PATH_CAP, L"%s\\%s_%04u%02u%02u_%02u%02u%02u",
                archive, name, st.wYear, st.wMonth, st.wDay,
                st.wHour, st.wMinute, st.wSecond);
        } else {
            wrote = swprintf_s(
                dest, PATH_CAP, L"%s\\%s_%04u%02u%02u_%02u%02u%02u_%d",
                archive, name, st.wYear, st.wMonth, st.wDay,
                st.wHour, st.wMinute, st.wSecond, i);
        }
        if (wrote < 0) {
            return 0;
        }
        if (!path_exists(dest)) {
            break;
        }
    }
    if (path_exists(dest)) {
        return 0;
    }
    /* No MOVEFILE_COPY_ALLOWED: a cross-volume copy would delete the source. */
    if (!MoveFileExW(dos_path, dest, MOVEFILE_WRITE_THROUGH)) {
        return 0;
    }
    {
        wchar_t msg[1024];
        swprintf_s(msg, 1024, L"archive %s -> %s", dos_path, dest);
        log_line(msg);
    }
    return 1;
}

typedef int (WINAPI *FnWsaStartup)(WORD, void *);
typedef uintptr_t (WINAPI *FnSock)(int, int, int);
typedef int (WINAPI *FnSockOp)(uintptr_t, const void *, int);
typedef int (WINAPI *FnSendRecv)(uintptr_t, char *, int, int);
typedef int (WINAPI *FnCloseSock)(uintptr_t);
typedef int (WINAPI *FnSetOpt)(uintptr_t, int, int, const char *, int);
typedef unsigned short (WINAPI *FnHtons)(unsigned short);

static int g_wsa_ok;
static HMODULE g_ws2;
static FnSock g_sock_fn;
static FnSockOp g_connect_fn;
static FnSendRecv g_send_fn;
static FnSendRecv g_recv_fn;
static FnCloseSock g_close_fn;
static FnSetOpt g_setopt_fn;
static FnHtons g_htons_fn;

/* Set means the sandbox must ask box-server. A bad value still must not fall through. */
static int recycle_delegation_configured(void) {
    wchar_t buf[2];
    DWORD n = GetEnvironmentVariableW(ENV_RECYCLE_PORT, buf, 2);
    return n > 0;
}

static int parse_port(const wchar_t *text) {
    int port = 0;
    if (text == NULL || text[0] == L'\0') {
        return 0;
    }
    for (; *text != L'\0'; text++) {
        if (*text < L'0' || *text > L'9') {
            return 0;
        }
        port = port * 10 + (int)(*text - L'0');
        if (port > 65535) {
            return 0;
        }
    }
    return port;
}

static int wsa_ready(void) {
    FnWsaStartup startup;
    unsigned char data[512];
    if (g_wsa_ok) {
        return 1;
    }
    if (g_ws2 == NULL) {
        g_ws2 = LoadLibraryW(L"ws2_32.dll");
    }
    if (g_ws2 == NULL) {
        return 0;
    }
    startup = (FnWsaStartup)(void *)GetProcAddress(g_ws2, "WSAStartup");
    g_sock_fn = (FnSock)(void *)GetProcAddress(g_ws2, "socket");
    g_connect_fn = (FnSockOp)(void *)GetProcAddress(g_ws2, "connect");
    g_send_fn = (FnSendRecv)(void *)GetProcAddress(g_ws2, "send");
    g_recv_fn = (FnSendRecv)(void *)GetProcAddress(g_ws2, "recv");
    g_close_fn = (FnCloseSock)(void *)GetProcAddress(g_ws2, "closesocket");
    g_setopt_fn = (FnSetOpt)(void *)GetProcAddress(g_ws2, "setsockopt");
    g_htons_fn = (FnHtons)(void *)GetProcAddress(g_ws2, "htons");
    if (startup == NULL || g_sock_fn == NULL || g_connect_fn == NULL || g_send_fn == NULL
        || g_recv_fn == NULL || g_close_fn == NULL || g_setopt_fn == NULL || g_htons_fn == NULL) {
        return 0;
    }
    if (startup(0x0202, data) != 0) {
        g_sock_fn = NULL;
        return 0;
    }
    g_wsa_ok = 1;
    return 1;
}

static void store_be32(unsigned char *dest, unsigned long value) {
    dest[0] = (unsigned char)((value >> 24) & 0xffu);
    dest[1] = (unsigned char)((value >> 16) & 0xffu);
    dest[2] = (unsigned char)((value >> 8) & 0xffu);
    dest[3] = (unsigned char)(value & 0xffu);
}

static unsigned long load_be32(const unsigned char *src) {
    return ((unsigned long)src[0] << 24) | ((unsigned long)src[1] << 16)
        | ((unsigned long)src[2] << 8) | (unsigned long)src[3];
}

static int sock_send_all(uintptr_t sock, const char *buf, int len) {
    int sent = 0;
    while (sent < len) {
        int n = g_send_fn(sock, (char *)(buf + sent), len - sent, 0);
        if (n <= 0) {
            return 0;
        }
        sent += n;
    }
    return 1;
}

static int sock_recv_all(uintptr_t sock, char *buf, int len) {
    int got = 0;
    while (got < len) {
        int n = g_recv_fn(sock, buf + got, len - got, 0);
        if (n <= 0) {
            return 0;
        }
        got += n;
    }
    return 1;
}

static char *utf8_of(const wchar_t *text, int *out_len) {
    int bytes;
    char *buf;
    *out_len = 0;
    bytes = WideCharToMultiByte(CP_UTF8, 0, text, -1, NULL, 0, NULL, NULL);
    if (bytes <= 1) {
        return NULL;
    }
    buf = (char *)HeapAlloc(GetProcessHeap(), 0, (SIZE_T)bytes);
    if (buf == NULL) {
        return NULL;
    }
    if (WideCharToMultiByte(CP_UTF8, 0, text, -1, buf, bytes, NULL, NULL) != bytes) {
        HeapFree(GetProcessHeap(), 0, buf);
        return NULL;
    }
    *out_len = bytes - 1;
    return buf;
}

/* Ask box-server, which runs as the logged-in user, to recycle this path.
 * Any failure leaves the file where it is. This function never deletes it. */
static int ask_recycle_broker(const wchar_t *dos_path) {
    wchar_t port_text[16];
    wchar_t token_w[RECYCLE_TOKEN_MAX];
    DWORD port_n;
    DWORD token_n;
    int port;
    int token_len = 0;
    int path_len = 0;
    char *token_utf = NULL;
    char *path_utf = NULL;
    unsigned char *frame = NULL;
    unsigned body_len;
    uintptr_t sock = (uintptr_t)-1;
    unsigned char addr[16];
    unsigned char len_buf[4];
    char resp[2];
    DWORD timeout_ms = 15000;
    unsigned short net_port;
    int ok = 0;

    port_n = GetEnvironmentVariableW(ENV_RECYCLE_PORT, port_text, 16);
    if (port_n == 0 || port_n >= 16) {
        return 0;
    }
    port = parse_port(port_text);
    if (port == 0 || !wsa_ready()) {
        return 0;
    }
    token_n = GetEnvironmentVariableW(ENV_RECYCLE_TOKEN, token_w, RECYCLE_TOKEN_MAX);
    if (token_n == 0 || token_n >= RECYCLE_TOKEN_MAX) {
        return 0;
    }
    token_utf = utf8_of(token_w, &token_len);
    path_utf = utf8_of(dos_path, &path_len);
    if (token_utf == NULL || path_utf == NULL || token_len <= 0 || path_len <= 0
        || (unsigned)token_len > RECYCLE_TOKEN_MAX || (unsigned)path_len > RECYCLE_PATH_MAX) {
        goto done;
    }
    body_len = 4u + (unsigned)token_len + 4u + (unsigned)path_len;
    frame = (unsigned char *)HeapAlloc(GetProcessHeap(), 0, 4u + body_len);
    if (frame == NULL) {
        goto done;
    }
    store_be32(frame, body_len);
    store_be32(frame + 4, (unsigned long)token_len);
    memcpy(frame + 8, token_utf, (size_t)token_len);
    store_be32(frame + 8 + token_len, (unsigned long)path_len);
    memcpy(frame + 12 + token_len, path_utf, (size_t)path_len);

    sock = g_sock_fn(2, 1, 6);
    if (sock == (uintptr_t)-1) {
        goto done;
    }
    g_setopt_fn(sock, 0xffff, 0x1005, (const char *)&timeout_ms, (int)sizeof(timeout_ms));
    g_setopt_fn(sock, 0xffff, 0x1006, (const char *)&timeout_ms, (int)sizeof(timeout_ms));
    memset(addr, 0, sizeof(addr));
    addr[0] = 2;
    net_port = g_htons_fn((unsigned short)port);
    memcpy(addr + 2, &net_port, sizeof(net_port));
    addr[4] = 127;
    addr[7] = 1;
    if (g_connect_fn(sock, addr, 16) != 0) {
        goto done;
    }
    if (!sock_send_all(sock, (const char *)frame, (int)(4u + body_len))) {
        goto done;
    }
    if (!sock_recv_all(sock, (char *)len_buf, 4)) {
        goto done;
    }
    if (load_be32(len_buf) != 2 || !sock_recv_all(sock, resp, 2)) {
        goto done;
    }
    ok = (resp[0] == 'o' && resp[1] == 'k');
done:
    if (sock != (uintptr_t)-1 && g_close_fn != NULL) {
        g_close_fn(sock);
    }
    if (frame != NULL) {
        HeapFree(GetProcessHeap(), 0, frame);
    }
    if (token_utf != NULL) {
        HeapFree(GetProcessHeap(), 0, token_utf);
    }
    if (path_utf != NULL) {
        HeapFree(GetProcessHeap(), 0, path_utf);
    }
    return ok;
}

static int soft_delete_path(const wchar_t *dos_path) {
    DWORD attr;
    if (dos_path == NULL || dos_path[0] == L'\0') {
        return 0;
    }
    if (t_bypass) {
        return 0;
    }
    t_bypass = 1;
    attr = GetFileAttributesW(dos_path);
    if (attr == INVALID_FILE_ATTRIBUTES) {
        t_bypass = 0;
        return 0;
    }
    if (is_skipped_path(dos_path)) {
        t_bypass = 0;
        return 0;
    }
    /* Sandbox deletes are recycled by box-server. Failure keeps the file.
     * Expand here: box-server's current directory is not this command's. */
    if (recycle_delegation_configured()) {
        wchar_t *full;
        DWORD full_n;
        int delegated;
        full = (wchar_t *)HeapAlloc(
            GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
        if (full == NULL) {
            t_bypass = 0;
            return 0;
        }
        full_n = GetFullPathNameW(dos_path, PATH_CAP, full, NULL);
        if (full_n == 0 || full_n >= PATH_CAP) {
            HeapFree(GetProcessHeap(), 0, full);
            t_bypass = 0;
            log_line(L"recycle broker refused; path could not be made absolute");
            return 0;
        }
        delegated = ask_recycle_broker(full);
        t_bypass = 0;
        if (delegated) {
            wchar_t msg[1024];
            swprintf_s(msg, 1024, L"recycle-delegated %s", full);
            log_line(msg);
            HeapFree(GetProcessHeap(), 0, full);
            return 1;
        }
        HeapFree(GetProcessHeap(), 0, full);
        log_line(L"recycle broker refused; source left in place");
        return 0;
    }
    {
        ULONGLONG bytes = 0;
        int count = 0;
        int sized = tree_size(dos_path, &bytes, &count);
        if ((attr & FILE_ATTRIBUTE_REPARSE_POINT) == 0 && sized
            && recycle_allowed(dos_path, bytes)) {
            if (send_to_recycle(dos_path)) {
                wchar_t msg[1024];
                swprintf_s(msg, 1024, L"recycle %s", dos_path);
                log_line(msg);
                t_bypass = 0;
                return 1;
            }
        }
        if (move_to_archive(dos_path)) {
            t_bypass = 0;
            return 1;
        }
        log_line(L"soft-delete failed; source left in place");
        t_bypass = 0;
        return 0;
    }
}

static void log_line(const wchar_t *text) {
    /* Debugger only. Writing a log file would create the archive directory. */
    OutputDebugStringW(text);
    OutputDebugStringW(L"\n");
}

/* -------------------------------------------------------------------------- */
/* Pending deletes: disposition is recorded while the handle is still open.  */
/* -------------------------------------------------------------------------- */

static int pending_put(HANDLE handle, const wchar_t *path) {
    int i;
    int free_slot = -1;
    wchar_t *copy;
    if (handle == NULL || handle == INVALID_HANDLE_VALUE || path == NULL || path[0] == L'\0') {
        return 0;
    }
    copy = heap_wcs_dup(path);
    if (copy == NULL) {
        return 0;
    }
    EnterCriticalSection(&g_lock);
    for (i = 0; i < PENDING_MAX; i++) {
        if (g_pending_handle[i] == handle) {
            HeapFree(GetProcessHeap(), 0, g_pending_path[i]);
            g_pending_path[i] = copy;
            LeaveCriticalSection(&g_lock);
            return 1;
        }
        if (free_slot < 0 && g_pending_handle[i] == NULL) {
            free_slot = i;
        }
    }
    if (free_slot < 0) {
        LeaveCriticalSection(&g_lock);
        HeapFree(GetProcessHeap(), 0, copy);
        return 0;
    }
    g_pending_handle[free_slot] = handle;
    g_pending_path[free_slot] = copy;
    LeaveCriticalSection(&g_lock);
    return 1;
}

static void pending_clear(HANDLE handle) {
    int i;
    EnterCriticalSection(&g_lock);
    for (i = 0; i < PENDING_MAX; i++) {
        if (g_pending_handle[i] == handle) {
            HeapFree(GetProcessHeap(), 0, g_pending_path[i]);
            g_pending_path[i] = NULL;
            g_pending_handle[i] = NULL;
        }
    }
    LeaveCriticalSection(&g_lock);
}

static int pending_take(HANDLE handle, wchar_t *out, size_t cch) {
    int i;
    int found = 0;
    out[0] = L'\0';
    EnterCriticalSection(&g_lock);
    for (i = 0; i < PENDING_MAX; i++) {
        if (g_pending_handle[i] == handle && g_pending_path[i] != NULL) {
            wcsncpy_s(out, cch, g_pending_path[i], _TRUNCATE);
            HeapFree(GetProcessHeap(), 0, g_pending_path[i]);
            g_pending_path[i] = NULL;
            g_pending_handle[i] = NULL;
            found = 1;
            break;
        }
    }
    LeaveCriticalSection(&g_lock);
    return found;
}

static void set_iosb(IO_STATUS_BLOCK_ *iosb, NTSTATUS status) {
    if (iosb == NULL || !memory_ok(iosb, sizeof(*iosb), 1)) {
        return;
    }
    iosb->StatusUnion.Status = status;
    iosb->Information = 0;
}

static int disposition_wants_delete(ULONG cls, const uint8_t *info, ULONG length) {
    if (cls == FILE_DISPOSITION_INFORMATION) {
        return length >= 1 && info[0] != 0;
    }
    if (cls == FILE_DISPOSITION_INFORMATION_EX && length >= sizeof(ULONG)) {
        ULONG flags = 0;
        memcpy(&flags, info, sizeof flags);
        return (flags & 1UL) != 0;
    }
    return 0;
}

static int disposition_clears_delete(ULONG cls, const uint8_t *info, ULONG length) {
    if (cls == FILE_DISPOSITION_INFORMATION) {
        return length >= 1 && info[0] == 0;
    }
    if (cls == FILE_DISPOSITION_INFORMATION_EX && length >= sizeof(ULONG)) {
        ULONG flags = 0;
        memcpy(&flags, info, sizeof flags);
        return (flags & 1UL) == 0;
    }
    return 0;
}

static int rename_replaces(ULONG cls, const uint8_t *info, ULONG length) {
    ULONG flags = 0;
    if (cls == FILE_RENAME_INFORMATION) {
        return length >= 1 && info[0] != 0;
    }
    if (cls == FILE_RENAME_INFORMATION_EX && length >= sizeof(ULONG)) {
        memcpy(&flags, info, sizeof flags);
        return (flags & FILE_RENAME_REPLACE_IF_EXISTS) != 0;
    }
    return 0;
}

static int rename_replace_dest(ULONG cls, const uint8_t *info, ULONG length, wchar_t *dest, size_t cch) {
    HANDLE root = NULL;
    ULONG name_bytes = 0;
    wchar_t *name = NULL;
    int ok = 0;
    dest[0] = L'\0';
    if (length < 20 || !memory_ok(info, length, 0)) {
        return 0;
    }
    memcpy(&root, info + 8, sizeof root);
    memcpy(&name_bytes, info + 16, sizeof name_bytes);
    if (!rename_replaces(cls, info, length) || name_bytes == 0 || (name_bytes % 2) != 0
        || 20u + name_bytes > length) {
        return 0;
    }
    name = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, (SIZE_T)name_bytes + sizeof(wchar_t));
    if (name == NULL) {
        return 0;
    }
    memcpy(name, info + 20, name_bytes);
    name[name_bytes / sizeof(wchar_t)] = L'\0';
    ok = join_root_name(root, name, dest, cch);
    if (name != NULL) {
        HeapFree(GetProcessHeap(), 0, name);
    }
    return ok;
}

/* -------------------------------------------------------------------------- */
/* Hooks.                                                                     */
/* -------------------------------------------------------------------------- */

static int path_has_file_id(
    const wchar_t *path, int have_id, DWORD volume, DWORD index_high, DWORD index_low) {
    BY_HANDLE_FILE_INFORMATION info;
    HANDLE opened;
    int same;
    /* A delete-only handle cannot be identified. Keep the recorded path. */
    if (!have_id) {
        return 1;
    }
    opened = CreateFileW(
        path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        NULL, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, NULL);
    if (opened == INVALID_HANDLE_VALUE) {
        return 0;
    }
    same = GetFileInformationByHandle(opened, &info)
        && info.dwVolumeSerialNumber == volume
        && info.nFileIndexHigh == index_high
        && info.nFileIndexLow == index_low;
    CloseHandle(opened);
    return same;
}

static NTSTATUS NTAPI hook_NtClose(HANDLE handle) {
    wchar_t *path;
    NTSTATUS status;
    int pending;
    int have_id = 0;
    DWORD volume = 0;
    DWORD index_high = 0;
    DWORD index_low = 0;
    BY_HANDLE_FILE_INFORMATION before;
    if (t_bypass) {
        return g_nt_close(handle);
    }
    path = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
    if (path == NULL) {
        return STATUS_ACCESS_DENIED;
    }
    pending = pending_take(handle, path, PATH_CAP);
    if (pending && GetFileInformationByHandle(handle, &before)) {
        have_id = 1;
        volume = before.dwVolumeSerialNumber;
        index_high = before.nFileIndexHigh;
        index_low = before.nFileIndexLow;
    }
    status = g_nt_close(handle);
    if (!pending) {
        HeapFree(GetProcessHeap(), 0, path);
        return status;
    }
    if (status < 0) {
        pending_put(handle, path);
        HeapFree(GetProcessHeap(), 0, path);
        return status;
    }
    if (!path_has_file_id(path, have_id, volume, index_high, index_low)) {
        HeapFree(GetProcessHeap(), 0, path);
        return STATUS_ACCESS_DENIED;
    }
    if (is_skipped_path(path)) {
        HeapFree(GetProcessHeap(), 0, path);
        return status;
    }
    if (!soft_delete_path(path)) {
        int gone = !path_exists(path);
        HeapFree(GetProcessHeap(), 0, path);
        if (gone) {
            return STATUS_OBJECT_NAME_NOT_FOUND;
        }
        return STATUS_ACCESS_DENIED;
    }
    HeapFree(GetProcessHeap(), 0, path);
    return STATUS_SUCCESS;
}

static NTSTATUS NTAPI hook_NtDeleteFile(OBJECT_ATTRIBUTES_ *oa) {
    wchar_t *path;
    int ok;
    if (t_bypass) {
        return g_nt_delete_file(oa);
    }
    path = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
    if (path == NULL) {
        return STATUS_ACCESS_DENIED;
    }
    if (!path_from_object(oa, path, PATH_CAP)) {
        HeapFree(GetProcessHeap(), 0, path);
        return STATUS_ACCESS_DENIED;
    }
    if (is_skipped_path(path)) {
        HeapFree(GetProcessHeap(), 0, path);
        return g_nt_delete_file(oa);
    }
    if (!path_exists(path)) {
        HeapFree(GetProcessHeap(), 0, path);
        return STATUS_OBJECT_NAME_NOT_FOUND;
    }
    ok = soft_delete_path(path);
    HeapFree(GetProcessHeap(), 0, path);
    if (!ok) {
        return STATUS_ACCESS_DENIED;
    }
    return STATUS_SUCCESS;
}

static NTSTATUS NTAPI hook_NtSetInformationFile(
    HANDLE file, IO_STATUS_BLOCK_ *iosb, PVOID info, ULONG length, ULONG cls) {
    uint8_t *bytes = NULL;
    wchar_t *path = NULL;
    wchar_t *dest = NULL;
    NTSTATUS status = STATUS_SUCCESS;
    int handled = 0;
    if (t_bypass) {
        return g_nt_set_info(file, iosb, info, length, cls);
    }
    if (cls != FILE_DISPOSITION_INFORMATION && cls != FILE_DISPOSITION_INFORMATION_EX
        && cls != FILE_RENAME_INFORMATION && cls != FILE_RENAME_INFORMATION_EX) {
        return g_nt_set_info(file, iosb, info, length, cls);
    }
    if (info == NULL || length == 0 || !memory_ok(info, length < 20 ? length : 20, 0)) {
        return STATUS_ACCESS_DENIED;
    }
    if (length > INFO_COPY_MAX) {
        /* A normal rename still has to work. Only a delete or replace that we cannot inspect is refused. */
        if (cls == FILE_DISPOSITION_INFORMATION || cls == FILE_DISPOSITION_INFORMATION_EX) {
            if (disposition_wants_delete(cls, (const uint8_t *)info, length)) {
                return STATUS_ACCESS_DENIED;
            }
        } else if (rename_replaces(cls, (const uint8_t *)info, length)) {
            return STATUS_ACCESS_DENIED;
        }
        return g_nt_set_info(file, iosb, info, length, cls);
    }
    bytes = (uint8_t *)HeapAlloc(GetProcessHeap(), 0, length);
    path = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
    dest = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
    if (bytes == NULL || path == NULL || dest == NULL) {
        HeapFree(GetProcessHeap(), 0, bytes);
        HeapFree(GetProcessHeap(), 0, path);
        HeapFree(GetProcessHeap(), 0, dest);
        return STATUS_ACCESS_DENIED;
    }
    if (!memory_ok(info, length, 0)) {
        HeapFree(GetProcessHeap(), 0, bytes);
        HeapFree(GetProcessHeap(), 0, path);
        HeapFree(GetProcessHeap(), 0, dest);
        return STATUS_ACCESS_DENIED;
    }
    memcpy(bytes, info, length);
    if (bytes != NULL && (cls == FILE_DISPOSITION_INFORMATION || cls == FILE_DISPOSITION_INFORMATION_EX)) {
        if (disposition_clears_delete(cls, bytes, length)) {
            pending_clear(file);
        } else if (disposition_wants_delete(cls, bytes, length)) {
            path_from_handle(file, path, PATH_CAP);
            if (path[0] == L'\0') {
                status = STATUS_ACCESS_DENIED;
                handled = 1;
            } else if (is_skipped_path(path)) {
                handled = 0;
            } else if (!pending_put(file, path)) {
                /* Handle stays open, so the move has to wait until NtClose. */
                status = STATUS_ACCESS_DENIED;
                handled = 1;
            } else {
                set_iosb(iosb, STATUS_SUCCESS);
                status = STATUS_SUCCESS;
                handled = 1;
            }
        }
    }
    if (!handled && bytes != NULL
        && (cls == FILE_RENAME_INFORMATION || cls == FILE_RENAME_INFORMATION_EX)) {
        if (rename_replace_dest(cls, bytes, length, dest, PATH_CAP)) {
            path_from_handle(file, path, PATH_CAP);
            if (dest[0] != L'\0' && _wcsicmp(path, dest) != 0 && path_exists(dest) && !is_skipped_path(dest)
                && !soft_delete_path(dest)) {
                status = STATUS_ACCESS_DENIED;
                handled = 1;
            }
        }
    }
    if (!handled) {
        status = g_nt_set_info(file, iosb, info, length, cls);
    }
    HeapFree(GetProcessHeap(), 0, bytes);
    HeapFree(GetProcessHeap(), 0, path);
    HeapFree(GetProcessHeap(), 0, dest);
    return status;
}

static NTSTATUS NTAPI hook_NtCreateFile(
    HANDLE *file_handle, ACCESS_MASK access, OBJECT_ATTRIBUTES_ *oa, IO_STATUS_BLOCK_ *iosb,
    LARGE_INTEGER *alloc_size, ULONG attributes, ULONG share, ULONG disposition,
    ULONG options, PVOID ea, ULONG ea_length) {
    ULONG cleaned = options;
    wchar_t *path = NULL;
    NTSTATUS status;
    int strip;
    if (t_bypass) {
        return g_nt_create_file(
            file_handle, access, oa, iosb, alloc_size, attributes, share, disposition,
            options, ea, ea_length);
    }
    strip = (options & FILE_DELETE_ON_CLOSE) != 0;
    if (strip) {
        path = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
        if (path == NULL || !path_from_object(oa, path, PATH_CAP)) {
            HeapFree(GetProcessHeap(), 0, path);
            return STATUS_ACCESS_DENIED;
        }
        if (!is_skipped_path(path)) {
            cleaned = options & ~FILE_DELETE_ON_CLOSE;
        } else {
            strip = 0;
        }
    }
    status = g_nt_create_file(
        file_handle, access, oa, iosb, alloc_size, attributes, share, disposition,
        cleaned, ea, ea_length);
    if (status >= 0 && strip && file_handle != NULL && path != NULL) {
        HANDLE opened = NULL;
        if (memory_ok(file_handle, sizeof(*file_handle), 0)) {
            opened = *file_handle;
        }
        if (opened != NULL && !pending_put(opened, path)) {
            HeapFree(GetProcessHeap(), 0, path);
            g_nt_close(opened);
            if (memory_ok(file_handle, sizeof(*file_handle), 1)) {
                *file_handle = NULL;
            }
            set_iosb(iosb, STATUS_ACCESS_DENIED);
            return STATUS_ACCESS_DENIED;
        }
    }
    HeapFree(GetProcessHeap(), 0, path);
    return status;
}

static NTSTATUS NTAPI hook_NtOpenFile(
    HANDLE *file_handle, ACCESS_MASK access, OBJECT_ATTRIBUTES_ *oa, IO_STATUS_BLOCK_ *iosb,
    ULONG share, ULONG options) {
    ULONG cleaned = options;
    wchar_t *path = NULL;
    NTSTATUS status;
    int strip;
    if (t_bypass) {
        return g_nt_open_file(file_handle, access, oa, iosb, share, options);
    }
    strip = (options & FILE_DELETE_ON_CLOSE) != 0;
    if (strip) {
        path = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, PATH_CAP * sizeof(wchar_t));
        if (path == NULL || !path_from_object(oa, path, PATH_CAP)) {
            HeapFree(GetProcessHeap(), 0, path);
            return STATUS_ACCESS_DENIED;
        }
        if (!is_skipped_path(path)) {
            cleaned = options & ~FILE_DELETE_ON_CLOSE;
        } else {
            strip = 0;
        }
    }
    status = g_nt_open_file(file_handle, access, oa, iosb, share, cleaned);
    if (status >= 0 && strip && file_handle != NULL && path != NULL) {
        HANDLE opened = NULL;
        if (memory_ok(file_handle, sizeof(*file_handle), 0)) {
            opened = *file_handle;
        }
        if (opened != NULL && !pending_put(opened, path)) {
            HeapFree(GetProcessHeap(), 0, path);
            g_nt_close(opened);
            if (memory_ok(file_handle, sizeof(*file_handle), 1)) {
                *file_handle = NULL;
            }
            set_iosb(iosb, STATUS_ACCESS_DENIED);
            return STATUS_ACCESS_DENIED;
        }
    }
    HeapFree(GetProcessHeap(), 0, path);
    return status;
}

/* -------------------------------------------------------------------------- */
/* Child inject. The shellcode stores the full 64-bit module handle.         */
/* -------------------------------------------------------------------------- */

#define LOADER_PATH_OFF 0x100
#define LOADER_RESULT_OFF 0x80
#define LOADER_ERROR_OFF 0x88

static void patch_lea(uint8_t *code, int at, int target) {
    int32_t disp = (int32_t)(target - (at + 7));
    memcpy(code + at + 3, &disp, sizeof disp);
}

static int build_loader(uint8_t *code, uint64_t load_library, uint64_t get_last_error) {
    int lea_path;
    int lea_res;
    int jnz_at;
    int lea_err;
    int done;
    int n = 0;
    code[n++] = 0x48;
    code[n++] = 0x83;
    code[n++] = 0xEC;
    code[n++] = 0x28;
    lea_path = n;
    code[n++] = 0x48;
    code[n++] = 0x8D;
    code[n++] = 0x0D;
    code[n++] = 0;
    code[n++] = 0;
    code[n++] = 0;
    code[n++] = 0;
    code[n++] = 0x48;
    code[n++] = 0xB8;
    memcpy(code + n, &load_library, sizeof load_library);
    n += 8;
    code[n++] = 0xFF;
    code[n++] = 0xD0;
    lea_res = n;
    code[n++] = 0x48;
    code[n++] = 0x8D;
    code[n++] = 0x0D;
    code[n++] = 0;
    code[n++] = 0;
    code[n++] = 0;
    code[n++] = 0;
    code[n++] = 0x48;
    code[n++] = 0x89;
    code[n++] = 0x01;
    code[n++] = 0x48;
    code[n++] = 0x85;
    code[n++] = 0xC0;
    jnz_at = n;
    code[n++] = 0x75;
    code[n++] = 0x00;
    code[n++] = 0x48;
    code[n++] = 0xB8;
    memcpy(code + n, &get_last_error, sizeof get_last_error);
    n += 8;
    code[n++] = 0xFF;
    code[n++] = 0xD0;
    lea_err = n;
    code[n++] = 0x48;
    code[n++] = 0x8D;
    code[n++] = 0x0D;
    code[n++] = 0;
    code[n++] = 0;
    code[n++] = 0;
    code[n++] = 0;
    code[n++] = 0x89;
    code[n++] = 0x01;
    done = n;
    code[jnz_at + 1] = (uint8_t)(done - (jnz_at + 2));
    code[n++] = 0x31;
    code[n++] = 0xC0;
    code[n++] = 0x48;
    code[n++] = 0x83;
    code[n++] = 0xC4;
    code[n++] = 0x28;
    code[n++] = 0xC3;
    patch_lea(code, lea_path, LOADER_PATH_OFF);
    patch_lea(code, lea_res, LOADER_RESULT_OFF);
    patch_lea(code, lea_err, LOADER_ERROR_OFF);
    return n;
}

static int inject_dll(HANDLE process) {
    wchar_t self_path[PATH_CAP];
    uint8_t code[128];
    uint8_t *remote;
    SIZE_T written = 0;
    SIZE_T path_bytes;
    HANDLE thread = NULL;
    DWORD wait_rc;
    uint64_t loaded = 0;
    uint32_t remote_error = 0;
    BOOL wow = FALSE;
    HMODULE kernel;
    FARPROC load_library;
    FARPROC get_last_error;
    int code_len;
    DWORD exit_code = 0;
    if (GetModuleFileNameW(g_module, self_path, PATH_CAP) == 0) {
        return 0;
    }
    if (IsWow64Process(process, &wow) && wow) {
        return 0;
    }
    kernel = GetModuleHandleW(L"kernel32.dll");
    load_library = GetProcAddress(kernel, "LoadLibraryW");
    get_last_error = GetProcAddress(kernel, "GetLastError");
    if (load_library == NULL || get_last_error == NULL) {
        return 0;
    }
    code_len = build_loader(code, (uint64_t)(uintptr_t)load_library, (uint64_t)(uintptr_t)get_last_error);
    remote = (uint8_t *)VirtualAllocEx(
        process, NULL, 8192, MEM_RESERVE | MEM_COMMIT, PAGE_EXECUTE_READWRITE);
    if (remote == NULL) {
        return 0;
    }
    path_bytes = (wcslen(self_path) + 1) * sizeof(wchar_t);
    if (LOADER_PATH_OFF + path_bytes > 8192) {
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        return 0;
    }
    if (!WriteProcessMemory(process, remote, code, (SIZE_T)code_len, &written)
        || !WriteProcessMemory(process, remote + LOADER_PATH_OFF, self_path, path_bytes, &written)) {
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        return 0;
    }
    thread = CreateRemoteThread(process, NULL, 0, (LPTHREAD_START_ROUTINE)remote, NULL, 0, NULL);
    if (thread == NULL) {
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        return 0;
    }
    wait_rc = WaitForSingleObject(thread, 15000);
    GetExitCodeThread(thread, &exit_code);
    CloseHandle(thread);
    if (wait_rc != WAIT_OBJECT_0) {
        VirtualFreeEx(process, remote, 0, MEM_RELEASE);
        return 0;
    }
    ReadProcessMemory(process, remote + LOADER_RESULT_OFF, &loaded, sizeof loaded, &written);
    ReadProcessMemory(process, remote + LOADER_ERROR_OFF, &remote_error, sizeof remote_error, &written);
    VirtualFreeEx(process, remote, 0, MEM_RELEASE);
    (void)exit_code;
    (void)remote_error;
    return loaded != 0;
}

/* Any DLL mapped into Git Bash breaks its fork, so this process must not
 * inject into an image that ships next to msys-2.0.dll or cygwin1.dll.
 * The check is the file on disk: the caller itself is usually not MSYS. */
static int directory_has_leaf(const wchar_t *exe, const wchar_t *leaf) {
    wchar_t path[1024];
    const wchar_t *slash;
    size_t dir_chars;
    size_t leaf_chars;
    if (exe == NULL || leaf == NULL) {
        return 0;
    }
    slash = wcsrchr(exe, L'\\');
    if (slash == NULL) {
        return 0;
    }
    dir_chars = (size_t)(slash - exe);
    leaf_chars = wcslen(leaf);
    if (dir_chars + 1 + leaf_chars + 1 >= 1024) {
        return 0;
    }
    memcpy(path, exe, dir_chars * sizeof(wchar_t));
    path[dir_chars] = L'\\';
    memcpy(path + dir_chars + 1, leaf, (leaf_chars + 1) * sizeof(wchar_t));
    return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
}

static int image_is_posix(const wchar_t *exe) {
    return directory_has_leaf(exe, L"msys-2.0.dll")
        || directory_has_leaf(exe, L"cygwin1.dll");
}

/* conhost is created for every console process, including Git Bash.
 * A remote LoadLibrary at its initial breakpoint access-violates it,
 * and the parent then fails to start (0xC0000142). */
static int image_is_console_host(const wchar_t *exe) {
    const wchar_t *slash;
    const wchar_t *name;
    if (exe == NULL) {
        return 0;
    }
    slash = wcsrchr(exe, L'\\');
    name = (slash != NULL) ? slash + 1 : exe;
    return _wcsicmp(name, L"conhost.exe") == 0 || _wcsicmp(name, L"openconsole.exe") == 0;
}

static int parent_is_posix_runtime(void) {
    return GetModuleHandleW(L"msys-2.0.dll") != NULL
        || GetModuleHandleW(L"cygwin1.dll") != NULL;
}

static int first_command_token(const wchar_t *cmd, wchar_t *out, size_t cap) {
    size_t i = 0;
    size_t n = 0;
    if (cmd == NULL || out == NULL || cap < 2) {
        return 0;
    }
    while (cmd[i] == L' ' || cmd[i] == L'\t') {
        i++;
    }
    if (cmd[i] == L'"') {
        i++;
        while (cmd[i] != L'\0' && cmd[i] != L'"' && n + 1 < cap) {
            out[n++] = cmd[i++];
        }
    } else {
        while (cmd[i] != L'\0' && cmd[i] != L' ' && cmd[i] != L'\t' && n + 1 < cap) {
            out[n++] = cmd[i++];
        }
    }
    out[n] = L'\0';
    return n > 0;
}

static int child_is_posix_image(LPCWSTR app, LPCWSTR cmd) {
    wchar_t token[1024];
    wchar_t full[1024];
    DWORD n;
    const wchar_t *raw = app;
    if (raw == NULL || raw[0] == L'\0') {
        if (!first_command_token(cmd, token, 1024)) {
            return 0;
        }
        raw = token;
    }
    if (wcschr(raw, L'\\') == NULL && wcschr(raw, L'/') == NULL) {
        n = SearchPathW(NULL, raw, L".exe", 1024, full, NULL);
        if (n == 0 || n >= 1024) {
            return 0;
        }
        return image_is_posix(full);
    }
    n = GetFullPathNameW(raw, 1024, full, NULL);
    if (n > 0 && n < 1024) {
        return image_is_posix(full);
    }
    return image_is_posix(raw);
}

static void fail_created_process(LPPROCESS_INFORMATION pi) {
    if (pi == NULL) {
        return;
    }
    if (pi->hProcess != NULL) {
        TerminateProcess(pi->hProcess, 1);
        CloseHandle(pi->hProcess);
        pi->hProcess = NULL;
    }
    if (pi->hThread != NULL) {
        CloseHandle(pi->hThread);
        pi->hThread = NULL;
    }
    pi->dwProcessId = 0;
    pi->dwThreadId = 0;
    SetLastError(ERROR_ACCESS_DENIED);
}

/* A debug event has to be continued before the next one can be read.
 * LoadLibrary on a debuggee raises LOAD_DLL events, so the inject runs
 * on another thread while this thread keeps continuing those events. */
#define DEBUG_CHILD_MAX 64

typedef struct DebugChild {
    DWORD pid;
    HANDLE process;
    HANDLE thread;
    int armed;
} DebugChild;

/* One Git Bash and the processes it starts. Owned by the thread that
 * created that Bash, so one stuck tree cannot block the next command. */
typedef struct DebugTree {
    DWORD root_pid;
    HANDLE root_process;
    int root_exited;
    int live;
    DebugChild children[DEBUG_CHILD_MAX];
} DebugTree;

typedef struct InjectReq {
    HANDLE process;
    HANDLE done;
    int ok;
} InjectReq;

static DWORD WINAPI inject_worker(LPVOID arg) {
    InjectReq *req = (InjectReq *)arg;
    req->ok = inject_dll(req->process);
    SetEvent(req->done);
    return 0;
}

static int debug_continue_status(const DEBUG_EVENT *ev) {
    DWORD code;
    if (ev->dwDebugEventCode != EXCEPTION_DEBUG_EVENT) {
        return DBG_CONTINUE;
    }
    code = ev->u.Exception.ExceptionRecord.ExceptionCode;
    if (code == EXCEPTION_BREAKPOINT || code == EXCEPTION_SINGLE_STEP) {
        return DBG_CONTINUE;
    }
    return DBG_EXCEPTION_NOT_HANDLED;
}

static void forget_debug_child(DebugTree *tree, DWORD pid) {
    int i;
    for (i = 0; i < DEBUG_CHILD_MAX; i++) {
        if (tree->children[i].pid != pid) {
            continue;
        }
        if (tree->children[i].process != NULL) {
            CloseHandle(tree->children[i].process);
        }
        if (tree->children[i].thread != NULL) {
            CloseHandle(tree->children[i].thread);
        }
        memset(&tree->children[i], 0, sizeof(tree->children[i]));
        return;
    }
}

static int remember_debug_child(DebugTree *tree, DWORD pid, HANDLE process, HANDLE thread) {
    int i;
    HANDLE dup_process = NULL;
    HANDLE dup_thread = NULL;
    if (!DuplicateHandle(
            GetCurrentProcess(), process, GetCurrentProcess(), &dup_process,
            0, FALSE, DUPLICATE_SAME_ACCESS)
        || !DuplicateHandle(
            GetCurrentProcess(), thread, GetCurrentProcess(), &dup_thread,
            0, FALSE, DUPLICATE_SAME_ACCESS)) {
        if (dup_process != NULL) {
            CloseHandle(dup_process);
        }
        if (dup_thread != NULL) {
            CloseHandle(dup_thread);
        }
        return 0;
    }
    for (i = 0; i < DEBUG_CHILD_MAX; i++) {
        if (tree->children[i].pid != 0) {
            continue;
        }
        tree->children[i].pid = pid;
        tree->children[i].process = dup_process;
        tree->children[i].thread = dup_thread;
        tree->children[i].armed = 1;
        return 1;
    }
    CloseHandle(dup_process);
    CloseHandle(dup_thread);
    return 0;
}

static void close_create_handles(CREATE_PROCESS_DEBUG_INFO *info) {
    if (info->hFile != NULL) {
        CloseHandle(info->hFile);
        info->hFile = NULL;
    }
    if (info->hProcess != NULL) {
        CloseHandle(info->hProcess);
        info->hProcess = NULL;
    }
    if (info->hThread != NULL) {
        CloseHandle(info->hThread);
        info->hThread = NULL;
    }
}

/* 1 means do not inject: MSYS/Cygwin, a console host, or an unreadable image. */
static int created_image_skip_inject(HANDLE process, HANDLE file) {
    wchar_t path[1024];
    DWORD cap = 1024;
    DWORD n;
    const wchar_t *bare;
    path[0] = L'\0';
    if (file != NULL) {
        n = GetFinalPathNameByHandleW(file, path, 1024, 0);
        if (n == 0 || n >= 1024) {
            path[0] = L'\0';
        }
    }
    if (path[0] == L'\0' && process != NULL) {
        if (!QueryFullProcessImageNameW(process, 0, path, &cap)) {
            path[0] = L'\0';
        }
    }
    bare = path;
    if (wcsncmp(path, L"\\\\?\\", 4) == 0) {
        bare = path + 4;
    }
    if (bare[0] == L'\0') {
        log_line(L"softdelete: debug image path unavailable, leaving it unhooked");
        return 1;
    }
    return image_is_posix(bare) || image_is_console_host(bare);
}

static void on_debug_create(DebugTree *tree, DEBUG_EVENT *ev) {
    CREATE_PROCESS_DEBUG_INFO *info = &ev->u.CreateProcessInfo;
    int skip = created_image_skip_inject(info->hProcess, info->hFile);
    tree->live += 1;
    if (!skip && !remember_debug_child(tree, ev->dwProcessId, info->hProcess, info->hThread)) {
        TerminateProcess(info->hProcess, 1);
    }
    close_create_handles(info);
}

static int dispatch_debug_event(DebugTree *tree, DEBUG_EVENT *ev);

static int inject_at_breakpoint(DebugTree *tree, DebugChild *slot, DEBUG_EVENT *held) {
    InjectReq req;
    HANDLE worker;
    memset(&req, 0, sizeof req);
    req.process = slot->process;
    req.done = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (req.done == NULL) {
        TerminateProcess(slot->process, 1);
        return 0;
    }
    ContinueDebugEvent(held->dwProcessId, held->dwThreadId, DBG_CONTINUE);
    worker = CreateThread(NULL, 0, inject_worker, &req, 0, NULL);
    if (worker == NULL) {
        req.ok = 0;
        SetEvent(req.done);
    }
    while (WaitForSingleObject(req.done, 0) != WAIT_OBJECT_0) {
        DEBUG_EVENT nested;
        if (!WaitForDebugEvent(&nested, 100)) {
            continue;
        }
        if (!dispatch_debug_event(tree, &nested)) {
            ContinueDebugEvent(
                nested.dwProcessId, nested.dwThreadId, debug_continue_status(&nested));
        }
    }
    if (worker != NULL) {
        WaitForSingleObject(worker, 20000);
        CloseHandle(worker);
    }
    CloseHandle(req.done);
    if (!req.ok) {
        TerminateProcess(slot->process, 1);
    }
    if (slot->process != NULL) {
        CloseHandle(slot->process);
        slot->process = NULL;
    }
    if (slot->thread != NULL) {
        CloseHandle(slot->thread);
        slot->thread = NULL;
    }
    return 1;
}

static int dispatch_debug_event(DebugTree *tree, DEBUG_EVENT *ev) {
    int i;
    if (ev->dwDebugEventCode == CREATE_PROCESS_DEBUG_EVENT) {
        on_debug_create(tree, ev);
        return 0;
    }
    if (ev->dwDebugEventCode == EXIT_PROCESS_DEBUG_EVENT) {
        forget_debug_child(tree, ev->dwProcessId);
        if (tree->live > 0) {
            tree->live -= 1;
        }
        if (ev->dwProcessId == tree->root_pid) {
            tree->root_exited = 1;
        }
        return 0;
    }
    if (ev->dwDebugEventCode == LOAD_DLL_DEBUG_EVENT && ev->u.LoadDll.hFile != NULL) {
        CloseHandle(ev->u.LoadDll.hFile);
        ev->u.LoadDll.hFile = NULL;
        return 0;
    }
    if (ev->dwDebugEventCode == CREATE_THREAD_DEBUG_EVENT && ev->u.CreateThread.hThread != NULL) {
        CloseHandle(ev->u.CreateThread.hThread);
        ev->u.CreateThread.hThread = NULL;
        return 0;
    }
    if (ev->dwDebugEventCode != EXCEPTION_DEBUG_EVENT) {
        return 0;
    }
    if (ev->u.Exception.ExceptionRecord.ExceptionCode != EXCEPTION_BREAKPOINT
        || !ev->u.Exception.dwFirstChance) {
        return 0;
    }
    for (i = 0; i < DEBUG_CHILD_MAX; i++) {
        if (tree->children[i].pid == ev->dwProcessId && tree->children[i].armed) {
            tree->children[i].armed = 0;
            return inject_at_breakpoint(tree, &tree->children[i], ev);
        }
    }
    return 0;
}

/* The root can be killed while still suspended, before it reports any
 * debug event. The process handle is then the only sign that it is gone. */
static int debug_tree_finished(DebugTree *tree) {
    if (tree->live > 0) {
        return 0;
    }
    if (tree->root_exited) {
        return 1;
    }
    return tree->root_process != NULL
        && WaitForSingleObject(tree->root_process, 0) == WAIT_OBJECT_0;
}

/* CreateProcessW is what actually attaches the debugger. Setting
 * DEBUG_PROCESS only inside CreateProcessInternalW does not, and it
 * leaves Git Bash stuck. Each Bash is created on its own thread, so the
 * debug port belongs to that thread and one tree cannot block the next. */
static SOFT_TLS int t_worker_creating;

typedef struct PosixStart {
    HANDLE token;
    LPCWSTR app;
    LPWSTR cmd;
    LPSECURITY_ATTRIBUTES process_attr;
    LPSECURITY_ATTRIBUTES thread_attr;
    BOOL inherit;
    DWORD flags;
    LPVOID env;
    LPCWSTR cwd;
    LPSTARTUPINFOW startup;
    LPPROCESS_INFORMATION pi_out;
    BOOL ok;
    DWORD error;
    HANDLE created;
} PosixStart;

static DWORD WINAPI posix_debug_thread(LPVOID arg) {
    PosixStart *req = (PosixStart *)arg;
    DebugTree tree;
    PROCESS_INFORMATION *pi;
    DWORD used;
    memset(&tree, 0, sizeof tree);
    DebugSetProcessKillOnExit(FALSE);
    used = req->flags | DEBUG_PROCESS;
    t_worker_creating = 1;
    if (req->token == NULL) {
        req->ok = CreateProcessW(
            req->app, req->cmd, req->process_attr, req->thread_attr, req->inherit, used,
            req->env, req->cwd, req->startup, req->pi_out);
    } else {
        req->ok = CreateProcessAsUserW(
            req->token, req->app, req->cmd, req->process_attr, req->thread_attr,
            req->inherit, used, req->env, req->cwd, req->startup, req->pi_out);
    }
    req->error = GetLastError();
    t_worker_creating = 0;
    pi = req->pi_out;
    if (!req->ok || pi == NULL || pi->hProcess == NULL) {
        req->ok = FALSE;
        if (req->error == 0) {
            req->error = ERROR_INVALID_PARAMETER;
        }
        SetEvent(req->created);
        return 0;
    }
    tree.root_pid = pi->dwProcessId;
    if (!DuplicateHandle(
            GetCurrentProcess(), pi->hProcess, GetCurrentProcess(), &tree.root_process,
            0, FALSE, DUPLICATE_SAME_ACCESS)) {
        TerminateProcess(pi->hProcess, 1);
        CloseHandle(pi->hProcess);
        if (pi->hThread != NULL) {
            CloseHandle(pi->hThread);
        }
        pi->hProcess = NULL;
        pi->hThread = NULL;
        pi->dwProcessId = 0;
        pi->dwThreadId = 0;
        req->ok = FALSE;
        req->error = ERROR_NOT_ENOUGH_MEMORY;
        SetEvent(req->created);
        return 0;
    }
    SetEvent(req->created);
    while (!debug_tree_finished(&tree)) {
        DEBUG_EVENT ev;
        if (!WaitForDebugEvent(&ev, 50)) {
            continue;
        }
        if (!dispatch_debug_event(&tree, &ev)) {
            ContinueDebugEvent(ev.dwProcessId, ev.dwThreadId, debug_continue_status(&ev));
        }
    }
    CloseHandle(tree.root_process);
    return 0;
}

static BOOL submit_posix_create(
    HANDLE token, LPCWSTR app, LPWSTR cmd, LPSECURITY_ATTRIBUTES process_attr,
    LPSECURITY_ATTRIBUTES thread_attr, BOOL inherit, DWORD flags, LPVOID env, LPCWSTR cwd,
    LPSTARTUPINFOW startup, LPPROCESS_INFORMATION pi) {
    PosixStart req;
    HANDLE thread;
    HANDLE waits[2];
    DWORD which;
    if (pi == NULL) {
        return FALSE;
    }
    memset(&req, 0, sizeof req);
    req.token = token;
    req.app = app;
    req.cmd = cmd;
    req.process_attr = process_attr;
    req.thread_attr = thread_attr;
    req.inherit = inherit;
    req.flags = flags;
    req.env = env;
    req.cwd = cwd;
    req.startup = startup;
    req.pi_out = pi;
    req.created = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (req.created == NULL) {
        return FALSE;
    }
    thread = CreateThread(NULL, 0, posix_debug_thread, &req, 0, NULL);
    if (thread == NULL) {
        CloseHandle(req.created);
        return FALSE;
    }
    waits[0] = req.created;
    waits[1] = thread;
    which = WaitForMultipleObjects(2, waits, FALSE, INFINITE);
    CloseHandle(thread);
    CloseHandle(req.created);
    if (which != WAIT_OBJECT_0) {
        SetLastError(ERROR_ACCESS_DENIED);
        return FALSE;
    }
    if (!req.ok) {
        SetLastError(req.error);
    }
    return req.ok;
}

static int env_block_has(const wchar_t *block, const wchar_t *key) {
    size_t key_len = wcslen(key);
    const wchar_t *cursor;
    if (block == NULL) {
        return 0;
    }
    for (cursor = block; *cursor != L'\0'; cursor += wcslen(cursor) + 1) {
        if (_wcsnicmp(cursor, key, key_len) == 0 && cursor[key_len] == L'=') {
            return 1;
        }
    }
    return 0;
}

static wchar_t *parent_env_value(const wchar_t *key) {
    DWORD n = GetEnvironmentVariableW(key, NULL, 0);
    wchar_t *value;
    if (n <= 1) {
        return NULL;
    }
    value = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, n * sizeof(wchar_t));
    if (value == NULL || GetEnvironmentVariableW(key, value, n) == 0) {
        HeapFree(GetProcessHeap(), 0, value);
        return NULL;
    }
    return value;
}

/* CreateProcess without CREATE_UNICODE_ENVIRONMENT passes an ANSI block. */
static wchar_t *ansi_env_to_wide(const char *block) {
    const char *cursor = block;
    size_t bytes;
    int chars;
    wchar_t *wide;
    if (block == NULL) {
        return NULL;
    }
    if (*cursor == '\0') {
        bytes = 1;
    } else {
        while (*cursor != '\0') {
            cursor += strlen(cursor) + 1;
        }
        bytes = (size_t)(cursor - block) + 1;
    }
    if (bytes == 0 || bytes > 0x7fffffff) {
        return NULL;
    }
    chars = MultiByteToWideChar(CP_ACP, 0, block, (int)bytes, NULL, 0);
    if (chars <= 0) {
        return NULL;
    }
    wide = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, (size_t)chars * sizeof(wchar_t));
    if (wide == NULL) {
        return NULL;
    }
    if (MultiByteToWideChar(CP_ACP, 0, block, (int)bytes, wide, chars) != chars) {
        HeapFree(GetProcessHeap(), 0, wide);
        return NULL;
    }
    return wide;
}

/* A sandboxed child that builds its own environment must still see the recycle port. */
static wchar_t *merge_child_soft_env(const wchar_t *block) {
    static const wchar_t *keys[] = {
        ENV_ENABLED, ENV_ARCHIVE, ENV_DLL, ENV_RECYCLE_PORT, ENV_RECYCLE_TOKEN,
    };
    wchar_t *values[5];
    size_t base = 1;
    size_t extra = 0;
    size_t pos = 0;
    const wchar_t *cursor;
    wchar_t *out;
    int i;
    memset(values, 0, sizeof values);
    for (cursor = block; *cursor != L'\0'; cursor += wcslen(cursor) + 1) {
        base += wcslen(cursor) + 1;
    }
    for (i = 0; i < 5; i++) {
        if (env_block_has(block, keys[i])) {
            continue;
        }
        values[i] = parent_env_value(keys[i]);
        if (values[i] != NULL) {
            extra += wcslen(keys[i]) + 1 + wcslen(values[i]) + 1;
        }
    }
    if (extra == 0) {
        return NULL;
    }
    out = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, (base + extra) * sizeof(wchar_t));
    if (out == NULL) {
        for (i = 0; i < 5; i++) {
            HeapFree(GetProcessHeap(), 0, values[i]);
        }
        return NULL;
    }
    for (cursor = block; *cursor != L'\0'; cursor += wcslen(cursor) + 1) {
        size_t count = wcslen(cursor) + 1;
        memcpy(out + pos, cursor, count * sizeof(wchar_t));
        pos += count;
    }
    for (i = 0; i < 5; i++) {
        int wrote;
        if (values[i] == NULL) {
            continue;
        }
        wrote = swprintf_s(out + pos, base + extra - pos, L"%s=%s", keys[i], values[i]);
        HeapFree(GetProcessHeap(), 0, values[i]);
        values[i] = NULL;
        if (wrote < 0) {
            for (; i < 5; i++) {
                HeapFree(GetProcessHeap(), 0, values[i]);
            }
            HeapFree(GetProcessHeap(), 0, out);
            return NULL;
        }
        pos += (size_t)wrote + 1;
    }
    out[pos] = L'\0';
    return out;
}

static BOOL WINAPI hook_CreateProcessInternalW(
    HANDLE token, LPCWSTR app, LPWSTR cmd, LPSECURITY_ATTRIBUTES process_attr,
    LPSECURITY_ATTRIBUTES thread_attr, BOOL inherit, DWORD flags, LPVOID env,
    LPCWSTR cwd, LPSTARTUPINFOW startup, LPPROCESS_INFORMATION pi, HANDLE *new_token) {
    DWORD used = flags;
    int added_suspend = 0;
    BOOL ok;
    wchar_t *owned_env = NULL;
    wchar_t *converted_env = NULL;
    const wchar_t *wide_env;
    LPVOID use_env = env;
    if (t_worker_creating || parent_is_posix_runtime()) {
        return g_create_process(
            token, app, cmd, process_attr, thread_attr, inherit, flags, env, cwd, startup, pi,
            new_token);
    }
    if (GetEnvironmentVariableW(ENV_RECYCLE_PORT, NULL, 0) > 1 && env != NULL) {
        wide_env = (const wchar_t *)env;
        if ((flags & CREATE_UNICODE_ENVIRONMENT) == 0) {
            converted_env = ansi_env_to_wide((const char *)env);
            if (converted_env == NULL) {
                log_line(L"softdelete: ANSI environment could not be converted, child was not started");
                SetLastError(ERROR_INVALID_PARAMETER);
                return FALSE;
            }
            wide_env = converted_env;
        }
        owned_env = merge_child_soft_env(wide_env);
        if (owned_env == NULL && !env_block_has(wide_env, ENV_RECYCLE_PORT)) {
            HeapFree(GetProcessHeap(), 0, converted_env);
            SetLastError(ERROR_NOT_ENOUGH_MEMORY);
            return FALSE;
        }
        if (owned_env != NULL) {
            use_env = owned_env;
            HeapFree(GetProcessHeap(), 0, converted_env);
            converted_env = NULL;
        } else if (converted_env != NULL) {
            use_env = converted_env;
            owned_env = converted_env;
            converted_env = NULL;
        }
        used |= CREATE_UNICODE_ENVIRONMENT;
    }
    if (child_is_posix_image(app, cmd)) {
        ok = submit_posix_create(
            token, app, cmd, process_attr, thread_attr, inherit, used, use_env, cwd, startup, pi);
        HeapFree(GetProcessHeap(), 0, owned_env);
        if (!ok) {
            log_line(L"softdelete: debug start failed, bash child was not started");
            return FALSE;
        }
        return TRUE;
    }
    if ((used & CREATE_SUSPENDED) == 0) {
        used |= CREATE_SUSPENDED;
        added_suspend = 1;
    }
    ok = g_create_process(
        token, app, cmd, process_attr, thread_attr, inherit, used, use_env, cwd, startup, pi,
        new_token);
    HeapFree(GetProcessHeap(), 0, owned_env);
    if (!ok || pi == NULL || pi->hProcess == NULL) {
        return ok;
    }
    if (!inject_dll(pi->hProcess)) {
        fail_created_process(pi);
        return FALSE;
    }
    if (added_suspend) {
        if (ResumeThread(pi->hThread) == (DWORD)-1) {
            fail_created_process(pi);
            return FALSE;
        }
    }
    return TRUE;
}

static int directory_is_empty(const wchar_t *path) {
    wchar_t *pattern;
    HANDLE find;
    WIN32_FIND_DATAW data;
    int empty = 1;
    size_t need = wcslen(path) + 3;
    pattern = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, need * sizeof(wchar_t));
    if (pattern == NULL || swprintf_s(pattern, need, L"%s\\*", path) < 0) {
        HeapFree(GetProcessHeap(), 0, pattern);
        return 0;
    }
    find = FindFirstFileExW(
        pattern, FindExInfoBasic, &data, FindExSearchNameMatch, NULL, FIND_FIRST_EX_LARGE_FETCH);
    HeapFree(GetProcessHeap(), 0, pattern);
    if (find == INVALID_HANDLE_VALUE) {
        return 0;
    }
    do {
        if (wcscmp(data.cFileName, L".") != 0 && wcscmp(data.cFileName, L"..") != 0) {
            empty = 0;
            break;
        }
    } while (FindNextFileW(find, &data));
    FindClose(find);
    return empty;
}

static BOOL WINAPI hook_DeleteFileW(LPCWSTR path) {
    DWORD attr;
    if (t_bypass) {
        return g_delete_file(path);
    }
    if (path == NULL || path[0] == L'\0') {
        return g_delete_file(path);
    }
    attr = GetFileAttributesW(path);
    if (attr == INVALID_FILE_ATTRIBUTES) {
        SetLastError(ERROR_FILE_NOT_FOUND);
        return FALSE;
    }
    /* DeleteFileW does not remove directories. Leave that to RemoveDirectoryW. */
    if ((attr & FILE_ATTRIBUTE_DIRECTORY) != 0 && (attr & FILE_ATTRIBUTE_REPARSE_POINT) == 0) {
        return g_delete_file(path);
    }
    if (is_skipped_path(path)) {
        return g_delete_file(path);
    }
    if (!soft_delete_path(path)) {
        SetLastError(ERROR_ACCESS_DENIED);
        return FALSE;
    }
    return TRUE;
}

static BOOL WINAPI hook_RemoveDirectoryW(LPCWSTR path) {
    DWORD attr;
    if (t_bypass) {
        return g_remove_directory(path);
    }
    if (path == NULL || path[0] == L'\0') {
        return g_remove_directory(path);
    }
    attr = GetFileAttributesW(path);
    if (attr == INVALID_FILE_ATTRIBUTES || (attr & FILE_ATTRIBUTE_DIRECTORY) == 0) {
        return g_remove_directory(path);
    }
    if (is_skipped_path(path)) {
        return g_remove_directory(path);
    }
    if ((attr & FILE_ATTRIBUTE_REPARSE_POINT) == 0 && !directory_is_empty(path)) {
        SetLastError(ERROR_DIR_NOT_EMPTY);
        return FALSE;
    }
    if (!soft_delete_path(path)) {
        SetLastError(ERROR_ACCESS_DENIED);
        return FALSE;
    }
    return TRUE;
}

static void write_load_error(const wchar_t *text) {
    wchar_t dir[MAX_PATH];
    wchar_t path[MAX_PATH];
    HANDLE file;
    DWORD written = 0;
    char utf8[1024];
    int n;
    OutputDebugStringW(text);
    if (GetTempPathW(MAX_PATH, dir) == 0) {
        return;
    }
    if (swprintf_s(path, MAX_PATH, L"%sjiuwen-softdelete-error.txt", dir) < 0) {
        return;
    }
    file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) {
        return;
    }
    n = WideCharToMultiByte(CP_UTF8, 0, text, -1, utf8, (int)sizeof utf8, NULL, NULL);
    if (n > 1) {
        WriteFile(file, utf8, (DWORD)(n - 1), &written, NULL);
    }
    CloseHandle(file);
}

static int install_all(void) {
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    HMODULE kernelbase = GetModuleHandleW(L"kernelbase.dll");
    void *nt_close;
    void *nt_delete;
    void *nt_set;
    void *nt_create;
    void *nt_open;
    void *create_process;
    void *delete_file;
    void *remove_directory;
    if (ntdll == NULL || kernelbase == NULL) {
        write_load_error(L"ntdll or kernelbase is not loaded");
        return 0;
    }
    nt_close = (void *)GetProcAddress(ntdll, "NtClose");
    nt_delete = (void *)GetProcAddress(ntdll, "NtDeleteFile");
    nt_set = (void *)GetProcAddress(ntdll, "NtSetInformationFile");
    nt_create = (void *)GetProcAddress(ntdll, "NtCreateFile");
    nt_open = (void *)GetProcAddress(ntdll, "NtOpenFile");
    create_process = (void *)GetProcAddress(kernelbase, "CreateProcessInternalW");
    delete_file = (void *)GetProcAddress(kernelbase, "DeleteFileW");
    remove_directory = (void *)GetProcAddress(kernelbase, "RemoveDirectoryW");
    if (nt_close == NULL || nt_delete == NULL || nt_set == NULL || nt_create == NULL
        || nt_open == NULL || create_process == NULL || delete_file == NULL
        || remove_directory == NULL) {
        write_load_error(L"required export is missing");
        return 0;
    }
    t_bypass = 1;
    if (!install_hook(nt_close, (void *)hook_NtClose)) {
        goto fail;
    }
    g_nt_close = (FnNtClose)g_hooks[g_hook_count - 1].trampoline;
    if (!install_hook(nt_delete, (void *)hook_NtDeleteFile)) {
        goto fail;
    }
    g_nt_delete_file = (FnNtDeleteFile)g_hooks[g_hook_count - 1].trampoline;
    if (!install_hook(nt_set, (void *)hook_NtSetInformationFile)) {
        goto fail;
    }
    g_nt_set_info = (FnNtSetInformationFile)g_hooks[g_hook_count - 1].trampoline;
    if (!install_hook(nt_create, (void *)hook_NtCreateFile)) {
        goto fail;
    }
    g_nt_create_file = (FnNtCreateFile)g_hooks[g_hook_count - 1].trampoline;
    if (!install_hook(nt_open, (void *)hook_NtOpenFile)) {
        goto fail;
    }
    g_nt_open_file = (FnNtOpenFile)g_hooks[g_hook_count - 1].trampoline;
    if (!install_hook(create_process, (void *)hook_CreateProcessInternalW)) {
        goto fail;
    }
    g_create_process = (FnCreateProcessInternalW)g_hooks[g_hook_count - 1].trampoline;
    if (!install_hook(delete_file, (void *)hook_DeleteFileW)) {
        goto fail;
    }
    g_delete_file = (BOOL(WINAPI *)(LPCWSTR))g_hooks[g_hook_count - 1].trampoline;
    if (!install_hook(remove_directory, (void *)hook_RemoveDirectoryW)) {
        goto fail;
    }
    g_remove_directory = (BOOL(WINAPI *)(LPCWSTR))g_hooks[g_hook_count - 1].trampoline;
    t_bypass = 0;
    return 1;
fail:
    remove_hooks();
    t_bypass = 0;
    write_load_error(L"inline hook could not be installed");
    return 0;
}

/* box-server sets this on the thread that calls SHFileOperation, so the
 * hook does not intercept the recycle it was asked to perform. */
SOFT_EXPORT void jiuwen_softdelete_set_bypass(int on) {
    t_bypass = on ? 1 : 0;
}

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID reserved) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = instance;
        DisableThreadLibraryCalls(instance);
        InitializeCriticalSection(&g_lock);
        g_lock_ready = 1;
        if (!install_all()) {
            if (g_lock_ready) {
                DeleteCriticalSection(&g_lock);
                g_lock_ready = 0;
            }
            return FALSE;
        }
    } else if (reason == DLL_PROCESS_DETACH && reserved == NULL) {
        remove_hooks();
        if (g_lock_ready) {
            DeleteCriticalSection(&g_lock);
            g_lock_ready = 0;
        }
    }
    return TRUE;
}
