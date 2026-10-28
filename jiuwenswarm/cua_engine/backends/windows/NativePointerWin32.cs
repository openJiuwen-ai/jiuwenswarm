using System;
using System.Collections.Generic;
using System.Drawing;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace ComputerUse.Windows {
    internal sealed class NativePointerLayout {
        internal readonly NativeCaptureGeometry Canvas;
        internal readonly NativeCaptureGeometry[] Displays;
        internal readonly string Signature;
        internal NativePointerLayout() {
            var canvas = SystemInformation.VirtualScreen;
            Canvas = Geometry(canvas);
            var screens = Screen.AllScreens.OrderBy(s => s.DeviceName, StringComparer.Ordinal).ToArray();
            if (screens.Length < 1 || screens.Length > 32 || screens.Select(s => s.DeviceName).Distinct().Count() != screens.Length)
                throw new Failure("observation_unavailable", "display layout is unavailable");
            Displays = screens.Select(s => Geometry(s.Bounds)).ToArray();
            Signature = canvas.ToString() + "|" + String.Join("|", screens.Select(s => s.DeviceName + ":" + s.Bounds));
        }
        static NativeCaptureGeometry Geometry(Rectangle r) { return new NativeCaptureGeometry(r.X, r.Y, r.Width, r.Height, 1); }
    }
    // Uses HWND identity and physical screen geometry. No UIA traversal,
    // legacy input strategy, clipboard or synthetic keyboard focus is involved.
    internal sealed class NativeWindowsPointer : INativePointerProvider {
        readonly NativeReadStore windows;
        readonly NativePointerCaptures captures;
        sealed class Target { internal Dictionary<string, object> Binding; internal NativePointerPoint Point; internal string Kind; }
        internal NativeWindowsPointer(NativeReadStore windows, NativePointerCaptures captures) { this.windows = windows; this.captures = captures; }
        public NativePointerCapture Capture(Dictionary<string, object> target, Request r) { r.Check(); return captures.Get(target); }
        public NativePointerState Read(Dictionary<string, object> target, NativePointerPoint point, string kind, bool dragging, Request r) {
            r.Check();
            if (!Native.Interactive()) throw new Failure("desktop_unavailable", "pointer input requires the unlocked interactive desktop");
            if (dragging) foreach (int key in new[] { 2, 4, 16, 17, 18, 91, 92 }) {
                if ((Native.GetAsyncKeyState(key) & 0x8000) != 0)
                    throw new Failure("input_unavailable", "unowned input became held during the drag");
            }
            var image = captures.Get(target); image.RequirePoint(point);
            if (new NativePointerLayout().Signature != image.Layout) throw new Failure("snapshot_expired", "display layout changed; read and view a new image");
            var window = windows.Validate(target, r);
            // Desktop coordinates address the visible desktop, including the
            // taskbar and popups. There is no fixed-window hit-test or activation.
            if (window == null) {
                NativeInputAccess.RequireForeground();
                NativeInputAccess.RequireWindow(Native.WindowFromPoint(new Native.POINT { X = point.X, Y = point.Y }));
                return new NativePointerState { Ready = true, Foreground = true };
            }
            IntPtr handle = window.Native.Handle;
            if (!Native.IsWindowVisible(handle) || Native.IsIconic(handle) || !Native.IsWindowEnabled(handle))
                throw new Failure("input_unavailable", "the selected window is hidden, minimized or disabled");
            windows.RequireNoKnownModal(window, r);
            uint pid;
            var gui = new Native.GUI { Size = Marshal.SizeOf(typeof(Native.GUI)) };
            bool threadKnown = Native.GetGUIThreadInfo(Native.GetWindowThreadProcessId(handle, out pid), ref gui);
            bool ownedGrab = threadKnown && Native.Inside(handle, gui.Capture);
            Rectangle bounds = Native.Bounds(handle);
            bool sameBounds = bounds.X == image.WindowBounds.X && bounds.Y == image.WindowBounds.Y && bounds.Width == image.WindowBounds.NativeWidth && bounds.Height == image.WindowBounds.NativeHeight;
            // A window move/resize may be the drag's intended effect. Continue
            // its original absolute path only under that window's actual grab.
            if (!sameBounds && !(dragging && ownedGrab)) throw new Failure("snapshot_expired", "captured window geometry changed; read and view a new image");
            var foreground = new Native.GUI { Size = Marshal.SizeOf(typeof(Native.GUI)) };
            bool foregroundKnown = Native.GetGUIThreadInfo(0, ref foreground);
            bool hit = Native.Inside(handle, Native.WindowFromPoint(new Native.POINT { X = point.X, Y = point.Y }));
            bool noOtherGrab = foregroundKnown && (foreground.Capture == IntPtr.Zero || dragging && Native.Inside(handle, foreground.Capture));
            bool route = noOtherGrab && (dragging && ownedGrab || hit && (!dragging || threadKnown && gui.Capture == IntPtr.Zero));
            bool active = Native.GetForegroundWindow() == handle;
            // WM_MOUSEWHEEL may route to focus rather than hover depending on
            // user settings. Do not send window-scoped wheel input elsewhere.
            if (kind == "scroll") route = route && active && threadKnown && Native.Inside(handle, gui.Focus);
            if (route) { NativeInputAccess.RequireForeground(); NativeInputAccess.RequireWindow(handle); }
            windows.Validate(target, r); r.Check();
            return new NativePointerState { Native = new Target { Binding = new Dictionary<string, object>(target), Point = point, Kind = kind },
                Ready = route, Foreground = active };
        }
        public void RequireIdle(Request r) { r.Check(); Native.Released(); r.Check(); }
        public void Activate(NativePointerState state, Request r) {
            var selected = (Target)state.Native;
            RequireIdle(r); Read(selected.Binding, selected.Point, selected.Kind, false, r);
            var window = windows.Validate(selected.Binding, r); window.Native.Validate(); r.Check();
            // A denied SetForegroundWindow is reported by subsequent readiness
            // observation. Never attach threads, fake Alt, or restore foreground.
            if (!Native.SetForegroundWindow(window.Native.Handle)) NativeInputAccess.RequireForeground();
        }
        public int Send(NativePointerEvent[] events) {
            var inputs = events.Select(e => new Native.INPUT { Type = 0, Data = new Native.UNION { Mouse = new Native.MOUSE {
                X = e.X, Y = e.Y, Flags = e.Flags, Data = unchecked((uint)e.Data) } } }).ToArray();
            return (int)Native.SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(Native.INPUT)));
        }
    }
}
