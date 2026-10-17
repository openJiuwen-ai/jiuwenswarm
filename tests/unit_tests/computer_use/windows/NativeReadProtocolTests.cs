// Runs the production v5 lifecycle/validation code on .NET without UIA.
// The provider is intentionally fake: these tests prove protocol behaviour,
// never Windows provider availability, native field extraction or GUI effects.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Text.Json;

// Wire's Framework serializer is compile-checked separately with real net48
// references. This host does not call it or pretend to test Framework JSON.
namespace System.Web.Script.Serialization {
    public sealed class JavaScriptSerializer {
        public int MaxJsonLength { get; set; }
        public int RecursionLimit { get; set; }
        public string Serialize(object value) { throw new NotSupportedException("Framework JSON is not exercised by this portable probe"); }
        public object DeserializeObject(string value) { throw new NotSupportedException("Framework JSON is not exercised by this portable probe"); }
    }
}
namespace ComputerUse.Windows {
    internal sealed class ReadProbeBackend : INativeReadBackend {
        internal int Validations, Reads, Releases;
        internal Action<Request> BeforeRead;
        internal bool RejectValidation, RejectWindows, CaptureText;
        internal readonly NativeCapturedText Texts = new NativeCapturedText();
        int textGeneration;
        public void Validate(Dictionary<string, object> target, Request r) { r.Check(); Validations++; if (RejectValidation || RejectWindows && target["surface"] != null) throw new Failure("stale_target", "fixture target retired"); }
        public void Release() { Releases++; }
        public object Inspect(Dictionary<string, object> target, Dictionary<string, object> query, Request r) {
            Reads++; if (BeforeRead != null) BeforeRead(r);
            string kind = (string)query["kind"];
            if (kind == "capturedText") return Wire.Obj("kind", kind, "session", "session", "textRef", query["textRef"],
                "page", Texts.Page((string)query["textRef"], (long)Wire.Num(query, "offset", 0, 9007199254740991, true),
                    (int)Wire.Num(query, "length", 2, 16384, true)));
            if (kind == "applications") return Wire.Obj("kind", kind, "session", "session", "platform", "windows", "observed", Wire.Obj("id", "stamp", "startedAt", 10, "completedAt", 11), "stopReasons", new string[0],
                "applications", new[] { Wire.Obj("id", "application", "displayName", "Protocol fixture", "isRunning", true, "path", null, "identifier", null) });
            if (kind == "surfaces") return Wire.Obj("kind", kind, "session", "session", "platform", "windows", "application", "application",
                "observed", Wire.Obj("id", "stamp", "startedAt", 10, "completedAt", 11), "stopReasons", new string[0],
                "provider", Wire.Obj("status", "fixture", "attribute", null, "writeStatus", null), "surfaces", new[] {
                    Wire.Obj("window", "window", "instance", "instance", "surface", "surface", "node", "node", "title", "Protocol fixture",
                        "kind", "window", "parent", null, "parentState", "none", "modal", false, "nativeWindow", true, "accessibility", true) });
            if (kind == "nodes") {
                var fields = Wire.Array(query["fields"]); var values = Wire.Obj(); var statuses = Wire.Obj();
                var textBudget = Texts.BeginBatch(); textGeneration++;
                foreach (string field in fields) {
                    if (field == "ControlType") values[field] = "ControlType.Window";
                    else if (field == "Name") values[field] = "Protocol fixture";
                    else if (field == "Text.DocumentRange" && CaptureText)
                        Texts.Observe(field, new string('\u03b1', 16383) + "\ud83d\ude42e\u0301\r\n" + new string('b', 8192) + ":v" + textGeneration,
                            true, textBudget, values, statuses);
                    else statuses[field] = Wire.Obj("state", "unsupported");
                }
                string node = (string)target["node"]; var baseRelations = new[] { "RawViewChildren", "RawViewParent", "LabeledBy" };
                var followed = query.ContainsKey("followRelations") ? Wire.Array(query["followRelations"]).Cast<string>() : new string[0];
                var relations = baseRelations.Concat(new[] { "GridItem.ContainingGrid", "SelectionItem.SelectionContainer" }).Concat(followed).Distinct().ToArray();
                return Wire.Obj("kind", kind, "session", "session", "surface", "surface", "platform", "windows",
                    "observed", Wire.Obj("id", "stamp", "startedAt", 10, "completedAt", 11), "roots", new[] { node },
                    "nodes", new[] { Wire.Obj("node", node, "requestedFields", fields, "values", values, "fieldStatus", statuses,
                        "actions", Wire.Obj("state", "complete", "items", new object[0], "total", 0)) },
                    "requestedRelations", relations, "relations", relations.Select(p => baseRelations.Contains(p)
                        ? Wire.Obj("source", node, "property", p, "targets", new string[0], "certainty", "confirmed", "state", "complete", "total", 0)
                        : Wire.Obj("source", node, "property", p, "targets", new string[0], "certainty", "unknown", "state", "unsupported")).ToArray(), "unread", new string[0], "stopReasons", new string[0]);
            }
            return Wire.Obj("kind", kind);
        }
    }
    internal static class NativeReadProtocolTests {
        static int assertions;
        static void Assert(bool condition, string reason) { assertions++; if (!condition) throw new Exception(reason); }
        static void Refused(string code, Action action) {
            try { action(); } catch (Failure error) { Assert(error.Code == code, "Expected " + code + ", got " + error.Code); return; }
            throw new Exception("Expected refusal " + code);
        }
        static Request Request(int timeout = 10000) { return new Request(CancellationToken.None, timeout); }
        static Dictionary<string, object> Target(bool window = false, string node = null) {
            return Wire.Obj("session", "session", "instance", window ? "instance" : null, "rootWindow", window ? "window" : null, "surface", window ? "surface" : null, "node", node, "capture", null);
        }
        static Dictionary<string, object> Begin(Dictionary<string, object> target) { return Wire.Obj("mode", "read", "target", target, "intent", Wire.Obj("kind", "read", "parameters", Wire.Obj())); }
        static Dictionary<string, object> Inspect(string operation, Dictionary<string, object> target, Dictionary<string, object> query) { return Wire.Obj("operation", operation, "target", target, "query", query); }
        static object Finish(NativeRuntime runtime, string operation) { return runtime.Perform("finish", Wire.Obj("operation", operation), Request()); }
        static string Open(NativeRuntime runtime, Dictionary<string, object> target, Request r = null) { return (string)Wire.Object(runtime.Perform("begin", Begin(target), r ?? Request()))["operation"]; }
        // Portable transport host, not the Win32 Driver/Framework serializer.
        // It lets the real Python API send requests to the production runtime.
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
            var runtime = new NativeRuntime("session", new ReadProbeBackend { CaptureText = true }); string line;
            try { while ((line = Console.ReadLine()) != null) {
                File.AppendAllText(journal, line + "\n");
                using (var parsed = JsonDocument.Parse(line)) {
                    var request = Wire.Object(Decode(parsed.RootElement)); if (!request.ContainsKey("method")) continue;
                    var reply = Wire.Obj("version", 5, "session", "session", "id", request["id"]);
                    try {
                        reply["result"] = (string)request["method"] == "hello"
                            ? Wire.Obj("platform", "windows", "desktopMode", "shared", "maxTimeoutMs", 120000, "capabilities", runtime.Capabilities)
                            : runtime.Perform((string)request["method"], Wire.Object(request["params"]), Request((int)(long)request["timeoutMs"]));
                    } catch (Failure error) { reply["error"] = Wire.Obj("code", error.Code, "effect", "none", "target", "fixture", "detail", error.Message, "next", error.Next); }
                    Console.WriteLine(JsonSerializer.Serialize(reply)); Console.Out.Flush();
                }
            } } finally { runtime.Close(); }
            return 0;
        }
        static void CapturedText() {
            var texts = new NativeCapturedText(32768, 2);
            var values = Wire.Obj(); var statuses = Wire.Obj(); var budget = texts.BeginBatch();
            string original = new string('a', 16383) + "\ud83d\ude42e\u0301\r\nEND";
            texts.Observe("Text.DocumentRange", original, true, budget, values, statuses);
            var status = Wire.Object(statuses["Text.DocumentRange"]); string token = (string)status["textRef"];
            Assert(((string)values["Text.DocumentRange"]).Length == 16383, "Preview split a surrogate");
            Assert((int)status["totalUnits"] == original.Length, "Capture length changed");
            string reconstructed = ""; long offset = 0;
            do {
                var page = Wire.Object(texts.Page(token, offset, 4096)); reconstructed += (string)page["text"];
                if (page["next"] == null) break;
                offset = (int)page["next"];
            } while (true);
            Assert(reconstructed == original, "Immutable pages changed Unicode or line breaks");
            Assert((string)Wire.Object(texts.Page(token, original.Length, 2))["text"] == "", "EOF is not empty");
            Refused("invalid_request", () => texts.Page(token, 16384, 2));
            Refused("invalid_request", () => texts.Page(token, original.Length + 1, 2));
            Assert(budget.RequestLimit == 16385, "Remaining capture budget allowed an unbounded request");
            texts.Observe("tooLargeForBatch", original, true, budget, values, statuses);
            Assert(!Wire.Object(statuses["tooLargeForBatch"]).ContainsKey("textRef"), "Batch evicted its own earlier capture");
            Assert((string)Wire.Object(texts.Page(token, 0, 2))["text"] == "aa", "Batch exhausted an earlier capture");
            texts.Observe("providerPrefix", new string('x', 16384) + "\ud83d", false, budget, values, statuses);
            var prefix = Wire.Object(statuses["providerPrefix"]);
            Assert(!prefix.ContainsKey("totalUnits") && !prefix.ContainsKey("textRef"), "Incomplete provider text was reported as complete");
            texts.Observe("empty", "", true, budget, values, statuses);
            Assert((string)values["empty"] == "" && !statuses.ContainsKey("empty"), "Empty complete text became unavailable");
            texts.Observe("newSnapshot", original + "new", true, texts.BeginBatch(), values, statuses);
            Refused("snapshot_expired", () => texts.Page(token, 0, 2));
            Refused("snapshot_expired", () => texts.Page("invented", 0, 2));

            var provider = new ReadProbeBackend { CaptureText = true };
            var runtime = new NativeRuntime("session", provider);
            Assert(runtime.Capabilities.Contains("nativeCapturedText"), "Text continuation capability missing");
            string operation = Open(runtime, Target(true, "node"));
            var facts = Wire.Object(runtime.Perform("inspect", Inspect(operation, Target(true, "node"),
                Wire.Obj("kind", "nodes", "fields", new[] { "Text.DocumentRange" }, "maxNodes", 10, "maxDepth", 1)), Request()));
            string captured = (string)Wire.Object(Wire.Object(Wire.Object(Wire.Array(facts["nodes"])[0])["fieldStatus"])["Text.DocumentRange"])["textRef"];
            var query = Wire.Obj("kind", "capturedText", "textRef", captured, "offset", 0, "length", 2);
            Refused("invalid_request", () => runtime.Perform("inspect", Inspect(operation, Target(true, "node"), query), Request()));
            Finish(runtime, operation);
            provider.RejectWindows = true;
            Refused("stale_target", () => Open(runtime, Target(true, "node")));
            operation = Open(runtime, Target());
            var response = Wire.Object(runtime.Perform("inspect", Inspect(operation, Target(), query), Request()));
            Assert((string)Wire.Object(response["page"])["text"] == "\u03b1\u03b1", "Capture depended on the surviving window");
            foreach (object invalid in new object[] { -1, 1.5, 9007199254740992d }) {
                query["offset"] = invalid;
                Refused("invalid_request", () => runtime.Perform("inspect", Inspect(operation, Target(), query), Request()));
            }
            query["offset"] = 0; query["extra"] = true;
            Refused("invalid_request", () => runtime.Perform("inspect", Inspect(operation, Target(), query), Request()));
            Finish(runtime, operation); runtime.Close();
        }
        static void TableQueryTests() {
            var provider = new ReadProbeBackend(); var runtime = new NativeRuntime("session", provider);
            Assert(new[] { "nativeTableFacts", "nativeAdvertisedFields", "nativeFollowRelations" }.All(runtime.Capabilities.Contains), "Table read negotiation missing");
            string operation = Open(runtime, Target(true, "node"));
            var query = Wire.Obj("kind", "nodes", "fields", new[] { "ControlType", "Grid.RowCount", "GridItem.Row" },
                "maxNodes", 10, "maxDepth", 1, "advertisedFields", new[] { "Grid.RowCount", "GridItem.Row" },
                "followRelations", new[] { "Selection.Selection", "TableItem.ColumnHeaderItems" });
            var response = Wire.Object(runtime.Perform("inspect", Inspect(operation, Target(true, "node"), query), Request()));
            Assert(Wire.Array(response["requestedRelations"]).Contains("Selection.Selection"), "Followed membership did not reach the provider");
            var fields = Wire.Object(Wire.Array(response["nodes"])[0]);
            Assert(!Wire.Object(fields["values"]).ContainsKey("Grid.RowCount"), "Unsupported count became zero");
            Assert((string)Wire.Object(Wire.Object(fields["fieldStatus"])["Grid.RowCount"])["state"] == "unsupported", "Unsupported count lost its status");
            int reads = provider.Reads;
            foreach (var invalid in new[] { new string[0], new[] { "Name" }, new[] { "GridItem.Row", "GridItem.Row" } }) {
                query["advertisedFields"] = invalid;
                Refused("invalid_request", () => runtime.Perform("inspect", Inspect(operation, Target(true, "node"), query), Request()));
            }
            query["advertisedFields"] = new[] { "GridItem.Row" };
            foreach (var invalid in new[] { new string[0], new[] { "Selection.Selection", "Selection.Selection" },
                    new[] { "GridItem.ContainingGrid" }, new[] { "SelectionItem.SelectionContainer" }, new[] { "unimplemented" } }) {
                query["followRelations"] = invalid;
                Refused("invalid_request", () => runtime.Perform("inspect", Inspect(operation, Target(true, "node"), query), Request()));
            }
            Assert(provider.Reads == reads, "Invalid observation profiles touched the provider");
            Finish(runtime, operation);
        }
        static int Main(string[] args) {
            if (args.Length == 2 && args[0] == "--serve") return Serve(args[1]);
            Assert(Equals(NativeReadValues.Number(0), 0.0), "Real zero must stay numeric");
            foreach (double value in new[] { Double.NaN, Double.PositiveInfinity, Double.NegativeInfinity }) {
                var encoded = Wire.Object(NativeReadValues.Number(value));
                Assert((string)encoded["nativeType"] == "UIA.Double", "Non-finite native number lost its type");
                Assert((string)encoded["value"] == (Double.IsNaN(value) ? "NaN" : value > 0 ? "+Infinity" : "-Infinity"), "Non-finite native number changed");
                Assert(JsonDocument.Parse(JsonSerializer.Serialize(encoded)).RootElement.ValueKind == JsonValueKind.Object, "Encoded UIA number is not valid JSON");
            }
            CapturedText();
            TableQueryTests();
            var provider = new ReadProbeBackend(); var runtime = new NativeRuntime("session", provider);
            Assert(!runtime.Capabilities.Contains("nativeBoundContracts") && !runtime.Capabilities.Contains("nativeCapture"), "No unimplemented input or capture capability");
            Assert(runtime.Capabilities.Contains("nativeNodePages"), "Public Element.read needs the implemented child-page capability");
            Assert(runtime.Capabilities.Contains("nativeRelationPages"), "Content continuations need their own capability");
            var bad = Begin(Target()); bad["mode"] = "write"; bad["intent"] = Wire.Obj("kind", "click", "parameters", Wire.Obj());
            Refused("mode_unsupported", () => runtime.Perform("begin", bad, Request())); Assert(provider.Validations == 0, "Write rejection touched provider");
            foreach (string method in new[] { "readApp", "clickElement", "listApps", "paste" }) Refused("invalid_request", () => runtime.Perform(method, Wire.Obj(), Request()));
            Refused("mode_unsupported", () => runtime.Perform("execute", Wire.Obj(), Request()));
            Refused("mode_unsupported", () => runtime.Perform("capture", Wire.Obj(), Request()));
            var foreign = Target(); foreign["session"] = "other"; Refused("stale_target", () => Open(runtime, foreign));
            var incomplete = Target(true); incomplete["instance"] = null; Refused("invalid_request", () => Open(runtime, incomplete));
            string first = Open(runtime, Target()); Refused("busy", () => Open(runtime, Target()));
            var directory = runtime.Perform("inspect", Inspect(first, Target(), Wire.Obj("kind", "applications")), Request());
            Assert(provider.Reads == 1, "One inspect reads once");
            int reads = provider.Reads;
            Refused("invalid_request", () => runtime.Perform("inspect", Inspect(first, Target(), Wire.Obj("kind", "applications", "extra", 1)), Request()));
            Refused("stale_target", () => runtime.Perform("inspect", Inspect(first, Target(true, "node"), Wire.Obj("kind", "nodes", "fields", new[] { "Name" }, "maxNodes", 10, "maxDepth", 1)), Request()));
            Assert(provider.Reads == reads, "Malformed/scope-changing inspect touched provider");
            var finished = Finish(runtime, first); int releaseCount = provider.Releases; Finish(runtime, first); Assert(provider.Releases == releaseCount, "Finish must be idempotent");
            Refused("stale_target", () => runtime.Perform("inspect", Inspect(first, Target(), Wire.Obj("kind", "applications")), Request()));
            Refused("stale_target", () => Finish(runtime, "invented"));
            string scoped = Open(runtime, Target(true, "node"));
            runtime.Perform("inspect", Inspect(scoped, Target(true, "other"), Wire.Obj("kind", "nodes", "fields", new[] { "Name" }, "maxNodes", 10, "maxDepth", 1)), Request());
            Assert(provider.Reads == reads + 1, "Read may inspect another native node inside its fixed surface");
            var otherWindow = Target(true, "other"); otherWindow["surface"] = "another surface";
            Refused("stale_target", () => runtime.Perform("inspect", Inspect(scoped, otherWindow, Wire.Obj("kind", "nodes", "fields", new[] { "Name" }, "maxNodes", 10, "maxDepth", 1)), Request()));
            var duplicated = Wire.Obj("kind", "nodes", "fields", new[] { "Name", "Name" }, "maxNodes", 10, "maxDepth", 1);
            Refused("invalid_request", () => runtime.Perform("inspect", Inspect(scoped, Target(true, "node"), duplicated), Request()));
            var page = Wire.Obj("kind", "nodes", "fields", new[] { "Name" }, "maxNodes", 10, "maxDepth", 1,
                "childrenPage", Wire.Obj("cursor", null, "limit", 9));
            runtime.Perform("inspect", Inspect(scoped, Target(true, "node"), page), Request());
            Assert(provider.Reads == reads + 2, "Element subtree first-page query must reach the provider");
            Wire.Object(page["childrenPage"])["limit"] = 10;
            Refused("invalid_request", () => runtime.Perform("inspect", Inspect(scoped, Target(true, "node"), page), Request()));
            Wire.Object(page["childrenPage"])["limit"] = 9; page["maxDepth"] = 0;
            Refused("invalid_request", () => runtime.Perform("inspect", Inspect(scoped, Target(true, "node"), page), Request()));
            var related = Wire.Obj("kind", "nodes", "fields", new[] { "Name" }, "maxNodes", 10, "maxDepth", 1,
                "relationPage", Wire.Obj("property", "LabeledBy", "cursor", "observed-page", "limit", 9));
            runtime.Perform("inspect", Inspect(scoped, Target(true, "node"), related), Request());
            Assert(provider.Reads == reads + 3, "Selected relationship page must reach the provider");
            related["childrenPage"] = Wire.Obj("cursor", null, "limit", 9);
            Refused("invalid_request", () => runtime.Perform("inspect", Inspect(scoped, Target(true, "node"), related), Request()));
            related.Remove("childrenPage"); related["maxDepth"] = 0;
            Refused("invalid_request", () => runtime.Perform("inspect", Inspect(scoped, Target(true, "node"), related), Request()));
            Finish(runtime, scoped);
            var deadline = Request(); deadline.Deadline = Wire.Now + 1000; string limited = Open(runtime, Target(), deadline);
            var next = Request(); provider.BeforeRead = r => Assert(r.Deadline <= deadline.Deadline, "RPC renewed operation deadline");
            runtime.Perform("inspect", Inspect(limited, Target(), Wire.Obj("kind", "applications")), next); provider.BeforeRead = null; Finish(runtime, limited);
            var expired = Request(); string expiry = Open(runtime, Target(), expired); expired.Deadline = Wire.Now - 1;
            Refused("deadline_exceeded", () => runtime.Perform("inspect", Inspect(expiry, Target(), Wire.Obj("kind", "applications")), expired));
            Finish(runtime, expiry);
            string cancelled = Open(runtime, Target()); var cancellation = new CancellationTokenSource();
            provider.BeforeRead = r => cancellation.Cancel();
            Refused("cancelled", () => runtime.Perform("inspect", Inspect(cancelled, Target(), Wire.Obj("kind", "applications")), new Request(cancellation.Token, 1000)));
            provider.BeforeRead = null; Finish(runtime, cancelled);
            provider.RejectValidation = true; releaseCount = provider.Releases;
            Refused("stale_target", () => Open(runtime, Target())); Assert(provider.Releases == releaseCount + 1, "Failed begin leaked pins"); provider.RejectValidation = false;
            string closed = Open(runtime, Target()); runtime.Close(); runtime.Close(); Finish(runtime, closed);
            string oldest = null;
            for (int n = 0; n < 9; n++) { string id = Open(runtime, Target()); if (oldest == null) oldest = id; Finish(runtime, id); }
            Refused("stale_target", () => Finish(runtime, oldest));
            Console.WriteLine(JsonSerializer.Serialize(new { assertions = assertions, directory = directory, finish = finished,
                scope = "Production C# lifecycle with a fake read provider; no UIA, Framework serializer or Windows GUI execution" }));
            return 0;
        }
    }
}
