using System;
using UIA = NativeUIA;
using System.Collections.Generic;
using System.Windows.Automation;

namespace ComputerUse.Windows {
    internal sealed class NativeUiaScroll : INativeScrollProvider {
        readonly NativeReadStore store;
        readonly NativeUiaBoundary boundary;
        internal NativeUiaScroll(NativeReadStore store) { this.store = store; boundary = new NativeUiaBoundary(store); }
        public NativeScrollState Read(Dictionary<string, object> target, string[] within, string direction, Request r) {
            boundary.Validate(target, within, r);
            var element = store.Node((string)target["node"], (string)target["surface"], r).Element;
            var role = NativeUiaBoundary.Property(element, AutomationElement.ControlTypeProperty, r) as ControlType;
            var state = new NativeScrollState { Native = element, Role = role == null ? null : role.ProgrammaticName,
                Label = NativeUiaBoundary.Label(element, r), Enabled = NativeUiaBoundary.Property(element, AutomationElement.IsEnabledProperty, r) as bool? };
            r.Check(); object raw;
            if (!element.TryGetCurrentPattern(ScrollPattern.Pattern, out raw)) state.Scrollable = false;
            else {
                var current = (UIA.IUIAutomationScrollPattern)raw;
                bool horizontal = direction == "left" || direction == "right";
                state.Scrollable = (horizontal ? current.CurrentHorizontallyScrollable : current.CurrentVerticallyScrollable) != 0;
                state.Percent = horizontal ? current.CurrentHorizontalScrollPercent : current.CurrentVerticalScrollPercent;
                state.ViewSize = horizontal ? current.CurrentHorizontalViewSize : current.CurrentVerticalViewSize;
            }
            // No HWND focus, parent substitution or input-queue requirement.
            boundary.Validate(target, within, r); return state;
        }
        public Action Prepare(NativeScrollState state, string direction, double pages, Request r) {
            r.Check(); object raw;
            if (!((UiaElement)state.Native).TryGetCurrentPattern(ScrollPattern.Pattern, out raw))
                throw new Failure("input_unavailable", "the addressed ScrollPattern is no longer available");
            r.Check(); var pattern = (UIA.IUIAutomationScrollPattern)raw;
            bool horizontal = direction == "left" || direction == "right";
            if (pages == 1) {
                var amount = direction == "up" || direction == "left" ? ScrollAmount.LargeDecrement : ScrollAmount.LargeIncrement;
                return () => pattern.Scroll((UIA.ScrollAmount)(horizontal ? amount : ScrollAmount.NoAmount), (UIA.ScrollAmount)(horizontal ? ScrollAmount.NoAmount : amount));
            }
            double percent = NativeScroll.FractionalPosition(state, direction, pages);
            return () => pattern.SetScrollPercent(horizontal ? percent : ScrollPattern.NoScroll, horizontal ? ScrollPattern.NoScroll : percent);
        }
    }
}
