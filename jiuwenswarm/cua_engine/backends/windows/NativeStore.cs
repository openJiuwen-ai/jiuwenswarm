using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Windows.Automation;

namespace ComputerUse.Windows {
    internal sealed class NativeWindowRecord {
        internal bool? ObservedModal;
        internal Window Native;
        internal string Application, RootNode;
    }
    internal sealed class NativeNodeRecord {
        internal string Id, Surface, Runtime;
        internal UiaElement Element;
        internal NativeWindowRecord Window;
        internal bool RelationOnly;
    }
    // Retention keeps native objects alive, not third-party UI or focus.
    internal sealed class NativeReadStore {
        readonly Dictionary<IntPtr, NativeWindowRecord> windows = new Dictionary<IntPtr, NativeWindowRecord>();
        readonly Dictionary<string, NativeWindowRecord> surfaces = new Dictionary<string, NativeWindowRecord>();
        readonly Dictionary<string, string> applications = new Dictionary<string, string>();
        readonly Queue<string> applicationOrder = new Queue<string>();
        readonly Dictionary<string, NativeNodeRecord> nodes = new Dictionary<string, NativeNodeRecord>();
        readonly Dictionary<string, string> identities = new Dictionary<string, string>();
        readonly Queue<string> nodeOrder = new Queue<string>();
        internal readonly HashSet<string> Pins = new HashSet<string>();
        internal object[] WindowCheckpoint() {
            return windows.Values.Select(item => {
                var w = item.Native;
                return (object)Wire.Obj("handle", w.Handle.ToInt64(), "pid", w.Pid, "started", w.Started.ToString(),
                    "window", w.Ref, "surface", w.Scope, "instance", w.Selector, "name", w.Name,
                    "application", item.Application, "modal", item.ObservedModal);
            }).ToArray();
        }
        internal void RestoreWindows(object[] checkpoint) {
            foreach (object raw in checkpoint) {
                var item = Wire.Object(raw); var handle = new IntPtr(Convert.ToInt64(item["handle"]));
                var w = new Window { Handle=handle, Pid=Convert.ToUInt32(item["pid"]), Started=Int64.Parse((string)item["started"]),
                    Generation=Native.Generation(handle), Ref=(string)item["window"], Scope=(string)item["surface"],
                    Selector=(string)item["instance"], Name=(string)item["name"] };
                try { w.Validate(); } catch (Failure) { continue; }
                var entry = new NativeWindowRecord { Native=w, Application=(string)item["application"], ObservedModal=item["modal"] as bool? };
                windows[handle]=entry; surfaces[w.Scope]=entry;
                string key=w.Pid+":"+w.Started; applications[key]=entry.Application; applicationOrder.Enqueue(key);
            }
        }
        internal static string Token(string kind) { return kind + ":" + Guid.NewGuid().ToString("N"); }
        internal static string Runtime(UiaElement element) { return String.Join(".", element.GetRuntimeId()); }
        internal static string Text(string value) { return value == null ? "" : value.Length > 4096 ? value.Substring(0, Char.IsHighSurrogate(value[4095]) ? 4095 : 4096) : value; }
        internal static void Note(List<string> notes, string value) { value = Text(value); if (notes.Count < 128 && !notes.Contains(value)) notes.Add(value); }
        static void RequireTracking() {
            if (!Native.GenerationTrackingAvailable) throw new Failure("observation_incomplete", "native window lifetime event tracking is unavailable; persistent window identity cannot be established");
        }
        internal List<NativeWindowRecord> Catalog(Request r, List<string> notes, bool accessibility = true) {
            r.Check(); RequireTracking();
            var current = NativeWindowDiscovery.Collect(r, notes, accessibility);
            // Absence from the visible directory does not end a window's life.
            // Preserve its selector through hide/show and discovery failures;
            // retire only when the native identity actually becomes invalid.
            foreach (var h in windows.Keys.ToArray()) {
                try { windows[h].Native.Validate(); } catch (Failure) { Retire(h); }
            }
            var result = new List<NativeWindowRecord>();
            foreach (var h in current) {
                r.Check(); if (result.Count >= 512) { Note(notes, "Window directory limited to 512 entries."); break; }
                try {
                    NativeWindowRecord entry;
                    if (windows.TryGetValue(h, out entry)) { try { entry.Native.Validate(); } catch (Failure) { Retire(h); entry = null; } }
                    if (entry == null) {
                        if (surfaces.Count >= 4096) { Note(notes, "Retained window identity budget exhausted."); continue; }
                        uint pid = Native.PID(h);
                        using (var process = Process.GetProcessById((int)pid)) {
                            long birth = process.StartTime.ToUniversalTime().Ticks;
                            string processKey = pid + ":" + birth, app;
                            if (!applications.TryGetValue(processKey, out app)) {
                                app = Token("application"); applications[processKey] = app; applicationOrder.Enqueue(processKey);
                                while (applicationOrder.Count > 4096) applications.Remove(applicationOrder.Dequeue());
                            }
                            var window = new Window { Handle = h, Pid = pid, Started = birth, Generation = Native.Generation(h), Ref = Token("window"), Scope = Token("surface"), Selector = Token("instance"), Name = process.ProcessName };
                            window.Validate(); entry = new NativeWindowRecord { Native = window, Application = app };
                            windows[h] = entry; surfaces[window.Scope] = entry;
                        }
                    }
                    result.Add(entry);
                } catch (Failure error) { if (error.Code == "cancelled" || error.Code == "deadline_exceeded") throw; Note(notes, "Window changed during discovery: " + error.Message); }
                catch (Exception error) { r.Check(); Note(notes, "Native window metadata unavailable: " + error.Message); }
            }
            return result;
        }
        void Retire(IntPtr handle) {
            NativeWindowRecord old;
            if (!windows.TryGetValue(handle, out old)) return;
            windows.Remove(handle); surfaces.Remove(old.Native.Scope);
            foreach (var id in nodes.Where(p => p.Value.Surface == old.Native.Scope).Select(p => p.Key).ToArray()) Remove(id);
        }
        void Remove(string id) {
            NativeNodeRecord item; if (!nodes.TryGetValue(id, out item)) return;
            nodes.Remove(id); if (item.Runtime != null) identities.Remove(item.Surface + ":" + item.Runtime); Pins.Remove(id);
        }
        internal void Release() { Pins.Clear(); Trim(); }
        void Trim() {
            if (nodeOrder.Count > 32768) {
                var retained = nodeOrder.Where(nodes.ContainsKey).Distinct().ToArray();
                nodeOrder.Clear(); foreach (string id in retained) nodeOrder.Enqueue(id);
            }
            int remaining = nodeOrder.Count;
            while (nodes.Count > 8192 && remaining-- > 0) {
                string id = nodeOrder.Dequeue();
                if (Pins.Contains(id)) nodeOrder.Enqueue(id); else Remove(id);
            }
            if (nodes.Count > 16384) throw new Failure("observation_incomplete", "native object retention budget exhausted");
        }
        internal IntPtr NativeHandle(UiaElement element, Request r) {
            var seen = new HashSet<string>();
            for (int n = 0; n < 64 && element != null; n++) {
                r.Check(); string id = Runtime(element); if (!seen.Add(id)) return IntPtr.Zero;
                int handle = element.Current.NativeWindowHandle;
                if (handle != 0) return new IntPtr(handle);
                element = UiaClient.Parent(element);
            }
            return IntPtr.Zero;
        }
        internal bool Within(UiaElement element, NativeWindowRecord window, Request r) {
            // UIA fragments may cross process/HWND boundaries (WebView hosts,
            // XAML islands). A foreign nearest HWND is not proof of a foreign
            // UI. Follow the live provider parent chain to our native root.
            var seen = new HashSet<string>();
            for (int n = 0; n < 128 && element != null; n++) {
                r.Check(); if (!seen.Add(Runtime(element))) return false;
                var handle = new IntPtr(element.Current.NativeWindowHandle);
                if (Native.Inside(window.Native.Handle, handle)) { window.Native.Validate(); return true; }
                element = UiaClient.Parent(element);
            }
            return false;
        }
        internal string Keep(UiaElement element, NativeWindowRecord window, Request r) {
            r.Check(); RequireTracking(); window.Native.Validate();
            if (!Within(element, window, r)) throw new Failure("observation_incomplete", "native relation leaves the selected window or has no provable ownership");
            string runtime = Runtime(element);
            if (String.IsNullOrEmpty(runtime)) throw new Failure("observation_incomplete", "UIA provider did not return a runtime identity");
            return Retain(element, window, runtime, false);
        }
        string Retain(UiaElement element, NativeWindowRecord window, string runtime, bool relationOnly) {
            string key = window.Native.Scope + ":" + runtime, id;
            if (runtime != null && identities.TryGetValue(key, out id)) {
                NativeNodeRecord known;
                if (nodes.TryGetValue(id, out known)) {
                    try { if (Runtime(known.Element) == runtime) { Pins.Add(id); return id; } }
                    catch (ElementNotAvailableException) { }
                    Remove(id);
                }
            }
            id = Token("node"); var item = new NativeNodeRecord { Id = id, Surface = window.Native.Scope, Runtime = runtime,
                Element = element, Window = window, RelationOnly = relationOnly };
            nodes[id] = item; if (runtime != null) identities[key] = id;
            nodeOrder.Enqueue(id); Pins.Add(id); Trim(); return id;
        }
        // A provider can expose text children or virtualized placeholders with
        // no usable HWND/parent. Preserve that observation, without granting
        // the input authority provided by Node()'s independent live checks.
        internal string KeepRelated(UiaElement element, NativeNodeRecord source, Request r) {
            r.Check(); RequireTracking(); source.Window.Native.Validate();
            // Read follows the observed relation; it does not need to repeat
            // an input ownership walk for every child. Check any directly
            // reported HWND and keep weaker provenance for HWND-less peers.
            // Node() still establishes live ancestry before any operation.
            IntPtr handle = IntPtr.Zero;
            try { handle = new IntPtr(element.Current.NativeWindowHandle); } catch (ElementNotAvailableException) { r.Check(); }
            if (handle != IntPtr.Zero && !Native.Inside(source.Window.Native.Handle, handle) && !Within(element, source.Window, r))
                throw new NativeFieldUnavailable("unsupported", "native relation points outside the selected window");
            string runtime = null;
            try { runtime = Runtime(element); } catch (ElementNotAvailableException) { r.Check(); }
            if (String.IsNullOrEmpty(runtime)) runtime = null;
            return Retain(element, source.Window, runtime, !Native.Inside(source.Window.Native.Handle, handle));
        }
        internal NativeNodeRecord ReadNode(string id, string surface, Request r) {
            r.Check(); RequireTracking(); NativeNodeRecord item;
            if (!nodes.TryGetValue(id, out item) || item.Surface != surface)
                throw new Failure("stale_target", "native observation reference is expired or belongs to another window");
            item.Window.Native.Validate();
            // A stale/virtualized native object is useful diagnostic evidence:
            // its individual fields will report unavailable. Do not turn one
            // inaccessible descendant into a failed whole-window read.
            if (item.Runtime != null) {
                try { if (Runtime(item.Element) != item.Runtime) throw new Failure("stale_target", "native observation identity changed"); }
                catch (ElementNotAvailableException) { r.Check(); }
            }
            Pins.Add(id); return item;
        }
        internal NativeNodeRecord Node(string id, string surface, Request r) {
            r.Check(); RequireTracking();
            NativeNodeRecord item;
            if (!nodes.TryGetValue(id, out item) || item.Surface != surface) throw new Failure("stale_target", "native element reference is expired or belongs to another window");
            item.Window.Native.Validate(); r.Check();
            if (item.Runtime == null) throw new Failure("stale_target", "observed UIA object has no verifiable runtime identity; use a fresh observation or a captured coordinate target");
            try { if (Runtime(item.Element) != item.Runtime || !Within(item.Element, item.Window, r)) throw new Failure("stale_target", "native UIA element identity or window ownership changed"); }
            catch (ElementNotAvailableException) { throw new Failure("stale_target", "native UIA element no longer exists"); }
            Pins.Add(id); return item;
        }
        internal void RequireNoKnownModal(NativeWindowRecord window, Request r) {
            foreach (var candidate in surfaces.Values.Where(x => x.ObservedModal == true && x != window)) {
                r.Check();
                try { candidate.Native.Validate(); } catch (Failure) { continue; }
                if (!Native.IsWindowVisible(candidate.Native.Handle) || Native.IsIconic(candidate.Native.Handle)) continue;
                var current = Native.GetWindow(candidate.Native.Handle, 4); var seen = new HashSet<IntPtr>();
                for (int n = 0; n < 64 && current != IntPtr.Zero && seen.Add(current); n++) {
                    r.Check();
                    if (current == window.Native.Handle) throw new Failure("blocked_by_modal", "an observed owned modal window blocks the captured window");
                    current = Native.GetWindow(current, 4);
                }
            }
        }
        internal NativeWindowRecord Validate(Dictionary<string, object> target, Request r, bool observation = false) {
            r.Check(); if (target["surface"] == null) return null;
            RequireTracking();
            NativeWindowRecord selected;
            if (!surfaces.TryGetValue((string)target["surface"], out selected)) throw new Failure("stale_target", "window surface was never observed or has expired");
            var root = surfaces.Values.FirstOrDefault(x => x.Native.Selector == (string)target["instance"] && x.Native.Ref == (string)target["rootWindow"]);
            if (root == null) throw new Failure("stale_target", "original working window is no longer retained");
            root.Native.Validate(); selected.Native.Validate();
            var seen = new HashSet<IntPtr>(); IntPtr current = selected.Native.Handle;
            for (int n = 0; current != root.Native.Handle; n++) {
                r.Check();
                if (current == IntPtr.Zero || n >= 64 || !seen.Add(current)) throw new Failure("stale_target", "surface does not belong to the original working window");
                current = Native.GetWindow(current, 4);
            }
            if (target["node"] != null) {
                if (observation) ReadNode((string)target["node"], (string)target["surface"], r);
                else Node((string)target["node"], (string)target["surface"], r);
            }
            return selected;
        }
    }
}
