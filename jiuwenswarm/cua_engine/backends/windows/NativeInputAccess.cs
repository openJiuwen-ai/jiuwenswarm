using System;
using System.Runtime.InteropServices;

namespace ComputerUse.Windows {
    // SendInput's accepted count does not establish delivery across UIPI.
    // Check known integrity boundaries before dispatch, without elevation,
    // token changes or changing the user's foreground as a workaround.
    internal static class NativeInputAccess {
        [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        [DllImport("advapi32.dll")] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
        [DllImport("advapi32.dll")] static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr data, int size, out int required);
        [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
        [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);

        sealed class Identity { internal int Level; internal bool UiAccess; }
        static Identity Read(IntPtr process) {
            IntPtr token;
            if (process == IntPtr.Zero || !OpenProcessToken(process, 8, out token)) return null;
            try {
                int needed;
                GetTokenInformation(token, 25, IntPtr.Zero, 0, out needed); // TokenIntegrityLevel
                if (needed < IntPtr.Size || needed > 4096) return null;
                IntPtr buffer = Marshal.AllocHGlobal(Math.Max(needed, 4));
                try {
                    if (!GetTokenInformation(token, 25, buffer, needed, out needed)) return null;
                    IntPtr sid = Marshal.ReadIntPtr(buffer);
                    if (sid == IntPtr.Zero) return null;
                    IntPtr countPointer = GetSidSubAuthorityCount(sid);
                    if (countPointer == IntPtr.Zero) return null;
                    byte count = Marshal.ReadByte(countPointer);
                    if (count == 0) return null;
                    IntPtr level = GetSidSubAuthority(sid, (uint)(count - 1));
                    if (level == IntPtr.Zero) return null;
                    var identity = new Identity { Level = Marshal.ReadInt32(level) };
                    // TokenUIAccess is independent of administrator elevation.
                    if (!GetTokenInformation(token, 26, buffer, 4, out needed)) return null;
                    identity.UiAccess = Marshal.ReadInt32(buffer) != 0;
                    return identity;
                } finally { Marshal.FreeHGlobal(buffer); }
            } finally { CloseHandle(token); }
        }
        internal static void RequireForeground() { RequireWindow(Native.GetForegroundWindow(), "foreground window"); }
        internal static void RequireWindow(IntPtr window, string description = "input target") {
            if (window == IntPtr.Zero) return;
            var own = Read(GetCurrentProcess());
            if (own == null || own.UiAccess) return;
            uint pid = Native.PID(window);
            IntPtr process = OpenProcess(0x1000, false, pid); // QUERY_LIMITED_INFORMATION
            if (process == IntPtr.Zero) return; // Unknown is not proof of a mismatch.
            Identity target;
            try { target = Read(process); } finally { CloseHandle(process); }
            if (target != null && target.Level > own.Level)
                throw new Failure("input_unavailable", description + " (PID " + pid + ") runs at a higher integrity level than the input driver; Windows UIPI can block injected input. Use a non-elevated target or switch away from the elevated foreground window, then read again.");
        }
    }
}
