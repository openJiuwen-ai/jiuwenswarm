// Production text recipe and Python/wire integration with fake native objects.
// The effects below are fixture effects, not Windows UIA/SendInput acceptance.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Text.Json;

namespace ComputerUse.Windows {
    internal sealed class TextProbe : KeyboardProbe, INativeKeyboardTextProvider {
        internal readonly ObjectProbe Objects;
        internal bool? Writable = true;
        internal bool Selectable = true, Full, SelectWorks = true;
        internal int Caret, Recipient, Selections, Submitted;
        internal Action AfterSend, OnSelection, OnSelect;
        internal TextProbe(ObjectProbe objects) { Objects = objects; Caret = objects.Text.Length; }
        public override NativeKeyboardState Read(Dictionary<string, object> target, string[] within, Request r, bool text = false) {
            var state = base.Read(target, within, r, text);
            state.TextWritable = Writable; state.TextSelection = Selectable; return state;
        }
        sealed class Sequence : INativeKeyboardSequence {
            readonly TextProbe owner; readonly int recipient;
            internal Sequence(TextProbe owner) { this.owner = owner; recipient = owner.Recipient; }
            public void Validate(Request r) {
                r.Check();
                if (owner.Recipient != recipient) throw new Failure("input_unavailable", "fixture input recipient changed");
            }
        }
        public INativeKeyboardSequence BeginSequence(NativeKeyboardState state, Request r) { r.Check(); return new Sequence(this); }
        public NativeTextSelection Selection(NativeKeyboardState state, Request r) {
            if (OnSelection != null) OnSelection(); r.Check();
            return new NativeTextSelection { Empty = Objects.Text.Length == 0, Full = Full };
        }
        public void SelectAll(NativeKeyboardState state, Request r) {
            Selections++; if (OnSelect != null) OnSelect(); r.Check();
            if (SelectWorks) Full = true;
        }
        public override int Send(NativeKeyEvent[] events) {
            int count = base.Send(events);
            if (count == events.Length && events.Any(e => !e.Up)) {
                string text = new String(events.Where(e => e.Unicode && !e.Up).Select(e => (char)e.Code).ToArray());
                if (text.Length > 0) {
                    if (Full) { Objects.Text = ""; Caret = 0; Full = false; }
                    Objects.Text = Objects.Text.Insert(Caret, text); Caret += text.Length;
                } else if (events.Any(e => !e.Unicode && !e.Up && e.Code == 8)) {
                    if (Full) { Objects.Text = ""; Full = false; Caret = 0; }
                    else if (Caret > 0) { Objects.Text = Objects.Text.Remove(--Caret, 1); }
                } else if (events.Any(e => !e.Unicode && !e.Up && e.Code == 13)) Submitted++;
                if (AfterSend != null) AfterSend();
            }
            return count;
        }
    }
    internal sealed class TextProbeRead : INativeReadBackend {
        readonly ObjectProbeRead reader;
        readonly TextProbe keys;
        internal TextProbeRead(TextProbe keys) { this.keys = keys; reader = new ObjectProbeRead(keys.Objects); }
        public void Validate(Dictionary<string, object> target, Request r) { reader.Validate(target, r); }
        public void Release() { reader.Release(); }
        public object Inspect(Dictionary<string, object> target, Dictionary<string, object> query, Request r) {
            var result = Wire.Object(reader.Inspect(target, query, r));
            if ((string)query["kind"] == "nodes") {
                var node = Wire.Object(Wire.Array(result["nodes"])[0]); var values = Wire.Object(node["values"]); var status = Wire.Object(node["fieldStatus"]);
                foreach (string field in Wire.Array(query["fields"])) {
                    object value;
                    if (field == "IsKeyboardFocusable") value = keys.Focusable;
                    else if (field == "Text.IsReadOnly") value = keys.Writable.HasValue ? (object)!keys.Writable.Value : "mixed";
                    else if (field == "Text.SupportedTextSelection") value = keys.Selectable ? "Single" : "None";
                    else if (field == "SupportedPatterns") value = keys.Objects.Writable
                        ? new[] { "ValuePatternIdentifiers.Pattern", "TextPatternIdentifiers.Pattern" } : new[] { "TextPatternIdentifiers.Pattern" };
                    else if (field == "Value.IsReadOnly" && !keys.Objects.Writable) { values.Remove(field); status[field] = Wire.Obj("state", "unsupported"); continue; }
                    else continue;
                    values[field] = value; status.Remove(field);
                }
            }
            return result;
        }
    }
    internal static class NativeTextTests {
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
        sealed class Case : IDisposable {
            internal readonly ObjectProbe Objects = new ObjectProbe { Writable = false };
            internal readonly TextProbe Keys;
            internal readonly NativeRuntime Runtime;
            internal string Operation;
            internal Dictionary<string, object> Facts, Command;
            internal Case(bool ready = true, bool assist = false) {
                Keys = new TextProbe(Objects) { Foreground = ready, Route = ready, Focused = ready };
                Runtime = new NativeRuntime("session", new TextProbeRead(Keys), new NativeExecution(Objects, new NativeKeyboard(Keys, "shared", assist)), new CaptureProbe());
            }
            internal void Start(string text, string kind = "insert", string node = "node", string unit = "key_sequence", Request request = null) {
                Operation = (string)Wire.Object(Runtime.Perform("begin", Wire.Obj("mode", "write", "target", Target(node),
                    "intent", Wire.Obj("kind", kind, "parameters", Wire.Obj("text", text))), request ?? Request()))["operation"];
                Facts = Wire.Object(Runtime.Perform("inspect", Wire.Obj("operation", Operation, "target", Target(node),
                    "query", Wire.Obj("kind", "inputContext", "within", new string[0])), Request()));
                var p = Wire.Array(Facts["primitives"]).Select(Wire.Object).First(x => (string)x["unit"] == unit);
                Command = Wire.Obj("kind", unit, "primitiveId", p["id"], "target", Facts["binding"], "foreground", false);
            }
            internal Dictionary<string, object> Send(int step = 1, Request request = null) {
                return Wire.Object(Runtime.Perform("execute", Wire.Obj("operation", Operation, "step", step, "command", Command), request ?? Request()));
            }
            internal void Finish() { Runtime.Perform("finish", Wire.Obj("operation", Operation), Request()); Operation = null; }
            public void Dispose() { Runtime.Close(); Assert(Objects.Acquired == Objects.Released, "desktop lease leaked"); }
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
        static int Serve(string journal, bool assist) {
            using (var c = new Case(false, assist)) {
                string line; while ((line = Console.ReadLine()) != null) {
                    File.AppendAllText(journal, line + "\n");
                    using (var parsed = JsonDocument.Parse(line)) {
                        var request = Wire.Object(Decode(parsed.RootElement)); if (!request.ContainsKey("method")) continue;
                        var reply = Wire.Obj("version", 5, "session", "session", "id", request["id"]);
                        try { reply["result"] = (string)request["method"] == "hello"
                            ? Wire.Obj("platform", "windows", "desktopMode", "shared", "maxTimeoutMs", 120000, "capabilities", c.Runtime.Capabilities)
                            : c.Runtime.Perform((string)request["method"], Wire.Object(request["params"]), Request((int)(long)request["timeoutMs"])); }
                        catch (Failure error) { reply["error"] = Wire.Obj("code", error.Code, "effect", "none", "target", "fixture", "detail", error.Message, "next", error.Next); }
                        File.WriteAllText(journal + ".effects.json", JsonSerializer.Serialize(new { text = c.Objects.Text, sent = c.Keys.Batches.Count,
                            selected = c.Keys.Selections, submitted = c.Keys.Submitted, activated = c.Keys.Activated, focused = c.Keys.FocusCalls,
                            objectSent = c.Objects.Sent, foreground = c.Keys.Foreground, acquired = c.Objects.Acquired, released = c.Objects.Released }));
                        Console.WriteLine(JsonSerializer.Serialize(reply));
                    }
                }
            }
            return 0;
        }
        static int Main(string[] args) {
            if (args.Length == 3 && args[0] == "--serve") return Serve(args[1], args[2] == "assist");
            Dictionary<string, object> sampleFacts = null, sampleReceipt = null;
            using (var c = new Case()) {
                Assert(c.Runtime.Capabilities.Contains("nativeTextInput"), "implemented typing not advertised");
                foreach (string text in new[] { "x\ny", "\t", "\u2028", "\ud800", "\udc00", new String('x', 1024 * 1024 + 1) })
                    Refused("invalid_request", () => c.Start(text));
                Assert(c.Objects.Acquired == 0 && c.Keys.Reads == 0, "invalid text caused preparation");
                c.Objects.Text = "ab"; c.Keys.Caret = 1; c.Start("中😀");
                sampleFacts = c.Facts; sampleReceipt = c.Send();
                Assert(c.Objects.Text == "a中😀b" && (string)sampleReceipt["dispatch"] == "dispatched", "insertion replaced contents or lost Unicode");
                Assert(c.Keys.Batches.Select(b => b.Length).SequenceEqual(new[] { 2, 4 }) && c.Keys.Batches.SelectMany(b => b).All(e => e.Unicode && !e.Extended), "surrogate pair split or interpreted as virtual keys");
                Assert(ReferenceEquals(c.Send(), sampleReceipt) && c.Keys.Batches.Count == 2, "duplicate operation retyped text");
                Refused("execution_unknown", () => c.Send(2));
            }
            using (var c = new Case()) {
                c.Start("new 😀", "replace"); var result = c.Send();
                Assert(c.Objects.Text == "new 😀" && c.Keys.Selections == 1 && (string)result["dispatch"] == "dispatched", "range replacement appended or selected twice");
                Assert(c.Keys.Batches.SelectMany(b => b).All(e => e.Unicode), "replacement used an unverified select-all shortcut");
            }
            using (var c = new Case()) {
                c.Keys.Full = true; c.Start("", "replace"); var result = c.Send();
                Assert(c.Objects.Text == "" && c.Keys.Batches.Count == 1 && c.Keys.Selections == 0, "clear did not delete selected range");
                Assert((string)result["dispatch"] == "dispatched", "deletion hidden"); c.Finish();
                c.Start("", "replace"); result = c.Send();
                Assert((string)result["confirmation"] == "confirmed" && (string)result["dispatch"] == "notDispatched" && c.Keys.Batches.Count == 1, "empty replacement sent an extra Backspace");
            }
            using (var c = new Case()) {
                c.Keys.Full = true; c.Start("", "replace");
                c.Keys.OnIdle = r => { c.Objects.Text = ""; c.Keys.Full = false; };
                var result = c.Send();
                Assert((string)result["dispatch"] == "notDispatched" && c.Keys.Batches.Count == 0, "stale empty-state sample caused an extra deletion");
            }
            using (var c = new Case()) {
                c.Start("new", "replace"); c.Keys.OnIdle = r => c.Keys.Full = false;
                var result = c.Send();
                Assert((string)result["dispatch"] == "unknown" && c.Keys.Selections == 1 && c.Keys.Batches.Count == 0 && c.Objects.Text == "initial", "selection loss typed into a partial range");
                Assert(result["retryReason"] == null, "selection side effect permitted replay");
            }
            using (var c = new Case()) {
                c.Keys.SelectWorks = false; c.Start("new", "replace", request: Request(80));
                var result = c.Send();
                Assert((string)result["errorCode"] == "deadline_exceeded" && (string)result["dispatch"] == "unknown" && c.Keys.Batches.Count == 0, "unconfirmed selection sent text or reset deadline");
            }
            using (var c = new Case()) {
                c.Start("unsafe"); c.Keys.Writable = false; var result = c.Send();
                Assert((string)result["dispatch"] == "notDispatched" && c.Keys.Batches.Count == 0, "read-only change sent text");
            }
            using (var c = new Case()) {
                c.Start("xy"); c.Keys.OnIdle = r => c.Keys.Recipient++;
                var result = c.Send();
                Assert((string)result["dispatch"] == "notDispatched" && c.Keys.Batches.Count == 0, "idle callback retargeted the first character");
            }
            foreach (string node in new[] { "node", null }) using (var c = new Case()) {
                c.Start("xyz", node: node); c.Keys.AfterSend = () => c.Keys.Recipient++;
                var result = c.Send();
                Assert((string)result["dispatch"] == "partial" && c.Keys.Batches.Count == 1 && c.Objects.Text == "initialx", "continuous input followed another recipient in the same window");
                Assert(result["retryReason"] == null, "partial text permitted fallback");
            }
            using (var c = new Case()) {
                c.Start("xy"); c.Keys.AfterSend = () => { c.Objects.Text += "[completion]"; c.Keys.Caret = c.Objects.Text.Length; };
                var result = c.Send();
                Assert((string)result["dispatch"] == "dispatched" && c.Objects.Text == "initialx[completion]y[completion]", "normal completion/value change was mistaken for recipient change");
            }
            using (var c = new Case()) using (var cancel = new CancellationTokenSource()) {
                c.Start("xy"); c.Keys.AfterSend = () => cancel.Cancel();
                var result = c.Send(request: new Request(cancel.Token, 1000));
                Assert((string)result["dispatch"] == "partial" && (string)result["errorCode"] == "cancelled" && c.Keys.Batches.Count == 1, "cancelled prefix sent remaining characters");
            }
            using (var c = new Case()) {
                c.Start("😀x"); c.Keys.OnSend = events => c.Keys.Batches.Count == 1 ? 1 : events.Length;
                var result = c.Send();
                Assert((string)result["dispatch"] == "unknown" && (string)result["cleanup"] == "complete" && c.Keys.Batches.Count == 2, "partial scalar not paired with cleanup");
                Assert(c.Keys.Batches.Last().All(e => e.Up && e.Unicode) && c.Keys.Batches.Last().Select(e => e.Code).SequenceEqual(new ushort[] { 0xde00, 0xd83d }), "surrogate cleanup changed key event type");
                c.Send(); Assert(c.Keys.Batches.Count == 2, "uncertain scalar replayed");
            }
            using (var c = new Case()) {
                c.Start("xy"); c.Keys.OnSend = events => 0;
                var result = c.Send();
                Assert((string)result["cleanup"] == "failed" && result["retryReason"] == null, "failed key release hidden");
                c.Finish(); Refused("execution_unknown", () => c.Start("new"));
            }
            using (var c = new Case(false)) {
                c.Start("x"); var result = c.Send();
                Assert((string)result["dispatch"] == "notDispatched" && c.Keys.Activated == 0 && c.Keys.Batches.Count == 0, "typing silently activated a background window");
            }
            using (var c = new Case(false)) {
                c.Objects.Writable = true; c.Keys.Writable = false;
                c.Start("native value", "replace", unit: "native_text_edit"); var result = c.Send();
                Assert((string)result["dispatch"] == "dispatched" && c.Objects.Text == "native value" && c.Keys.Activated == 0 && c.Keys.Batches.Count == 0, "unavailable keyboard blocked independent object replacement");
            }
            Console.WriteLine(JsonSerializer.Serialize(new { assertions = assertions, facts = sampleFacts, receipt = sampleReceipt,
                scope = "Production v5 text recipes with fake providers; Windows native adapter compile-checked separately, no Windows GUI execution" }));
            return 0;
        }
    }
}
