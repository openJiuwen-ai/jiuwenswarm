using System;
using UIA = NativeUIA;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Automation;

namespace ComputerUse.Windows {
    // Optional UIA enrichment cannot hold the Win32 window catalogue hostage.
    // The enrichment thread lives until worker exit, preserving COM apartment
    // lifetime for retained objects. Both threads belong to the process MTA. Nothing is retained in the store
    // until this task completes. A timeout returns no UIA objects and marks the
    // entire containing worker for termination by its supervisor after reply.
    internal sealed class NativeRootProbe {
        readonly BlockingCollection<Action> work = new BlockingCollection<Action>();
        bool blocked;
        internal bool Blocked { get { return blocked; } }
        internal NativeRootProbe() {
            var thread = new Thread(() => { foreach (var action in work.GetConsumingEnumerable()) action(); });
            thread.IsBackground = true; thread.SetApartmentState(ApartmentState.MTA); thread.Start();
        }
        internal sealed class Root { internal UiaElement Element; internal bool? Modal; internal string Error; }
        internal Dictionary<IntPtr, Root> Read(NativeWindowRecord[] windows, Request request) {
            if (blocked) return null;
            var completion = new TaskCompletionSource<Dictionary<IntPtr, Root>>();
            work.Add(() => {
                try {
                    var roots = new Dictionary<IntPtr, Root>();
                    foreach (var window in windows) {
                        var result = new Root(); roots[window.Native.Handle] = result;
                        try {
                            window.Native.Validate();
                            result.Element = UiaElement.FromHandle(window.Native.Handle);
                            if (result.Element != null) {
                                object pattern;
                                if (result.Element.TryGetCurrentPattern(WindowPattern.Pattern, out pattern))
                                    result.Modal = ((UIA.IUIAutomationWindowPattern)pattern).CurrentIsModal != 0;
                            }
                        } catch (Exception error) { result.Error = NativeReadStore.Text(error.Message); }
                    }
                    completion.SetResult(roots);
                } catch (Exception error) { completion.SetException(error); }
            });
            double until = Math.Min(request.Deadline, Wire.Now + 1000);
            try {
                while (!completion.Task.IsCompleted && Wire.Now < until) { request.Check(); Thread.Sleep(10); }
                request.Check();
            } catch { blocked = !completion.Task.IsCompleted; throw; }
            blocked = !completion.Task.IsCompleted;
            return blocked ? null : completion.Task.GetAwaiter().GetResult();
        }
    }
}
