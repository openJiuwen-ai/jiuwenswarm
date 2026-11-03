using System;
using System.Collections.Generic;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Automation;
using System.Windows.Forms;

namespace ComputerUse.Windows {
    internal static class Native {
        [StructLayout(LayoutKind.Sequential)] internal struct RECT { internal int Left, Top, Right, Bottom; internal Rectangle Box { get { return Rectangle.FromLTRB(Left, Top, Right, Bottom); } } }
        [StructLayout(LayoutKind.Sequential)] internal struct POINT { internal int X, Y; }
        [StructLayout(LayoutKind.Sequential)] internal struct GUI { internal int Size; internal uint Flags; internal IntPtr Active, Focus, Capture, MenuOwner, MoveSize, Caret; internal RECT CaretRect; }
        [StructLayout(LayoutKind.Sequential)] internal struct INPUT { internal uint Type; internal UNION Data; }
        [StructLayout(LayoutKind.Explicit)] internal struct UNION { [FieldOffset(0)] internal MOUSE Mouse; [FieldOffset(0)] internal KEY Keyboard; }
        [StructLayout(LayoutKind.Sequential)] internal struct MOUSE { internal int X, Y; internal uint Data, Flags, Time; internal UIntPtr Extra; }
        [StructLayout(LayoutKind.Sequential)] internal struct KEY { internal ushort Code, Scan; internal uint Flags, Time; internal UIntPtr Extra; }
        internal delegate bool EnumProc(IntPtr hwnd, IntPtr arg);
        internal delegate void EventProc(IntPtr hook, uint kind, IntPtr hwnd, int objectId, int childId, uint thread, uint time);
        [DllImport("user32.dll")] internal static extern bool EnumWindows(EnumProc proc, IntPtr arg);
        [DllImport("user32.dll")] internal static extern bool IsWindow(IntPtr h);
        [DllImport("user32.dll")] internal static extern bool IsWindowVisible(IntPtr h);
        [DllImport("user32.dll")] internal static extern bool IsWindowEnabled(IntPtr h);
        [DllImport("user32.dll")] internal static extern bool IsIconic(IntPtr h);
        [DllImport("user32.dll")] internal static extern bool IsChild(IntPtr parent, IntPtr child);
        [DllImport("user32.dll")] internal static extern IntPtr GetAncestor(IntPtr h, uint flags);
        [DllImport("user32.dll")] internal static extern IntPtr GetWindow(IntPtr h, uint command);
        [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern int GetWindowText(IntPtr h, StringBuilder text, int length);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern int GetClassName(IntPtr h, StringBuilder text, int length);
        [DllImport("user32.dll")] internal static extern bool GetWindowRect(IntPtr h, out RECT r);
        [DllImport("user32.dll")] internal static extern bool GetClientRect(IntPtr h, out RECT r);
        [DllImport("user32.dll")] internal static extern bool ClientToScreen(IntPtr h, ref POINT p);
        [DllImport("user32.dll")] internal static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
        [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr h);
        [DllImport("user32.dll")] internal static extern bool ShowWindow(IntPtr h, int command);
        [DllImport("user32.dll")] internal static extern bool GetGUIThreadInfo(uint thread, ref GUI info);
        [DllImport("kernel32.dll")] internal static extern uint GetCurrentThreadId();
        [DllImport("user32.dll")] internal static extern IntPtr WindowFromPoint(POINT point);
        [DllImport("user32.dll")] internal static extern bool GetCursorPos(out POINT point);
        [DllImport("user32.dll")] internal static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")] internal static extern short GetAsyncKeyState(int key);
        [DllImport("user32.dll", SetLastError = true)] internal static extern uint SendInput(uint count, INPUT[] inputs, int size);
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] internal static extern IntPtr SendMessageTimeout(IntPtr h, uint message, IntPtr w, string text, uint flags, uint timeout, out UIntPtr result);
        [DllImport("user32.dll", EntryPoint = "SendMessageTimeoutW", SetLastError = true)] internal static extern IntPtr SendMessageTimeoutRaw(IntPtr h, uint message, IntPtr w, IntPtr l, uint flags, uint timeout, out UIntPtr result);
        [DllImport("user32.dll")] internal static extern uint GetClipboardSequenceNumber();
        [DllImport("user32.dll")] internal static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
        [DllImport("user32.dll")] internal static extern bool CloseDesktop(IntPtr desktop);
        [DllImport("user32.dll")] internal static extern IntPtr GetThreadDesktop(uint thread);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] internal static extern bool GetUserObjectInformation(IntPtr handle, int index, StringBuilder value, int length, out uint required);
        [DllImport("user32.dll")] internal static extern bool SetProcessDPIAware();
        [DllImport("user32.dll")] internal static extern bool SetProcessDpiAwarenessContext(IntPtr context);
        [DllImport("user32.dll")] internal static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
        [DllImport("user32.dll")] internal static extern IntPtr GetWindowDpiAwarenessContext(IntPtr h);
        [DllImport("dwmapi.dll")] internal static extern int DwmGetWindowAttribute(IntPtr h, uint attr, out int value, int size);
        [DllImport("user32.dll")] internal static extern IntPtr SetWinEventHook(uint min, uint max, IntPtr module, EventProc callback, uint process, uint thread, uint flags);
        [DllImport("user32.dll")] internal static extern bool UnhookWinEvent(IntPtr hook);
        static readonly Dictionary<IntPtr, long> generations = new Dictionary<IntPtr, long>();
        internal static volatile bool GenerationTrackingAvailable;
        static EventProc destroy;
        internal static void Initialize() {
            try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { SetProcessDPIAware(); }
            var ready = new ManualResetEvent(false);
            var thread = new Thread(() => {
                destroy = (hook, kind, h, obj, child, tid, time) => { if (obj == 0 && child == 0) lock (generations) { long n; generations.TryGetValue(h, out n); generations[h] = n + 1; } };
                var subscription = SetWinEventHook(0x8001, 0x8001, IntPtr.Zero, destroy, 0, 0, 0);
                GenerationTrackingAvailable = subscription != IntPtr.Zero;
                ready.Set();
                try { Application.Run(); }
                finally { GenerationTrackingAvailable = false; if (subscription != IntPtr.Zero) UnhookWinEvent(subscription); }
            });
            thread.IsBackground = true; thread.SetApartmentState(ApartmentState.STA); thread.Start(); ready.WaitOne();
        }
        internal static long Generation(IntPtr h) { lock (generations) { long n; generations.TryGetValue(h, out n); return n; } }
        internal static Dictionary<IntPtr, long> GenerationSnapshot() { lock (generations) return new Dictionary<IntPtr, long>(generations); }
        internal static bool Interactive() {
            var input = OpenInputDesktop(0, false, 1); if (input == IntPtr.Zero) return false;
            try {
                var a = new StringBuilder(256); var b = new StringBuilder(256); uint size;
                return GetUserObjectInformation(input, 2, a, 512, out size) && GetUserObjectInformation(GetThreadDesktop(GetCurrentThreadId()), 2, b, 512, out size)
                    && a.ToString() == b.ToString() && GetForegroundWindow() != IntPtr.Zero;
            } finally { CloseDesktop(input); }
        }
        internal static string Title(IntPtr h) { var b = new StringBuilder(4096); GetWindowText(h, b, b.Capacity); return b.ToString(); }
        internal static string Class(IntPtr h) { var b = new StringBuilder(256); GetClassName(h, b, b.Capacity); return b.ToString(); }
        internal static bool IsEdit(IntPtr h) { var name = Class(h); return name.Equals("Edit", StringComparison.OrdinalIgnoreCase) || name.StartsWith("WindowsForms10.EDIT.", StringComparison.OrdinalIgnoreCase); }
        internal static Rectangle Bounds(IntPtr h) { RECT r; if (!GetWindowRect(h, out r) || r.Right <= r.Left || r.Bottom <= r.Top) throw new Failure("stale_target", "window geometry unavailable"); return r.Box; }
        internal static uint PID(IntPtr h) { uint id; GetWindowThreadProcessId(h, out id); return id; }
        internal static bool Inside(IntPtr window, IntPtr child) { return child == window || (child != IntPtr.Zero && IsChild(window, child)); }
        internal static IntPtr Focus(IntPtr h) { uint pid; var info = new GUI { Size = Marshal.SizeOf(typeof(GUI)) }; return GetGUIThreadInfo(GetWindowThreadProcessId(h, out pid), ref info) ? info.Focus : IntPtr.Zero; }
        internal static List<IntPtr> Windows() {
            var result = new List<IntPtr>();
            EnumWindows((h, p) => { int cloaked; if (IsWindowVisible(h) && (DwmGetWindowAttribute(h, 14, out cloaked, 4) != 0 || cloaked == 0)) result.Add(h); return true; }, IntPtr.Zero);
            return result;
        }
        internal static void Released() {
            foreach (int key in new[] { 1, 2, 4, 16, 17, 18, 91, 92 }) if ((GetAsyncKeyState(key) & 0x8000) != 0) throw new Failure("input_unavailable", "a user key or mouse button is held; no input sent", "release held input before continuing");
        }
        internal static void Foreground(Window window, bool allowed, Request r) {
            window.Validate(); var h = window.Handle;
            if (!Interactive()) throw new Failure("desktop_unavailable", "input requires the unlocked interactive desktop");
            if (GetForegroundWindow() == h) return;
            if (!allowed) throw new Failure("input_unavailable", "this route needs the target foreground window; strict shared mode did not activate it", "ask the host to enable COMPUTER_USE_FOREGROUND_ASSIST=1, or use a supported background control action");
            Released(); r.Check(); r.Effect = true;
            if (IsIconic(h)) ShowWindow(h, 9);
            SetForegroundWindow(h);
            for (int i = 0; i < 15 && GetForegroundWindow() != h; i++) { r.Wait(20); window.Validate(); }
            if (GetForegroundWindow() != h) {
                // Some modern providers own the activation path. This prepares
                // focus only; the requested input has not been dispatched.
                // Do not attach unrelated input queues or synthesize Alt keys.
                r.Check(); window.Validate();
                try { UiaElement.FromHandle(h).SetFocus(); }
                catch (InvalidOperationException) { }
                catch (COMException) { } // Native provider refusal; observe the actual foreground below.
                for (int i = 0; i < 15 && GetForegroundWindow() != h; i++) { r.Wait(20); window.Validate(); }
            }
            window.Validate();
            if (GetForegroundWindow() != h) {
                NativeInputAccess.RequireForeground();
                throw new Failure("input_unavailable", "Windows did not grant foreground activation; no requested input sent", "bring the intended window forward and read it again");
            }
        }
        internal static INPUT Key(ushort key, bool up, bool unicode = false, bool extended = false) { return new INPUT { Type = 1, Data = new UNION { Keyboard = new KEY { Code = unicode ? (ushort)0 : key, Scan = unicode ? key : (ushort)0, Flags = (up ? 2u : 0u) | (unicode ? 4u : 0u) | (extended ? 1u : 0u) } } }; }
        internal static INPUT Mouse(uint flags, int data = 0) { return new INPUT { Type = 0, Data = new UNION { Mouse = new MOUSE { Flags = flags, Data = unchecked((uint)data) } } }; }
        internal static void Send(INPUT[] inputs, Request r) {
            r.Sending(); uint sent = SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(INPUT)));
            if (sent != inputs.Length) throw new Failure("execution_unknown", "SendInput accepted " + sent + "/" + inputs.Length + " events (possibly blocked by integrity level); not retried");
        }
        internal static void Release(INPUT input) { SendInput(1, new[] { input }, Marshal.SizeOf(typeof(INPUT))); }
        internal static void EditMessage(IntPtr h, uint message, IntPtr w, string value, Request r) {
            r.Sending(); UIntPtr result;
            if (SendMessageTimeout(h, message, w, value, 2 | 32, 1000, out result) == IntPtr.Zero) throw new Failure("execution_unknown", "native edit did not reply; it may have executed and was not retried");
        }
        internal static int[] Selection(IntPtr h) {
            var a = Marshal.AllocHGlobal(4); var b = Marshal.AllocHGlobal(4);
            try { Marshal.WriteInt32(a, 0); Marshal.WriteInt32(b, 0); UIntPtr result; if (SendMessageTimeoutRaw(h, 0xB0, a, b, 2 | 32, 500, out result) == IntPtr.Zero) return null; return new[] { Marshal.ReadInt32(a), Marshal.ReadInt32(b) }; }
            finally { Marshal.FreeHGlobal(a); Marshal.FreeHGlobal(b); }
        }
    }
}
