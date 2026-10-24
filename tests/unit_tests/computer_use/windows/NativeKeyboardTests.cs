// The real coordinator/keyboard unit with explicit fake native boundaries.
// This validates protocol, policy handoff and cleanup, not Win32/UIA behavior.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Text.Json;

namespace ComputerUse.Windows {
    internal class KeyboardProbe : INativeKeyboardProvider {
        internal bool Alive = true, Enabled = true, Focusable = true, Foreground, Route, Focused, Held;
        internal bool FocusWorks = true;
        internal string Role = "ControlType.Edit";
        internal int Activated, FocusCalls, Reads;
        internal Action<Request> OnRead, OnIdle, OnFocus;
        internal Func<NativeKeyEvent[], int> OnSend;
        internal readonly List<NativeKeyEvent[]> Batches = new List<NativeKeyEvent[]>();
        public virtual NativeKeyboardState Read(Dictionary<string, object> target, string[] within, Request r, bool text = false) {
            Reads++; if (OnRead != null) OnRead(r); r.Check();
            if (!Alive) throw new Failure("stale_target", "fixture target retired");
            if (within.Any(x => x != "parent")) throw new Failure("stale_target", "fixture ancestor moved");
            return new NativeKeyboardState { Native = this, Role = target["node"] == null ? null : Role, Label = "Fixture keyboard target",
                Enabled = Enabled, Focusable = Focusable, Foreground = Foreground, WindowRoute = Foreground && Route, Focused = target["node"] == null || Focused };
        }
        public void RequireIdle(NativeKeyEvent[] events, Request r) {
            if (OnIdle != null) OnIdle(r); r.Check();
            if (Held) throw new Failure("input_unavailable", "user input held");
        }
        public void Activate(NativeKeyboardState state, Request r) { Activated++; r.Check(); Foreground = Route = true; }
        public void Focus(NativeKeyboardState state, Request r) { FocusCalls++; if (OnFocus != null) OnFocus(r); r.Check(); Focused = FocusWorks; }
        public virtual int Send(NativeKeyEvent[] events) { Batches.Add(events); return OnSend == null ? events.Length : OnSend(events); }
    }
    internal sealed class KeyboardProbeRead : INativeReadBackend {
        readonly ObjectProbeRead reader;
        internal KeyboardProbeRead(ObjectProbe objects) { reader = new ObjectProbeRead(objects); }
        public void Validate(Dictionary<string, object> target, Request r) { reader.Validate(target, r); }
        public void Release() { reader.Release(); }
        public object Inspect(Dictionary<string, object> target, Dictionary<string, object> query, Request r) {
            var result = Wire.Object(reader.Inspect(target, query, r));
            if ((string)query["kind"] == "nodes" && Wire.Array(query["fields"]).Contains("IsKeyboardFocusable")) {
                var node = Wire.Object(Wire.Array(result["nodes"])[0]);
                Wire.Object(node["values"])["IsKeyboardFocusable"] = true;
                Wire.Object(node["fieldStatus"]).Remove("IsKeyboardFocusable");
            }
            return result;
        }
    }
    internal static class NativeKeyboardTests {
        static int assertions;
        static Request Request(int ms = 5000) { return new Request(CancellationToken.None, ms); }
        static void Assert(bool ok, string detail) { assertions++; if (!ok) throw new Exception(detail); }
        static void Refused(string code, Action call) {
            try { call(); } catch (Failure error) { Assert(error.Code == code, "Expected " + code + "; got " + error.Code); return; }
            throw new Exception("Expected " + code);
        }
        static Dictionary<string, object> Target(string node = "node") {
            return Wire.Obj("session", "session", "instance", "instance", "rootWindow", "window", "surface", "surface", "node", node, "capture", null);
        }
        static NativeRuntime Runtime(KeyboardProbe keys, ObjectProbe objects, string mode = "shared", bool assist = false) {
            return new NativeRuntime("session", new KeyboardProbeRead(objects), new NativeExecution(objects, new NativeKeyboard(keys, mode, assist)), new CaptureProbe());
        }
        static string Begin(NativeRuntime runtime, string kind = "key", string key = "Return", string node = "node", Request r = null) {
            return (string)Wire.Object(runtime.Perform("begin", Wire.Obj("mode", "write", "target", Target(node),
                "intent", Wire.Obj("kind", kind, "parameters", kind == "focus" ? Wire.Obj() : Wire.Obj("key", key))), r ?? Request()))["operation"];
        }
        static Dictionary<string, object> Inspect(NativeRuntime runtime, string operation, string node = "node", string[] within = null) {
            return Wire.Object(runtime.Perform("inspect", Wire.Obj("operation", operation, "target", Target(node),
                "query", Wire.Obj("kind", "inputContext", "within", within ?? new string[0])), Request()));
        }
        static Dictionary<string, object> Command(Dictionary<string, object> facts, bool prepare = false, bool foreground = false) {
            var p = Wire.Object(Wire.Array(facts["primitives"])[0]);
            return Wire.Obj("kind", prepare ? "prepare_receiver" : p["unit"], "primitiveId", p["id"], "target", facts["binding"], "foreground", foreground);
        }
        static Dictionary<string, object> Execute(NativeRuntime runtime, string operation, object command, int step = 1, Request r = null) {
            return Wire.Object(runtime.Perform("execute", Wire.Obj("operation", operation, "step", step, "command", command), r ?? Request()));
        }
        static object Finish(NativeRuntime runtime, string operation) { return runtime.Perform("finish", Wire.Obj("operation", operation), Request()); }
        static object Decode(JsonElement value) {
            switch (value.ValueKind) {
                case JsonValueKind.Object: return value.EnumerateObject().ToDictionary(p => p.Name, p => Decode(p.Value));
                case JsonValueKind.Array: return value.EnumerateArray().Select(Decode).ToArray();
                case JsonValueKind.String: return value.GetString();
                case JsonValueKind.Number: return value.GetInt64();
                case JsonValueKind.True: return true;
                case JsonValueKind.False: return false;
                default: return null;
            }
        }
        static int Serve(string journal, bool assist) {
            var keys = new KeyboardProbe(); var objects = new ObjectProbe(); var runtime = Runtime(keys, objects, assist: assist);
            try { string line; while ((line = Console.ReadLine()) != null) {
                File.AppendAllText(journal, line + "\n");
                using (var parsed = JsonDocument.Parse(line)) {
                    var request = Wire.Object(Decode(parsed.RootElement)); if (!request.ContainsKey("method")) continue;
                    var reply = Wire.Obj("version", 5, "session", "session", "id", request["id"]);
                    try { reply["result"] = (string)request["method"] == "hello"
                        ? Wire.Obj("platform", "windows", "desktopMode", "shared", "maxTimeoutMs", 120000, "capabilities", runtime.Capabilities)
                        : runtime.Perform((string)request["method"], Wire.Object(request["params"]), Request((int)(long)request["timeoutMs"]));
                    } catch (Failure error) { reply["error"] = Wire.Obj("code", error.Code, "effect", "none", "target", "fixture", "detail", error.Message, "next", error.Next); }
                    File.WriteAllText(journal + ".effects.json", JsonSerializer.Serialize(new { sent = keys.Batches.Count, activated = keys.Activated,
                        focused = keys.FocusCalls, foreground = keys.Foreground, objectSent = objects.Sent, acquired = objects.Acquired, released = objects.Released }));
                    Console.WriteLine(JsonSerializer.Serialize(reply)); Console.Out.Flush();
                }
            } } finally { runtime.Close(); }
            return 0;
        }
        static int Main(string[] args) {
            if (args.Length == 3 && args[0] == "--serve") return Serve(args[1], args[2] == "assist");
            var keys = new KeyboardProbe(); var objects = new ObjectProbe(); var runtime = Runtime(keys, objects);
            Assert(runtime.Capabilities.Contains("nativeKeyboardIntents") && !runtime.Capabilities.Contains("nativeTextInput"), "unsupported typing advertised");
            Refused("invalid_request", () => Begin(runtime, key: "ctrl+ctrl+p"));
            Refused("invalid_request", () => Begin(runtime, key: "shift+"));
            Refused("invalid_request", () => Begin(runtime, key: "F01"));
            Refused("mode_unsupported", () => Begin(runtime, key: "alt+Tab"));
            Refused("mode_unsupported", () => Begin(runtime, key: "super+p"));
            Refused("mode_unsupported", () => Begin(runtime, key: "super"));
            Refused("invalid_request", () => Begin(runtime, key: "ctrl+super"));
            Assert(objects.Acquired == 0 && keys.Reads == 0, "invalid chord caused preparation");
            var chord = NativeKeys.Chord("ctrl+shift+Left", "shared");
            Assert(chord.Length == 6 && chord[2].Extended && chord[3].Up && chord[4].Code == 16 && chord[5].Code == 17, "chord pairing/extended flags wrong");
            Assert(NativeKeys.Chord("super+p", "exclusive")[0].Extended, "Windows key needs extended flag");
            var start = NativeKeys.Chord("super", "exclusive");
            Assert(start.Length == 2 && start.All(e => e.Code == 91 && e.Extended && !e.Unicode)
                && !start[0].Up && start[1].Up, "standalone Windows key must have one paired down/up");

            string op = Begin(runtime); var facts = Inspect(runtime, op); var primitive = Wire.Object(Wire.Array(facts["primitives"])[0]);
            Assert((string)primitive["backgroundPreparation"] == "no" && Wire.Array(primitive["pending"]).Contains("foreground"), "background global keys claimed ready");
            var r = Request(); var receipt = Execute(runtime, op, Command(facts, true), r: r);
            Assert((string)receipt["retryReason"] == "preparationUnavailable" && !r.Effect && keys.Activated == 0 && keys.FocusCalls == 0, "background probe activated target");
            receipt = Execute(runtime, op, Command(facts, true, true), 2);
            Assert((string)receipt["errorCode"] == "mode_unsupported" && keys.Activated == 0, "client flag bypassed host policy");
            receipt = Execute(runtime, op, Command(facts), 3);
            Assert((string)receipt["dispatch"] == "notDispatched" && keys.Batches.Count == 0, "background key dispatched");
            receipt = Execute(runtime, op, Command(facts, foreground: true), 4);
            Assert((string)receipt["errorCode"] == "invalid_request", "dispatch allowed implicit activation"); Finish(runtime, op);

            keys.Foreground = keys.Route = true;
            // A stale UIA focus declaration is not a usable native input queue.
            keys.Focused = true; keys.Route = false; keys.OnFocus = request => keys.Route = true;
            op = Begin(runtime, "focus"); facts = Inspect(runtime, op); receipt = Execute(runtime, op, Command(facts, true));
            Assert((string)receipt["confirmation"] == "confirmed" && keys.FocusCalls == 1 && keys.Batches.Count == 0, "UIA/native queue disagreement prevented focus repair");
            Finish(runtime, op); keys.OnFocus = null; keys.FocusCalls = 0; keys.Focused = false;
            op = Begin(runtime, "focus"); facts = Inspect(runtime, op); r = Request(); receipt = Execute(runtime, op, Command(facts, true), r: r);
            Assert((string)receipt["confirmation"] == "confirmed" && (string)receipt["dispatch"] == "notDispatched" && r.Effect && !r.Dispatched, "focus preparation confused with requested key");
            Assert(keys.FocusCalls == 1 && keys.Activated == 0 && keys.Batches.Count == 0, "foreground target preparation activated or typed"); Finish(runtime, op);
            op = Begin(runtime); facts = Inspect(runtime, op);
            Refused("stale_target", () => Inspect(runtime, op, "other"));
            var wrong = Command(facts); wrong["target"] = Target("other"); receipt = Execute(runtime, op, wrong);
            Assert((string)receipt["errorCode"] == "stale_target" && keys.Batches.Count == 0, "contract changed target");
            keys.OnIdle = request => keys.Focused = false;
            receipt = Execute(runtime, op, Command(facts), 2);
            Assert((string)receipt["dispatch"] == "notDispatched" && keys.Batches.Count == 0, "idle callback focus change escaped final guard");
            keys.OnIdle = null; keys.Focused = true; keys.Held = true;
            receipt = Execute(runtime, op, Command(facts), 3);
            Assert((string)receipt["dispatch"] == "notDispatched" && keys.Batches.Count == 0, "held key was overwritten");
            keys.Held = false; keys.Role = "ControlType.Button";
            receipt = Execute(runtime, op, Command(facts), 4);
            Assert((string)receipt["dispatch"] == "notDispatched", "changed role dispatched old contract");
            keys.Role = "ControlType.Edit"; keys.OnSend = events => { keys.Alive = false; return events.Length; };
            var command = Command(facts); receipt = Execute(runtime, op, command, 5);
            Assert((string)receipt["dispatch"] == "dispatched" && keys.Batches.Count == 1, "Return closing target invalidated successful dispatch");
            Assert(ReferenceEquals(receipt, Execute(runtime, op, command, 5)) && keys.Batches.Count == 1, "duplicate step replayed key");
            Refused("execution_unknown", () => Execute(runtime, op, command, 6)); Finish(runtime, op);
            keys.OnSend = null; keys.Alive = true;

            op = Begin(runtime, node: null); facts = Inspect(runtime, op, null);
            Refused("invalid_request", () => Inspect(runtime, op, null, new[] { "parent" }));
            Refused("stale_target", () => Inspect(runtime, op, "node"));
            receipt = Execute(runtime, op, Command(facts));
            Assert((string)receipt["dispatch"] == "dispatched" && keys.Batches.Count == 2, "window shortcut required a synthetic element"); Finish(runtime, op);
            op = Begin(runtime); command = Command(Inspect(runtime, op)); keys.OnSend = events => keys.Batches.Count == 3 ? 1 : events.Length;
            receipt = Execute(runtime, op, command);
            Assert((string)receipt["dispatch"] == "unknown" && (string)receipt["cleanup"] == "complete" && keys.Batches.Count == 4 && keys.Batches.Last().All(e => e.Up), "partial injection missing paired cleanup");
            Execute(runtime, op, command); Assert(keys.Batches.Count == 4, "partial chord replayed"); Finish(runtime, op); keys.OnSend = null;

            var cancel = new CancellationTokenSource(); op = Begin(runtime); command = Command(Inspect(runtime, op));
            keys.OnSend = events => { cancel.Cancel(); return events.Length; };
            receipt = Execute(runtime, op, command, r: new Request(cancel.Token, 1000));
            Assert((string)receipt["dispatch"] == "dispatched" && (string)receipt["errorCode"] == "cancelled", "late cancellation erased dispatch receipt");
            Finish(runtime, op); keys.OnSend = null;

            // Cancellation and native exceptions cannot cancel the paired release.
            var interrupted = new CancellationTokenSource(); op = Begin(runtime, key: "ctrl+p"); command = Command(Inspect(runtime, op));
            keys.OnSend = events => {
                if (events.Any(e => !e.Up)) { interrupted.Cancel(); throw new Exception("reply lost after key down"); }
                return events.Length;
            };
            receipt = Execute(runtime, op, command, r: new Request(interrupted.Token, 1000));
            Assert((string)receipt["dispatch"] == "unknown" && (string)receipt["cleanup"] == "complete", "cancelled injection skipped cleanup");
            Assert(keys.Batches.Last().All(e => e.Up) && keys.Batches.Last().Select(e => e.Code).SequenceEqual(new ushort[] { 80, 17 }), "owned releases not reversed");
            Finish(runtime, op); keys.OnSend = null;

            keys.Focused = false; keys.FocusWorks = false; op = Begin(runtime, "focus", r: Request(30)); command = Command(Inspect(runtime, op));
            receipt = Execute(runtime, op, command);
            Assert((string)receipt["dispatch"] == "notDispatched" && (string)receipt["errorCode"] == "deadline_exceeded", "focus did not honor original deadline");
            Finish(runtime, op); keys.FocusWorks = true;
            Assert(objects.Acquired == objects.Released, "completed operations leaked desktop lease");

            var assistKeys = new KeyboardProbe(); var assistObjects = new ObjectProbe(); var assisted = Runtime(assistKeys, assistObjects, assist: true);
            op = Begin(assisted); facts = Inspect(assisted, op); receipt = Execute(assisted, op, Command(facts, true, true));
            Assert((string)receipt["confirmation"] == "confirmed" && assistKeys.Activated == 1 && assistKeys.FocusCalls == 1 && assistKeys.Batches.Count == 0, "authorized preparation did not establish exact recipient");
            command = Command(Inspect(assisted, op)); receipt = Execute(assisted, op, command, 2);
            Assert((string)receipt["dispatch"] == "dispatched" && assistKeys.Activated == 1 && assistKeys.Foreground, "dispatch reactivated or restored foreground"); Finish(assisted, op);
            assistKeys.Foreground = assistKeys.Route = assistKeys.Focused = false;
            op = Begin(assisted); facts = Inspect(assisted, op); assistKeys.OnFocus = request => assistKeys.Alive = false;
            receipt = Execute(assisted, op, Command(facts, true, true));
            Assert((string)receipt["dispatch"] == "notDispatched" && (string)receipt["errorCode"] == "stale_target" && assistKeys.Batches.Count == 1, "focus provider retargeted input"); Finish(assisted, op);

            var failedKeys = new KeyboardProbe { Foreground = true, Route = true, Focused = true }; var failedObjects = new ObjectProbe();
            var failed = Runtime(failedKeys, failedObjects); op = Begin(failed, key: "ctrl+p"); command = Command(Inspect(failed, op));
            failedKeys.OnSend = events => 0; receipt = Execute(failed, op, command);
            Assert((string)receipt["dispatch"] == "unknown" && (string)receipt["cleanup"] == "failed", "uncertain release claimed cleanup");
            Assert((string)Wire.Object(Finish(failed, op))["cleanup"] == "failed" && failedObjects.Acquired == failedObjects.Released, "failed cleanup lost at finish");
            Refused("execution_unknown", () => Begin(failed));
            Console.WriteLine(JsonSerializer.Serialize(new { assertions = assertions, facts = facts, receipt = receipt,
                scope = "Production keyboard unit and operation runtime with fake native providers; no Windows GUI execution" }));
            return 0;
        }
    }
}
