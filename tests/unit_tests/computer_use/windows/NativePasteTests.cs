// Production transaction, receiver unit and runtime; native APIs are explicit
// test providers. These tests do not claim Windows clipboard/UIA GUI behavior.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Text.Json;

namespace ComputerUse.Windows {
    internal sealed class ClipboardProbe : INativeClipboardApi, INativeClipboardProvider {
        internal static IList<NativeClipboardData> TextFormats(string text) {
            return new[] { new NativeClipboardData(13, Encoding.Unicode.GetBytes(text + "\0")),
                new NativeClipboardData(1, Encoding.ASCII.GetBytes(text + "\0")), new NativeClipboardData(7, Encoding.ASCII.GetBytes(text + "\0")),
                new NativeClipboardData(16, BitConverter.GetBytes((uint)1033)) };
        }
        internal IList<NativeClipboardData> Contents = TextFormats("previous").Concat(new[] { new NativeClipboardData(0xc001, new byte[] { 1, 2, 0, 255 }) }).ToArray();
        internal uint Revision = 1;
        internal bool Locked, FailRead;
        internal int Saves, Replacements, Handoffs, Disposals, Payloads, PayloadDisposals, FailReplaceAt, FailAfterEntries;
        internal Action OnReplace, OnPrepare, OnHandoff;
        static IList<NativeClipboardData> Copy(IList<NativeClipboardData> entries) { return entries.Select(x => new NativeClipboardData(x.Format, (byte[])x.Bytes.Clone())).ToArray(); }
        void Check() { if (!Locked) throw new Exception("Native clipboard operation escaped its lock"); }
        sealed class Lock : IDisposable {
            readonly ClipboardProbe owner;
            readonly uint opened;
            internal Lock(ClipboardProbe owner) { this.owner = owner; opened = owner.Revision; }
            public void Dispose() {
                if (!owner.Locked) throw new Exception("lock disposed twice");
                // Windows/Wine synthesize related text formats at CloseClipboard.
                // A version taken while only Unicode was installed is not final.
                if (opened != owner.Revision && owner.Contents.Any(x => x.Format == 13)) {
                    var missing = TextFormats(owner.Text).Where(x => !owner.Contents.Any(y => y.Format == x.Format)).ToArray();
                    if (missing.Length > 0) { owner.Contents = owner.Contents.Concat(missing).ToArray(); owner.Revision += 2; }
                }
                owner.Locked = false;
            }
        }
        sealed class Payload : INativeClipboardPayload {
            internal readonly IList<NativeClipboardData> Entries;
            readonly ClipboardProbe owner;
            internal Payload(ClipboardProbe owner, IList<NativeClipboardData> entries) { this.owner = owner; Entries = Copy(entries); }
            public void Dispose() { owner.PayloadDisposals++; }
        }
        public IDisposable Open(Request r) { r.Check(); if (Locked) throw new Exception("recursive native clipboard lock"); Locked = true; return new Lock(this); }
        public uint Version { get { Check(); return Revision; } }
        public IList<NativeClipboardData> Read(Request r) { Check(); r.Check(); if (FailRead) throw new Failure("input_unavailable", "fixture non-preservable format"); return Copy(Contents); }
        public INativeClipboardPayload Prepare(IList<NativeClipboardData> entries, Request r) {
            r.Check(); if (OnPrepare != null) OnPrepare(); r.Check(); Payloads++; return new Payload(this, entries);
        }
        public INativeClipboardPayload PrepareText(string text, Request r) {
            return Prepare(NativeClipboardText.Formats(text, 1033, Encoding.ASCII, Encoding.ASCII), r);
        }
        public void Replace(INativeClipboardPayload payload, Action changed, Request r) {
            Check(); r.Check(); Replacements++; Contents = new NativeClipboardData[0]; Revision++; changed();
            if (OnReplace != null) OnReplace(); r.Check();
            if (Replacements == FailReplaceAt) {
                Contents = Copy(((Payload)payload).Entries.Take(FailAfterEntries).ToArray()); Revision++;
                throw new Failure("input_unavailable", "fixture partial clipboard publication");
            }
            Contents = Copy(((Payload)payload).Entries); Revision++;
        }
        public void Handoff() { Handoffs++; if (OnHandoff != null) OnHandoff(); }
        public void Dispose() { if (Locked) throw new Exception("clipboard disposed with lock held"); Disposals++; }
        public NativeClipboardTransaction Save(Request r) { Saves++; return new NativeClipboardTransaction(this, r); }
        internal string Text { get { var item = Contents.FirstOrDefault(x => x.Format == 13); return item == null ? "" : Encoding.Unicode.GetString(item.Bytes).TrimEnd('\0'); } }
        internal void CopyNew(string text) { if (Locked) throw new Exception("outside writer entered clipboard lock"); Contents = TextFormats(text); Revision++; }
        internal string Fingerprint { get { return String.Join(";", Contents.Select(x => x.Format + ":" + Convert.ToBase64String(x.Bytes))); } }
    }
    internal static class NativePasteTests {
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
            internal readonly ClipboardProbe Clipboard = new ClipboardProbe();
            internal readonly TextProbe Keys;
            internal readonly NativeRuntime Runtime;
            internal string Operation;
            internal Dictionary<string, object> Facts, Command;
            internal Case(bool ready = true, bool assist = false) {
                Keys = new TextProbe(Objects) { Foreground = ready, Route = ready, Focused = ready };
                Keys.OnSend = events => {
                    if (events.Any(e => !e.Up && e.Code == 86)) {
                        if (Keys.Full) { Objects.Text = ""; Keys.Caret = 0; Keys.Full = false; }
                        string text = Clipboard.Text; Objects.Text = Objects.Text.Insert(Keys.Caret, text); Keys.Caret += text.Length;
                    }
                    return events.Length;
                };
                Runtime = new NativeRuntime("session", new TextProbeRead(Keys),
                    new NativeExecution(Objects, new NativeKeyboard(Keys, "shared", assist, Clipboard)), new CaptureProbe());
            }
            internal void Start(string text, string node = "node") {
                Operation = (string)Wire.Object(Runtime.Perform("begin", Wire.Obj("mode", "write", "target", Target(node),
                    "intent", Wire.Obj("kind", "paste", "parameters", Wire.Obj("text", text))), Request()))["operation"];
                Facts = Wire.Object(Runtime.Perform("inspect", Wire.Obj("operation", Operation, "target", Target(node),
                    "query", Wire.Obj("kind", "inputContext", "within", new string[0])), Request()));
                var p = Wire.Object(Wire.Array(Facts["primitives"])[0]);
                Command = Wire.Obj("kind", "clipboard_paste", "primitiveId", p["id"], "target", Facts["binding"], "foreground", false);
            }
            internal Dictionary<string, object> Send(int step = 1, Request request = null) {
                return Wire.Object(Runtime.Perform("execute", Wire.Obj("operation", Operation, "step", step, "command", Command), request ?? Request()));
            }
            public void Dispose() { Runtime.Close(); Assert(Objects.Acquired == Objects.Released, "desktop lease leaked"); }
        }
        static void Transactions() {
            var api = new ClipboardProbe(); string original = api.Fingerprint;
            var lease = api.Save(Request()); lease.Install("汉字\t🙂\r\nnext", Request()); lease.Validate(Request());
            Assert(api.Text == "汉字\t🙂\r\nnext" && api.Handoffs == 0, "clipboard text changed or handoff preceded input");
            Assert(lease.Finish(true) == NativeClipboardRestore.Restored && api.Fingerprint == original, "all old representations were not restored");
            Assert(api.Handoffs == 1 && api.PayloadDisposals == api.Payloads && api.Disposals == 1, "clipboard resources leaked");
            lease.Finish(true); Assert(api.Replacements == 2 && api.Handoffs == 1, "finish replayed clipboard writes");
            api = new ClipboardProbe(); lease = api.Save(Request()); api.CopyNew("new copy");
            Refused("input_unavailable", () => lease.Install("ours", Request()));
            lease.Finish(false); Assert(api.Text == "new copy" && api.Replacements == 0, "save/install race replaced newer data");
            api = new ClipboardProbe(); lease = api.Save(Request()); lease.Install("ours", Request()); api.CopyNew("new copy");
            Refused("input_unavailable", () => lease.Validate(Request()));
            Assert(lease.Finish(false) == NativeClipboardRestore.OwnershipLost && api.Text == "new copy", "ownership loss overwrote a copy");
            api = new ClipboardProbe { FailReplaceAt = 1 }; original = api.Fingerprint; lease = api.Save(Request());
            Refused("input_unavailable", () => lease.Install("ours", Request()));
            Assert(lease.Prepared && lease.Finish(false) == NativeClipboardRestore.Restored && api.Fingerprint == original, "partial publication failed to restore before input");
            api = new ClipboardProbe { FailReplaceAt = 1, FailAfterEntries = 1 }; original = api.Fingerprint; lease = api.Save(Request());
            Refused("input_unavailable", () => lease.Install("ours", Request()));
            Assert(lease.Finish(false) == NativeClipboardRestore.Restored && api.Fingerprint == original && api.Replacements == 2,
                "a partial Unicode-only publication closed before rollback and adopted synthesized data");
            api = new ClipboardProbe { FailReplaceAt = 2 }; lease = api.Save(Request()); lease.Install("ours", Request());
            Assert(lease.Finish(true) == NativeClipboardRestore.Failed && lease.Finish(true) == NativeClipboardRestore.Failed, "failed restoration hidden by repeated cleanup");
            api = new ClipboardProbe { FailRead = true };
            Refused("input_unavailable", () => api.Save(Request()));
            Assert(api.Replacements == 0 && api.Disposals == 1, "non-preservable clipboard was changed");
            api = new ClipboardProbe(); original = api.Fingerprint; lease = api.Save(Request());
            using (var cancel = new CancellationTokenSource()) {
                api.OnReplace = () => { if (api.Replacements == 1) cancel.Cancel(); };
                Refused("cancelled", () => lease.Install("ours", new Request(cancel.Token, 1000)));
                Assert(lease.Finish(false) == NativeClipboardRestore.Restored && api.Fingerprint == original, "cancellation after clear prevented restoration");
            }
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
                string original = c.Clipboard.Fingerprint, line;
                while ((line = Console.ReadLine()) != null) {
                    File.AppendAllText(journal, line + "\n");
                    using (var parsed = JsonDocument.Parse(line)) {
                        var request = Wire.Object(Decode(parsed.RootElement)); if (!request.ContainsKey("method")) continue;
                        var reply = Wire.Obj("version", 5, "session", "session", "id", request["id"]);
                        try { reply["result"] = (string)request["method"] == "hello"
                            ? Wire.Obj("platform", "windows", "desktopMode", "shared", "maxTimeoutMs", 120000, "capabilities", c.Runtime.Capabilities)
                            : c.Runtime.Perform((string)request["method"], Wire.Object(request["params"]), Request((int)(long)request["timeoutMs"])); }
                        catch (Failure error) { reply["error"] = Wire.Obj("code", error.Code, "effect", "none", "target", "fixture", "detail", error.Message, "next", error.Next); }
                        File.WriteAllText(journal + ".effects.json", JsonSerializer.Serialize(new { text = c.Objects.Text, sent = c.Keys.Batches.Count,
                            clipboardSaved = c.Clipboard.Saves, restored = c.Clipboard.Fingerprint == original,
                            activated = c.Keys.Activated, focused = c.Keys.FocusCalls, acquired = c.Objects.Acquired, released = c.Objects.Released }));
                        Console.WriteLine(JsonSerializer.Serialize(reply));
                    }
                }
            }
            return 0;
        }
        static int Main(string[] args) {
            if (args.Length == 3 && args[0] == "--serve") return Serve(args[1], args[2] == "assist");
            Transactions(); object sampleFacts = null, sampleReceipt = null;
            using (var c = new Case()) {
                string original = c.Clipboard.Fingerprint; c.Start("汉字\t🙂\r\nnext"); sampleFacts = c.Facts;
                sampleReceipt = c.Send();
                Assert(c.Objects.Text == "initial汉字\t🙂\r\nnext" && c.Clipboard.Fingerprint == original, "paste did not preserve multiline text or clipboard");
                Assert(c.Keys.Batches.Count == 1 && c.Keys.Batches[0].Select(e => e.Code).SequenceEqual(new ushort[] { 17, 86, 86, 17 }), "paste used typing, a different chord or repeated input");
                Assert((string)Wire.Object(sampleReceipt)["confirmation"] == "unavailable", "dispatch became business confirmation");
                Assert(ReferenceEquals(c.Send(), sampleReceipt) && c.Clipboard.Saves == 1, "duplicate step repasted");
                Refused("execution_unknown", () => c.Send(2));
            }
            using (var c = new Case(false, true)) {
                c.Start("x"); c.Command["kind"] = "prepare_receiver"; c.Command["foreground"] = true;
                Assert((string)c.Send()["confirmation"] == "confirmed" && c.Clipboard.Saves == 0, "preparation changed clipboard");
                c.Command["kind"] = "clipboard_paste"; c.Command["foreground"] = false;
                Assert((string)c.Send(2)["dispatch"] == "dispatched" && c.Keys.Activated == 1, "paste reactivated or did not send");
            }
            using (var c = new Case(false)) {
                c.Start("x"); Assert((string)c.Send()["dispatch"] == "notDispatched" && c.Clipboard.Saves == 0 && c.Keys.Activated == 0, "background dispatch touched clipboard or foreground");
            }
            using (var c = new Case(false)) {
                c.Keys.Full = true; c.Start(""); var receipt = c.Send();
                Assert((string)receipt["confirmation"] == "confirmed" && c.Clipboard.Saves == 0 && c.Keys.Batches.Count == 0 && c.Objects.Text == "initial", "empty paste changed text or selected another receiver");
            }
            foreach (string node in new[] { "node", null }) using (var c = new Case()) {
                string original = c.Clipboard.Fingerprint; c.Start("x", node); c.Keys.OnIdle = r => c.Keys.Recipient++;
                var receipt = c.Send();
                Assert((string)receipt["dispatch"] == "notDispatched" && c.Keys.Batches.Count == 0 && c.Clipboard.Fingerprint == original && c.Clipboard.Handoffs == 0, "recipient drift pasted or skipped restoration");
            }
            using (var c = new Case()) {
                c.Start("x"); c.Keys.OnIdle = r => c.Clipboard.CopyNew("user copy"); var receipt = c.Send();
                Assert((string)receipt["dispatch"] == "notDispatched" && c.Keys.Batches.Count == 0 && c.Clipboard.Text == "user copy", "clipboard drift dispatched another payload");
            }
            using (var c = new Case()) {
                c.Start("x"); c.Keys.OnRead = r => { if (c.Clipboard.Replacements == 1) c.Clipboard.CopyNew("provider copy"); };
                var receipt = c.Send();
                Assert((string)receipt["dispatch"] == "notDispatched" && c.Keys.Batches.Count == 0 && c.Clipboard.Text == "provider copy", "receiver callback changed the payload after its last check");
            }
            using (var c = new Case()) {
                c.Start("x"); c.Keys.AfterSend = () => c.Clipboard.CopyNew("user copy"); var receipt = c.Send();
                Assert((string)receipt["dispatch"] == "dispatched" && c.Clipboard.Text == "user copy" && (string)receipt["cleanup"] == "complete", "later copy was overwritten");
            }
            using (var c = new Case()) using (var cancel = new CancellationTokenSource()) {
                string original = c.Clipboard.Fingerprint; c.Start("x"); c.Keys.AfterSend = () => cancel.Cancel();
                var receipt = c.Send(request: new Request(cancel.Token, 1000));
                Assert((string)receipt["dispatch"] == "dispatched" && (string)receipt["errorCode"] == "cancelled" && c.Clipboard.Handoffs == 1 && c.Clipboard.Fingerprint == original, "post-send cancellation skipped handoff/restoration or hid dispatch");
            }
            using (var c = new Case()) {
                c.Start("x"); c.Keys.OnSend = events => c.Keys.Batches.Count == 1 ? 1 : events.Length;
                var receipt = c.Send();
                Assert((string)receipt["dispatch"] == "unknown" && c.Keys.Batches.Count == 2 && c.Keys.Batches.Last().All(e => e.Up), "partial chord lacked key cleanup");
                Assert(c.Clipboard.Handoffs == 1 && (string)receipt["cleanup"] == "complete", "uncertain chord skipped clipboard cleanup");
                c.Send(); Assert(c.Clipboard.Saves == 1 && c.Keys.Batches.Count == 2, "uncertain paste replayed");
            }
            using (var c = new Case()) {
                c.Clipboard.FailReplaceAt = 2; c.Start("x"); var receipt = c.Send();
                Assert((string)receipt["cleanup"] == "failed" && receipt["retryReason"] == null && (string)receipt["errorCode"] == "execution_unknown", "restore failure did not quarantine input");
                Refused("execution_unknown", () => c.Start("again"));
            }
            foreach (string invalid in new[] { "\0", "\ud800", new string('x', 1024 * 1024 + 1) }) using (var c = new Case()) {
                Refused("invalid_request", () => c.Start(invalid));
                Assert(c.Clipboard.Saves == 0 && c.Objects.Acquired == 0, "invalid paste prepared a resource");
            }
            Console.WriteLine(JsonSerializer.Serialize(new { assertions = assertions, facts = sampleFacts, receipt = sampleReceipt,
                scope = "Production clipboard transaction and paste recipe with fake native boundaries; no Windows GUI execution" }));
            return 0;
        }
    }
}
