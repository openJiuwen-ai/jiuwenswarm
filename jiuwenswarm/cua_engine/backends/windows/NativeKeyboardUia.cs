using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows.Automation;

namespace ComputerUse.Windows {
    internal sealed partial class NativeUiaKeyboard : INativeKeyboardProvider, INativeKeyboardTextProvider {
        readonly NativeReadStore store;
        readonly NativeUiaBoundary boundary;
        sealed class Target {
            internal NativeWindowRecord Window;
            internal NativeNodeRecord Node;
            internal Dictionary<string, object> Binding;
            internal string[] Within;
        }
        internal NativeUiaKeyboard(NativeReadStore store) { this.store = store; boundary = new NativeUiaBoundary(store); }
        public NativeKeyboardState Read(Dictionary<string, object> target, string[] within, Request r, bool text = false) {
            var window = store.Validate(target, r);
            if (window == null) throw new Failure("invalid_request", "keyboard input needs an observed working window");
            var node = target["node"] == null ? null : store.Node((string)target["node"], (string)target["surface"], r);
            var state = new NativeKeyboardState { Native = new Target { Window = window, Node = node,
                Binding = new Dictionary<string, object>(target), Within = (string[])within.Clone() },
                Enabled = true, Focused = node == null, Label = window.Native.Name };
            if (node != null) {
                var role = NativeUiaBoundary.Property(node.Element, AutomationElement.ControlTypeProperty, r) as ControlType;
                state.Role = role == null ? null : role.ProgrammaticName;
                state.Label = NativeUiaBoundary.Label(node.Element, r);
                state.Enabled = NativeUiaBoundary.Property(node.Element, AutomationElement.IsEnabledProperty, r) as bool?;
                state.Focusable = NativeUiaBoundary.Property(node.Element, AutomationElement.IsKeyboardFocusableProperty, r) as bool?;
                if (text) ReadTextCapability(node.Element, state, r);
            }
            // Do not accept a focus flag on a container as ownership of an
            // arbitrary focused descendant. Python must select that relationship.
            boundary.Validate(target, within, r);
            if (node != null) {
                try {
                    r.Check(); var focused = UiaElement.FocusedElement; r.Check();
                    state.Focused = focused != null && NativeReadStore.Runtime(focused) == node.Runtime;
                } catch { r.Check(); state.Focused = false; }
            }
            var handle = window.Native.Handle;
            var inputOwner = node == null ? handle : store.NativeHandle(node.Element, r);
            // UIA callbacks may run arbitrary application code. Recheck retained
            // identity after them, then sample the native queue immediately last.
            store.Validate(target, r);
            if (node != null) store.Node(node.Id, node.Surface, r);
            state.Foreground = Native.GetForegroundWindow() == handle;
            var nativeFocus = Native.Focus(handle);
            state.WindowRoute = state.Foreground && Native.Inside(handle, nativeFocus) && Native.Inside(inputOwner, nativeFocus);
            if (state.WindowRoute) NativeInputAccess.RequireWindow(handle);
            r.Check(); return state;
        }
        public void RequireIdle(NativeKeyEvent[] events, Request r) {
            r.Check(); Native.Released();
            foreach (ushort code in events.Where(e => !e.Up && !e.Unicode).Select(e => e.Code).Distinct())
                if ((Native.GetAsyncKeyState(code) & 0x8000) != 0)
                    throw new Failure("input_unavailable", "a requested key is already held; no input sent");
            r.Check();
        }
        public void Activate(NativeKeyboardState state, Request r) {
            var target = (Target)state.Native;
            boundary.Validate(target.Binding, target.Within, r);
            RequireIdle(new NativeKeyEvent[0], r);
            // This unit is reached only after Python and NativeKeyboard have
            // authorized foreground preparation, before requested input. Use
            // the existing bounded Win32/UIA window activation helper.
            Native.Foreground(target.Window.Native, true, r);
            boundary.Validate(target.Binding, target.Within, r);
        }
        public void Focus(NativeKeyboardState state, Request r) {
            var target = (Target)state.Native;
            boundary.Validate(target.Binding, target.Within, r);
            if (target.Node == null) throw new Failure("invalid_request", "UIA focus requires a selected element");
            var node = store.Node(target.Node.Id, target.Node.Surface, r);
            if (NativeUiaBoundary.Property(node.Element, AutomationElement.IsKeyboardFocusableProperty, r) as bool? != true)
                throw new Failure("input_unavailable", "the original control no longer supports keyboard focus");
            RequireIdle(new NativeKeyEvent[0], r);
            target.Window.Native.Validate();
            // SetFocus providers can activate a window. Never call it to probe
            // an inactive window under strict shared policy.
            if (Native.GetForegroundWindow() != target.Window.Native.Handle)
                throw new Failure("input_unavailable", "the selected window is no longer foreground; no focus request sent");
            r.Check(); node.Element.SetFocus();
        }
        public int Send(NativeKeyEvent[] events) {
            var inputs = events.Select(e => Native.Key(e.Code, e.Up, e.Unicode, e.Extended)).ToArray();
            return (int)Native.SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(Native.INPUT)));
        }
    }
}
