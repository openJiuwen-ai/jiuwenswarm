using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;

namespace ComputerUse.Windows {
    // Explicit exclusive-desktop input follows the real foreground queue. It
    // does not infer an Element, activate a window, or query an application's
    // UIA provider. Addressed App/Element operations retain their own contracts.
    internal sealed class NativeDesktopKeyboard : INativeKeyboardProvider, INativeKeyboardTextProvider {
        readonly NativePointerCaptures captures;
        sealed class Receiver {
            internal IntPtr Window, Focus;
            internal uint Pid;
            internal long Generation;
        }
        internal NativeDesktopKeyboard(NativePointerCaptures captures) { this.captures = captures; }
        static void Check(Request r) {
            r.Check();
            if (!Native.Interactive()) throw new Failure("desktop_unavailable", "desktop input requires the unlocked interactive desktop");
        }
        public NativeKeyboardState Read(Dictionary<string, object> target, string[] within, Request r, bool text = false) {
            Check(r);
            if (target["surface"] != null || target["node"] != null || within.Length != 0)
                throw new Failure("invalid_request", "desktop keyboard cannot address a window or element");
            var image = captures.Get(target);
            if (new NativePointerLayout().Signature != image.Layout)
                throw new Failure("snapshot_expired", "display layout changed; read the desktop again");
            var window = Native.GetForegroundWindow();
            NativeInputAccess.RequireWindow(window, "foreground window");
            var focus = window == IntPtr.Zero ? IntPtr.Zero : Native.Focus(window);
            var receiver = new Receiver { Window = window, Focus = focus,
                Pid = focus == IntPtr.Zero ? 0 : Native.PID(focus), Generation = focus == IntPtr.Zero ? 0 : Native.Generation(focus) };
            // A global shortcut can operate the shell even with no focused edit.
            // Text needs an actual native recipient, which the sequence pins.
            bool ready = !text || window != IntPtr.Zero && focus != IntPtr.Zero && Native.Inside(window, focus);
            Check(r);
            return new NativeKeyboardState { Native = receiver, Label = "Foreground desktop", Enabled = true,
                Foreground = true, WindowRoute = ready, Focused = ready, TextWritable = true };
        }
        public void RequireIdle(NativeKeyEvent[] events, Request r) {
            Check(r); Native.Released();
            foreach (ushort key in events.Where(e => !e.Up && !e.Unicode).Select(e => e.Code).Distinct())
                if ((Native.GetAsyncKeyState(key) & 0x8000) != 0)
                    throw new Failure("input_unavailable", "a requested key is already held; no input sent");
            Check(r);
        }
        public void Activate(NativeKeyboardState state, Request r) { throw new Failure("input_unavailable", "desktop input follows the current foreground; choose its recipient with a desktop click or shortcut"); }
        public void Focus(NativeKeyboardState state, Request r) { throw new Failure("invalid_request", "desktop input has no addressed element to focus"); }
        public int Send(NativeKeyEvent[] events) {
            var inputs = events.Select(e => Native.Key(e.Code, e.Up, e.Unicode, e.Extended)).ToArray();
            return (int)Native.SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(Native.INPUT)));
        }
        sealed class Sequence : INativeKeyboardSequence {
            readonly Receiver original;
            internal Sequence(Receiver receiver, Request r) { original = receiver; Validate(r); }
            public void Validate(Request r) {
                Check(r);
                NativeInputAccess.RequireWindow(original.Window, "foreground window");
                if (original.Window == IntPtr.Zero || original.Focus == IntPtr.Zero || !Native.IsWindow(original.Focus)
                    || Native.GetForegroundWindow() != original.Window || Native.Focus(original.Window) != original.Focus
                    || Native.PID(original.Focus) != original.Pid || Native.Generation(original.Focus) != original.Generation)
                    throw new Failure("input_unavailable", "the native desktop text recipient changed; remaining text was not sent");
            }
        }
        public INativeKeyboardSequence BeginSequence(NativeKeyboardState state, Request r) { return new Sequence((Receiver)state.Native, r); }
        public NativeTextSelection Selection(NativeKeyboardState state, Request r) { throw new Failure("invalid_request", "desktop typing inserts text; replacement requires an Element"); }
        public void SelectAll(NativeKeyboardState state, Request r) { throw new Failure("invalid_request", "desktop typing does not implicitly select all"); }
    }
}
