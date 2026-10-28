using System;
using UIA = NativeUIA;
using System.Collections.Generic;
using System.Linq;
using System.Security.Principal;
using System.Threading;
using System.Windows.Automation;

namespace ComputerUse.Windows {
    internal static class NativeUiaActions {
        internal static readonly Dictionary<string, AutomationPattern> Patterns = new Dictionary<string, AutomationPattern> {
            { "Invoke.Invoke", InvokePattern.Pattern }, { "Toggle.Toggle", TogglePattern.Pattern },
            { "SelectionItem.Select", SelectionItemPattern.Pattern }, { "SelectionItem.AddToSelection", SelectionItemPattern.Pattern },
            { "SelectionItem.RemoveFromSelection", SelectionItemPattern.Pattern }, { "ExpandCollapse.Expand", ExpandCollapsePattern.Pattern },
            { "ExpandCollapse.Collapse", ExpandCollapsePattern.Pattern }, { "ScrollItem.ScrollIntoView", ScrollItemPattern.Pattern }, { "Window.Close", WindowPattern.Pattern }
        };
        internal static string[] Names(HashSet<int> supported) { return Patterns.Where(p => supported.Contains(p.Value.Id)).Select(p => p.Key).ToArray(); }
    }
    internal sealed class NativeUiaBoundary {
        readonly NativeReadStore store;
        internal NativeUiaBoundary(NativeReadStore store) { this.store = store; }
        internal static object Property(UiaElement element, AutomationProperty property, Request r) {
            r.Check(); var value = element.GetCurrentPropertyValue(property, true); r.Check();
            return ReferenceEquals(value, AutomationElement.NotSupported) ? null : value;
        }
        internal static string Label(UiaElement element, Request r) {
            try { return Property(element, AutomationElement.NameProperty, r) as string; }
            catch { r.Check(); return null; } // A label is diagnostic, never input authority.
        }
        static void WindowBoundary(NativeWindowRecord window, Request r) {
            window.Native.Validate(); r.Check();
            foreach (var candidate in Native.Windows()) {
                if (candidate == window.Native.Handle) continue;
                var current = Native.GetWindow(candidate, 4); var seen = new HashSet<IntPtr>();
                for (int n = 0; n < 64 && current != IntPtr.Zero && current != window.Native.Handle && seen.Add(current); n++) {
                    r.Check(); current = Native.GetWindow(current, 4);
                }
                if (current != window.Native.Handle) continue;
                object modal = null;
                try { modal = Property(UiaElement.FromHandle(candidate), WindowPattern.IsModalProperty, r); }
                catch (Exception) { r.Check(); } // Unknown modality is not a confirmed modal.
                if (modal is bool && (bool)modal) throw new Failure("blocked_by_modal", "an owned native modal window blocks the selected control");
            }
            window.Native.Validate(); r.Check();
            if (!Native.IsWindowEnabled(window.Native.Handle)) throw new Failure("input_unavailable", "the selected native window is disabled; the reason is not inferred");
        }
        internal NativeWindowRecord Validate(Dictionary<string, object> target, string[] within, Request r) {
            r.Check();
            if (!Native.Interactive()) throw new Failure("desktop_unavailable", "native input requires the current unlocked interactive desktop");
            var window = store.Validate(target, r);
            if (window == null) throw new Failure("invalid_request", "native input needs an observed working window");
            if (target["node"] == null) {
                if (within.Length != 0) throw new Failure("invalid_request", "window input cannot have element ancestors");
                WindowBoundary(window, r); return window;
            }
            var node = store.Node((string)target["node"], (string)target["surface"], r);
            if (within.Contains(node.Id)) throw new Failure("invalid_request", "ancestor constraints must not include the selected control");
            var required = new HashSet<string>(within.Select(id => store.Node(id, node.Surface, r).Runtime));
            if (required.Count != 0) {
                var current = UiaClient.Parent(node.Element); var visited = new HashSet<string>();
                for (int n = 0; n < 128 && current != null; n++) {
                    r.Check(); string key = NativeReadStore.Runtime(current); if (!visited.Add(key)) break;
                    required.Remove(key); if (required.Count == 0) break;
                    if (!store.Within(current, window, r)) break;
                    current = UiaClient.Parent(current);
                }
                if (required.Count != 0) throw new Failure("stale_target", "the control left its observed ancestor scope");
            }
            // UIA callbacks during parent queries may destroy or move windows.
            WindowBoundary(window, r);
            store.Validate(target, r);
            r.Check(); return window;
        }
    }
    internal sealed class NativeUiaObjects : INativeObjectProvider {
        readonly NativeReadStore store;
        readonly NativeUiaBoundary boundary;
        internal NativeUiaObjects(NativeReadStore store) { this.store = store; boundary = new NativeUiaBoundary(store); }
        sealed class Lease : IDisposable {
            Mutex mutex; bool held;
            internal Lease(Request r) {
                mutex = new Mutex(false, "Local\\ComputerUse-" + WindowsIdentity.GetCurrent().User.Value);
                try { while (!held) { r.Check(); try { held = mutex.WaitOne(25); } catch (AbandonedMutexException) { held = true; } } r.Check(); }
                catch { Dispose(); throw; }
            }
            public void Dispose() {
                if (mutex == null) return;
                var current = mutex;
                if (held) { current.ReleaseMutex(); held = false; }
                current.Dispose(); mutex = null;
            }
        }
        public IDisposable Acquire(Request r) { return new Lease(r); }
        public NativeObjectState Read(Dictionary<string, object> target, string[] within, Request r) {
            // Resolve the retained object first; provider calls may change the
            // UI, so scope/ancestors/modal are checked once after those calls.
            // Repeated global scans cannot make independent UIA calls atomic.
            var window = store.Validate(target, r);
            if (window == null || target["node"] == null) throw new Failure("invalid_request", "object input needs an observed native element");
            var node = store.Node((string)target["node"], (string)target["surface"], r); var element = node.Element;
            var role = NativeUiaBoundary.Property(element, AutomationElement.ControlTypeProperty, r) as ControlType;
            var state = new NativeObjectState { Native = element, Role = role == null ? null : role.ProgrammaticName,
                Label = NativeUiaBoundary.Label(element, r),
                Enabled = NativeUiaBoundary.Property(element, AutomationElement.IsEnabledProperty, r) as bool? };
            r.Check(); var patterns = new HashSet<int>(element.GetSupportedPatterns().Select(p => p.Id)); r.Check();
            state.Actions = NativeUiaActions.Names(patterns); state.ActionsComplete = true;
            state.Replaceable = patterns.Contains(ValuePattern.Pattern.Id) ? !(NativeUiaBoundary.Property(element, ValuePattern.IsReadOnlyProperty, r) as bool?) : false;
            boundary.Validate(target, within, r); return state;
        }
        public Action Prepare(NativeObjectState state, string unit, string action, string text, Request r) {
            var element = (UiaElement)state.Native; object pattern;
            r.Check();
            if (unit == "native_text_edit") {
                if (!element.TryGetCurrentPattern(ValuePattern.Pattern, out pattern)) throw new Failure("input_unavailable", "the addressed ValuePattern is no longer available");
                var value = (UIA.IUIAutomationValuePattern)pattern;
                if (value.CurrentIsReadOnly != 0) throw new Failure("input_unavailable", "the addressed value became read-only");
                r.Check(); return () => value.SetValue(text);
            }
            AutomationPattern type;
            if (!NativeUiaActions.Patterns.TryGetValue(action, out type) || !element.TryGetCurrentPattern(type, out pattern))
                throw new Failure("input_unavailable", "the selected UIA pattern is no longer available");
            r.Check();
            switch (action) {
                case "Invoke.Invoke": return () => ((UIA.IUIAutomationInvokePattern)pattern).Invoke();
                case "Toggle.Toggle": return () => ((UIA.IUIAutomationTogglePattern)pattern).Toggle();
                case "SelectionItem.Select": return () => ((UIA.IUIAutomationSelectionItemPattern)pattern).Select();
                case "SelectionItem.AddToSelection": return () => ((UIA.IUIAutomationSelectionItemPattern)pattern).AddToSelection();
                case "SelectionItem.RemoveFromSelection": return () => ((UIA.IUIAutomationSelectionItemPattern)pattern).RemoveFromSelection();
                case "ExpandCollapse.Expand": return () => ((UIA.IUIAutomationExpandCollapsePattern)pattern).Expand();
                case "ExpandCollapse.Collapse": return () => ((UIA.IUIAutomationExpandCollapsePattern)pattern).Collapse();
                case "ScrollItem.ScrollIntoView": return () => ((UIA.IUIAutomationScrollItemPattern)pattern).ScrollIntoView();
                case "Window.Close": return () => ((UIA.IUIAutomationWindowPattern)pattern).Close();
                default: throw new Failure("invalid_request", "unknown native UIA action");
            }
        }
    }
}
