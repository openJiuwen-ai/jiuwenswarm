using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Automation;

namespace ComputerUse.Windows {
    // Discovery returns native handles, not UIA objects or input permission.
    // Win32 remains usable when a UIA provider hangs. A single background MTA
    // job is allowed; a timed-out job is never multiplied or reused as a fresh
    // observation. Only scalars leave the worker, never retained UIA objects.
    internal static class NativeWindowDiscovery {
        internal sealed class Candidate {
            internal IntPtr Handle;
            internal uint Pid;
            internal long Generation;
        }
        internal sealed class Snapshot {
            internal readonly List<Candidate> Windows = new List<Candidate>();
            internal readonly List<string> Notes = new List<string>();
        }
        static readonly object gate = new object();
        static Task<Snapshot> pending;
        internal static bool RequiresRestart { get; private set; }

        internal static bool Visible(IntPtr handle) {
            if (handle == IntPtr.Zero || !Native.IsWindow(handle) || !Native.IsWindowVisible(handle)) return false;
            int cloaked;
            return Native.DwmGetWindowAttribute(handle, 14, out cloaked, 4) != 0 || cloaked == 0;
        }
        static Snapshot Uia() {
            var result = new Snapshot();
            try {
                var desktop = UiaElement.RootElement;
                var cache = UiaClient.Cache(new[] { AutomationElement.NativeWindowHandleProperty.Id, AutomationElement.ProcessIdProperty.Id }, true);
                {
                    var children = desktop.Snapshot(cache).CachedChildren;
                    if (children.Length > 512) result.Notes.Add("UIA desktop directory limited to 512 entries.");
                    for (int n = 0; n < Math.Min(children.Length, 512); n++) {
                        try {
                            var cached = children[n];
                            var h = new IntPtr(Convert.ToInt64(cached.GetCachedPropertyValue(AutomationElement.NativeWindowHandleProperty)));
                            if (h == IntPtr.Zero) {
                                NativeReadStore.Note(result.Notes, "A UIA desktop entry has no native window handle; it cannot yet be selected as a working window.");
                                continue;
                            }
                            if (!Visible(h)) continue;
                            // Do not map an unrelated provider to its host by a
                            // name/rectangle heuristic. HWND + live PID agree.
                            uint pid = Native.PID(h);
                            var reportedPid = Convert.ToInt32(cached.GetCachedPropertyValue(AutomationElement.ProcessIdProperty));
                            if (pid == 0 || reportedPid <= 0 || pid != (uint)reportedPid) {
                                NativeReadStore.Note(result.Notes, "A UIA desktop entry has conflicting native process identity.");
                                continue;
                            }
                            if (Native.GetAncestor(h, 2) != h) continue;
                            result.Windows.Add(new Candidate { Handle = h, Pid = pid, Generation = Native.Generation(h) });
                        } catch (Exception error) { NativeReadStore.Note(result.Notes, "UIA desktop entry unavailable: " + error.Message); }
                    }
                }
            } catch (Exception error) { NativeReadStore.Note(result.Notes, "UIA desktop enumeration unavailable: " + error.Message); }
            return result;
        }
        static Snapshot Supplement(Request request, List<string> notes) {
            Task<Snapshot> task;
            lock (gate) {
                if (pending != null && !pending.IsCompleted) {
                    NativeReadStore.Note(notes, "UIA desktop enumeration is still busy; using native window discovery.");
                    return null;
                }
                var completion = new TaskCompletionSource<Snapshot>();
                task = pending = completion.Task;
                var thread = new Thread(() => {
                    try { completion.SetResult(Uia()); }
                    catch (Exception error) { completion.SetException(error); }
                });
                try {
                    thread.IsBackground = true; thread.SetApartmentState(ApartmentState.MTA); thread.Start();
                } catch (Exception error) { completion.SetException(error); }
            }
            double until = Math.Min(request.Deadline, Wire.Now + 1200);
            try {
                while (!task.IsCompleted && Wire.Now < until) { request.Check(); Thread.Sleep(20); }
                request.Check();
            } catch { RequiresRestart = !task.IsCompleted; throw; }
            if (!task.IsCompleted) {
                RequiresRestart = true;
                NativeReadStore.Note(notes, "UIA desktop enumeration exceeded its 1200 ms budget; using native window discovery.");
                return null;
            }
            try { return task.GetAwaiter().GetResult(); }
            catch (Exception error) {
                NativeReadStore.Note(notes, "UIA desktop enumeration unavailable: " + error.Message);
                return null;
            }
        }
        internal static List<IntPtr> Collect(Request request, List<string> notes, bool accessibility = true) {
            request.Check();
            var handles = new List<IntPtr>();
            if (!Native.EnumWindows((h, p) => { if (Visible(h)) handles.Add(h); return true; }, IntPtr.Zero))
                NativeReadStore.Note(notes, "Win32 window enumeration failed; directory coverage is incomplete.");
            var seen = new HashSet<IntPtr>(handles);
            // Foreground is an independent discovery entry; it
            // is evidence of an interface, not a request to activate anything.
            IntPtr front = Native.GetForegroundWindow();
            IntPtr root = front == IntPtr.Zero ? IntPtr.Zero : Native.GetAncestor(front, 2);
            if (Visible(root) && seen.Add(root)) handles.Insert(0, root);
            var extra = accessibility ? Supplement(request, notes) : null;
            if (extra != null) {
                foreach (string note in extra.Notes) NativeReadStore.Note(notes, note);
                foreach (var item in extra.Windows) {
                    request.Check();
                    if (Native.Generation(item.Handle) != item.Generation || Native.PID(item.Handle) != item.Pid || !Visible(item.Handle)) {
                        NativeReadStore.Note(notes, "A UIA-discovered window changed before directory assembly."); continue;
                    }
                    if (seen.Add(item.Handle)) handles.Add(item.Handle);
                }
            }
            return handles;
        }
    }
}
