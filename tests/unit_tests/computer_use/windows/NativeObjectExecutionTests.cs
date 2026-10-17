// Production v5 runtime/object-unit probe with explicit fake native objects.
// No UIA adapter, Win32 input, GDI pixels or Framework serializer is executed.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Text.Json;

namespace ComputerUse.Windows {
    internal sealed class ObjectProbe : INativeObjectProvider {
        internal int Acquired, Released, Reads, Sent;
        internal bool Enabled = true, Writable = true, Alive = true, FailRelease;
        internal string Text = "initial";
        internal Action BeforeRead, BeforePrepare, OnSend;
        internal readonly string[] Actions = { "Invoke.Invoke", "Toggle.Toggle", "ScrollItem.ScrollIntoView" };
        sealed class Lease : IDisposable {
            readonly ObjectProbe owner;
            internal Lease(ObjectProbe owner) { this.owner = owner; }
            public void Dispose() { owner.Released++; if (owner.FailRelease) throw new Exception("lease release failed"); }
        }
        public IDisposable Acquire(Request r) { r.Check(); Acquired++; return new Lease(this); }
        public NativeObjectState Read(Dictionary<string, object> target, string[] within, Request r) {
            Reads++; if (BeforeRead != null) BeforeRead(); r.Check();
            if (!Alive) throw new Failure("stale_target", "fake object retired");
            if (within.Any(x => x != "parent")) throw new Failure("stale_target", "fake ancestor changed");
            return new NativeObjectState { Native = this, Role = "ControlType.Edit", Label = "Fixture field",
                Enabled = Enabled, Replaceable = Writable, Actions = Actions, ActionsComplete = true };
        }
        public Action Prepare(NativeObjectState state, string unit, string action, string text, Request r) {
            if (BeforePrepare != null) BeforePrepare(); r.Check();
            return () => { Sent++; if (unit == "native_text_edit") Text = text; if (OnSend != null) OnSend(); };
        }
    }
    internal sealed class ObjectProbeRead : INativeReadBackend {
        readonly ObjectProbe owner;
        readonly ReadProbeBackend directory = new ReadProbeBackend();
        internal bool Reject, FailRelease;
        internal ObjectProbeRead(ObjectProbe owner) { this.owner = owner; }
        public void Validate(Dictionary<string, object> target, Request r) { r.Check(); if (Reject) throw new Failure("stale_target", "fixture refused"); }
        public void Release() { if (FailRelease) throw new Exception("pin cleanup failed"); }
        public object Inspect(Dictionary<string, object> target, Dictionary<string, object> query, Request r) {
            var result = Wire.Object(directory.Inspect(target, query, r));
            if ((string)query["kind"] != "nodes") return result;
            var node = Wire.Object(Wire.Array(result["nodes"])[0]); var values = Wire.Obj(); var status = Wire.Obj();
            foreach (string field in Wire.Array(query["fields"])) {
                if (field == "ControlType") values[field] = "ControlType.Edit";
                else if (field == "Name") values[field] = "Fixture field";
                else if (field == "Value.Value") values[field] = owner.Text;
                else if (field == "Value.IsReadOnly") values[field] = !owner.Writable;
                else if (field == "IsEnabled") values[field] = owner.Enabled;
                else if (field == "SupportedPatterns") values[field] = new[] { "ValuePatternIdentifiers.Pattern", "InvokePatternIdentifiers.Pattern" };
                else status[field] = Wire.Obj("state", "unsupported");
            }
            node["values"] = values; node["fieldStatus"] = status;
            node["actions"] = Wire.Obj("state", "complete", "items", owner.Actions.Select(a => Wire.Obj("name", a, "description", a)).ToArray(), "total", owner.Actions.Length);
            return result;
        }
    }
    internal sealed class CaptureProbe : INativeCaptureBackend {
        internal int Calls;
        internal Action<Request> Before;
        public object Capture(Dictionary<string, object> target, string kind, int maxDimension, Request r) {
            Calls++; if (Before != null) Before(r); r.Check();
            var stamp = Wire.Obj("id", "observation", "startedAt", 1, "completedAt", 3);
            string scope = kind == "window" ? (string)target["surface"] : "session:desktop";
            var image = Wire.Obj("id", "capture", "kind", kind, "scope", scope, "startedAt", 1, "completedAt", 2, "width", 1, "height", 1,
                "png", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=");
            if (kind == "window") return Wire.Obj("kind", "capture", "binding", target, "observed", stamp, "image", image,
                "coordinates", Wire.Obj("space", "window-pixels", "origin", "top-left", "nativeSpace", "screen-pixels", "toNative", new[] { 1, 0, 0, 1, -200, 50 }));
            return Wire.Obj("kind", "desktopCapture", "binding", target, "observed", stamp, "desktopRef", "capture", "scope", scope, "screenshot", image,
                "observation", Wire.Obj("mode", "shared", "canvas", Wire.Obj("width", 1, "height", 1), "frontmost", null, "displays", new[] {
                    Wire.Obj("name", "Fixture", "rect", Wire.Obj("x", 0, "y", 0, "width", 1, "height", 1), "captured", true) }, "windows", new object[0], "omittedWindows", 0));
        }
    }
    internal static class NativeObjectExecutionTests {
        static int assertions;
        static void Assert(bool condition, string reason) { assertions++; if (!condition) throw new Exception(reason); }
        static void Refused(string code, Action action) {
            try { action(); } catch (Failure error) { Assert(error.Code == code, "Expected " + code + ", got " + error.Code); return; }
            throw new Exception("Expected refusal " + code);
        }
        static Request Request(int timeout = 10000) { return new Request(CancellationToken.None, timeout); }
        static Dictionary<string, object> Target(string node = "node", bool window = true) {
            return Wire.Obj("session", "session", "instance", window ? "instance" : null, "rootWindow", window ? "window" : null,
                "surface", window ? "surface" : null, "node", node, "capture", null);
        }
        static Dictionary<string, object> Intent(string kind, object parameters = null) { return Wire.Obj("kind", kind, "parameters", parameters ?? Wire.Obj()); }
        static string Begin(NativeRuntime runtime, Dictionary<string, object> target, string kind = "click", object parameters = null, Request request = null) {
            return (string)Wire.Object(runtime.Perform("begin", Wire.Obj("mode", kind == "read" ? "read" : "write", "target", target,
                "intent", Intent(kind, parameters)), request ?? Request()))["operation"];
        }
        static Dictionary<string, object> Inspect(NativeRuntime runtime, string operation, Dictionary<string, object> target, string[] within = null) {
            return Wire.Object(runtime.Perform("inspect", Wire.Obj("operation", operation, "target", target,
                "query", Wire.Obj("kind", "inputContext", "within", within ?? new string[0])), Request()));
        }
        static Dictionary<string, object> Command(Dictionary<string, object> facts, string action = null) {
            var item = Wire.Array(facts["primitives"]).Select(Wire.Object).First(p => action == null || (string)Wire.Object(p["action"])["name"] == action);
            return Wire.Obj("kind", item["unit"], "primitiveId", item["id"], "target", facts["binding"], "foreground", false);
        }
        static Dictionary<string, object> Execute(NativeRuntime runtime, string operation, object command, int step = 1, Request request = null) {
            return Wire.Object(runtime.Perform("execute", Wire.Obj("operation", operation, "step", step, "command", command), request ?? Request()));
        }
        static object Finish(NativeRuntime runtime, string operation) { return runtime.Perform("finish", Wire.Obj("operation", operation), Request()); }
        static Dictionary<string, object> Capture(NativeRuntime runtime, string operation, Dictionary<string, object> target, string kind, Request request = null) {
            return Wire.Object(runtime.Perform("capture", Wire.Obj("operation", operation, "target", target, "kind", kind, "maxDimension", 1024), request ?? Request()));
        }
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
        static int Serve(string journal) {
            var provider = new ObjectProbe(); var runtime = new NativeRuntime("session", new ObjectProbeRead(provider), new NativeExecution(provider), new CaptureProbe());
            try { string line; while ((line = Console.ReadLine()) != null) {
                File.AppendAllText(journal, line + "\n");
                using (var parsed = JsonDocument.Parse(line)) {
                    var request = Wire.Object(Decode(parsed.RootElement)); if (!request.ContainsKey("method")) continue;
                    var reply = Wire.Obj("version", 5, "session", "session", "id", request["id"]);
                    try { reply["result"] = (string)request["method"] == "hello"
                        ? Wire.Obj("platform", "windows", "desktopMode", "shared", "maxTimeoutMs", 120000, "capabilities", runtime.Capabilities)
                        : runtime.Perform((string)request["method"], Wire.Object(request["params"]), Request((int)(long)request["timeoutMs"]));
                    } catch (Failure error) { reply["error"] = Wire.Obj("code", error.Code, "effect", "none", "target", "fixture", "detail", error.Message, "next", error.Next); }
                    File.WriteAllText(journal + ".effects.json", JsonSerializer.Serialize(new { text = provider.Text, sent = provider.Sent, acquired = provider.Acquired, released = provider.Released }));
                    Console.WriteLine(JsonSerializer.Serialize(reply)); Console.Out.Flush();
                }
            } } finally { runtime.Close(); }
            File.WriteAllText(journal + ".effects.json", JsonSerializer.Serialize(new { text = provider.Text, sent = provider.Sent, acquired = provider.Acquired, released = provider.Released }));
            return 0;
        }
        static int Main(string[] args) {
            if (args.Length == 2 && args[0] == "--serve") return Serve(args[1]);
            var provider = new ObjectProbe(); var reader = new ObjectProbeRead(provider); var capture = new CaptureProbe();
            var runtime = new NativeRuntime("session", reader, new NativeExecution(provider), capture);
            Assert(runtime.Capabilities.Contains("nativeActionIntents") && runtime.Capabilities.Contains("nativeCapture"), "missing declared implementations");
            foreach (var bad in new[] { Intent("insert", Wire.Obj("text", "x")), Intent("paste", Wire.Obj("text", "x")), Intent("key", Wire.Obj("key", "Return")) })
                Refused("mode_unsupported", () => NativeExecution.ValidateIntent(bad));
            Refused("invalid_request", () => NativeExecution.ValidateIntent(Intent("replace", Wire.Obj("text", "a\nb"))));
            Refused("invalid_request", () => NativeExecution.ValidateIntent(Intent("replace", Wire.Obj("text", "\ud800"))));
            Refused("mode_unsupported", () => NativeExecution.ValidateIntent(Intent("click", Wire.Obj("button", "right"))));
            NativeExecution.ValidateIntent(Intent("replace", Wire.Obj("text", "你好 😀")));
            string operation = Begin(runtime, Target());
            Assert(provider.Acquired == 1 && provider.Released == 0, "write lease not held across RPC");
            Refused("busy", () => Begin(runtime, Target())); Assert(provider.Acquired == 1, "busy acquired another lease");
            Refused("invalid_request", () => Capture(runtime, operation, Target(null), "window"));
            var facts = Inspect(runtime, operation, Target(), new[] { "parent" });
            Refused("stale_target", () => Inspect(runtime, operation, Target("other"), new[] { "parent" }));
            Refused("stale_target", () => Inspect(runtime, operation, Target(), new string[0]));
            var command = Command(facts, "Invoke.Invoke"); var receipt = Execute(runtime, operation, command);
            Assert((string)receipt["dispatch"] == "dispatched" && provider.Sent == 1, "action did not dispatch once");
            Assert(ReferenceEquals(receipt, Execute(runtime, operation, command)) && provider.Sent == 1, "step replay repeated native input");
            var different = new Dictionary<string, object>(command); different["foreground"] = true;
            Refused("request_conflict", () => Execute(runtime, operation, different));
            Refused("execution_unknown", () => Execute(runtime, operation, command, 2));
            Finish(runtime, operation); Finish(runtime, operation); Assert(provider.Released == 1, "finish duplicated cleanup");
            operation = Begin(runtime, Target(), "replace", Wire.Obj("text", "new 😀"));
            facts = Inspect(runtime, operation, Target()); receipt = Execute(runtime, operation, Command(facts));
            Assert(provider.Text == "new 😀" && (string)receipt["dispatch"] == "dispatched", "replace did not retain original payload"); Finish(runtime, operation);
            operation = Begin(runtime, Target(), "replace", Wire.Obj("text", ""));
            command = Command(Inspect(runtime, operation, Target())); provider.Writable = false;
            receipt = Execute(runtime, operation, command);
            Assert((string)receipt["dispatch"] == "notDispatched" && provider.Text == "new 😀", "readonly change sent input");
            Finish(runtime, operation); provider.Writable = true;
            operation = Begin(runtime, Target()); command = Command(Inspect(runtime, operation, Target()));
            provider.BeforePrepare = () => provider.Alive = false; int sent = provider.Sent;
            receipt = Execute(runtime, operation, command);
            Assert((string)receipt["dispatch"] == "notDispatched" && (string)receipt["errorCode"] == "stale_target" && provider.Sent == sent, "prepare callback retargeted input");
            Finish(runtime, operation); provider.BeforePrepare = null; provider.Alive = true;
            operation = Begin(runtime, Target()); command = Command(Inspect(runtime, operation, Target()));
            provider.OnSend = () => { throw new Exception("provider reply lost"); };
            receipt = Execute(runtime, operation, command);
            Assert((string)receipt["dispatch"] == "unknown" && provider.Sent == ++sent, "uncertain mutation hidden");
            Execute(runtime, operation, command); Assert(provider.Sent == sent, "uncertain mutation replayed"); Finish(runtime, operation); provider.OnSend = null;
            operation = Begin(runtime, Target()); command = Command(Inspect(runtime, operation, Target()));
            using (var cancel = new CancellationTokenSource()) {
                provider.OnSend = () => cancel.Cancel();
                receipt = Execute(runtime, operation, command, request: new Request(cancel.Token, 10000));
                Assert((string)receipt["dispatch"] == "dispatched" && (string)receipt["errorCode"] == "cancelled", "post-dispatch cancel erased effect");
            }
            Finish(runtime, operation); provider.OnSend = null;
            var shortRequest = Request(30); operation = Begin(runtime, Target(), request: shortRequest); Thread.Sleep(50); runtime.Expire();
            Finish(runtime, operation); Assert(provider.Acquired == provider.Released, "idle expiry leaked lease");
            operation = Begin(runtime, Target(null), "read"); var windowCapture = Capture(runtime, operation, Target(null), "window");
            Refused("invalid_request", () => Capture(runtime, operation, Target("node"), "window"));
            Refused("invalid_request", () => Capture(runtime, operation, Target(null), "desktop"));
            Finish(runtime, operation);
            var deadline = Request(10000); operation = Begin(runtime, Target(null, false), "read", request: deadline);
            capture.Before = r => Assert(r.Deadline <= deadline.Deadline, "capture renewed operation deadline");
            var desktopCapture = Capture(runtime, operation, Target(null, false), "desktop"); capture.Before = null; Finish(runtime, operation);
            operation = Begin(runtime, Target(null), "read"); capture.Before = r => r.Deadline = Wire.Now - 1;
            Refused("deadline_exceeded", () => Capture(runtime, operation, Target(null), "window")); Finish(runtime, operation); capture.Before = null;
            reader.Reject = true; int releases = provider.Released;
            Refused("stale_target", () => Begin(runtime, Target())); Assert(provider.Released == releases+1, "failed begin leaked lease"); reader.Reject = false;
            operation = Begin(runtime, Target()); provider.FailRelease = true;
            Assert((string)Wire.Object(Finish(runtime, operation))["cleanup"] == "failed", "cleanup failure hidden");
            Refused("execution_unknown", () => Begin(runtime, Target()));
            var badReader = new ObjectProbeRead(provider) { Reject = true, FailRelease = true };
            var badRuntime = new NativeRuntime("session", badReader);
            Refused("stale_target", () => Begin(badRuntime, Target(null), "read")); badReader.Reject = badReader.FailRelease = false;
            Refused("execution_unknown", () => Begin(badRuntime, Target(null), "read"));
            Console.WriteLine(JsonSerializer.Serialize(new { assertions = assertions, facts = facts, receipt = receipt,
                windowCapture = windowCapture, desktopCapture = desktopCapture,
                scope = "Production v5 lifecycle and object units, fake native providers; no Windows GUI execution" }));
            return 0;
        }
    }
}
