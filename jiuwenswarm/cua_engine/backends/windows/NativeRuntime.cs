using System;
using System.Collections.Generic;
using System.Linq;

namespace ComputerUse.Windows {
    internal interface INativeRuntime {
        string[] Capabilities { get; }
        object Perform(string method, Dictionary<string, object> parameters, Request request);
        void Expire();
        void Close();
    }
    // A native observation backend, not the v3 Backend strategy facade.
    internal interface INativeReadBackend {
        void Validate(Dictionary<string, object> target, Request request);
        object Inspect(Dictionary<string, object> target, Dictionary<string, object> query, Request request);
        void Release();
    }
    internal interface INativeCaptureBackend {
        object Capture(Dictionary<string, object> target, string kind, int maxDimension, Request request);
    }

    internal sealed class NativeRuntime : INativeRuntime {
        static readonly string[] ReadCapabilities = { "cancel", "requestDedup", "nativeOperations", "nativeDirectory", "nativeRelations", "nativeNodePages", "nativeNodeDepthBudget", "nativeRelationPages", "nativeCapturedText", "nativeTableFacts", "nativeAdvertisedFields", "nativeFollowRelations", "nativeUiaReadV2" };
        static readonly string[] FollowRelations = { "RawViewChildren", "LabeledBy", "Selection.Selection", "Table.RowHeaders", "Table.ColumnHeaders", "TableItem.RowHeaderItems", "TableItem.ColumnHeaderItems", "Text.Children", "ItemContainer.Items", "Grid.Cells" };
        public string[] Capabilities { get { return ReadCapabilities.Concat(writer == null ? new string[0] : writer.Capabilities)
            .Concat(capture == null ? new string[0] : new[] { "nativeCapture", "nativeCaptureKinds" }).ToArray(); } }
        readonly string session;
        readonly INativeReadBackend reader;
        readonly NativeExecution writer;
        readonly INativeCaptureBackend capture;
        readonly Dictionary<string, string> finished = new Dictionary<string, string>();
        readonly Queue<string> finishedOrder = new Queue<string>();
        string operation;
        double deadline;
        Dictionary<string, object> original;
        NativeWriteOperation write;
        IDisposable lease;
        bool retired;
        sealed class Step { internal Dictionary<string, object> Command; internal object Result; }
        readonly Dictionary<long, Step> steps = new Dictionary<long, Step>();
        readonly Queue<long> stepOrder = new Queue<long>();
        long highStep;
        internal NativeRuntime(string session, INativeReadBackend reader, NativeExecution writer = null, INativeCaptureBackend capture = null) {
            this.session = session; this.reader = reader; this.writer = writer; this.capture = capture;
        }
        internal static readonly string[] BindingKeys = { "session", "instance", "rootWindow", "surface", "node", "capture" };
        internal Dictionary<string, object> Binding(object raw) {
            var value = Wire.Object(raw); Wire.Keys(value, String.Join(",", BindingKeys));
            foreach (var key in BindingKeys) if (value[key] != null) Wire.Str(value, key);
            if (!Equals(value["session"], session)) throw new Failure("stale_target", "target belongs to another native session");
            bool window = value["surface"] != null;
            if ((value["instance"] != null) != window || (value["rootWindow"] != null) != window || !window && value["node"] != null)
                throw new Failure("invalid_request", "native binding has an incomplete window scope");
            if (value["capture"] != null && value["node"] != null) throw new Failure("invalid_request", "image input cannot select an accessibility node");
            return new Dictionary<string, object>(value);
        }
        static string[] Names(Dictionary<string, object> query, string key, int maximum) {
            var raw = Wire.Array(query[key]);
            if (raw.Length < 1 || raw.Length > maximum || raw.Any(x => !(x is string) || String.IsNullOrWhiteSpace((string)x)) || raw.Distinct().Count() != raw.Length)
                throw new Failure("invalid_request", "invalid native property list");
            return raw.Cast<string>().ToArray();
        }
        internal static void Query(Dictionary<string, object> query) {
            string kind = Wire.Str(query, "kind");
            switch (kind) {
                case "applications": Wire.Keys(query, "kind"); break;
                case "surfaces": Wire.Keys(query, "kind,application", "includeAccessibility"); Wire.Str(query, "application");
                    if (query.ContainsKey("includeAccessibility")) Wire.Bool(query, "includeAccessibility"); break;
                case "nodes":
                    Wire.Keys(query, "kind,fields,maxNodes,maxDepth", "childrenPage,relationPage,advertisedFields,followRelations");
                    var fields = Names(query, "fields", 128);
                    if (query.ContainsKey("advertisedFields") && Names(query, "advertisedFields", 128).Except(fields).Any())
                        throw new Failure("invalid_request", "advertised properties must belong to the requested fields");
                    if (query.ContainsKey("followRelations") && Names(query, "followRelations", 16).Except(FollowRelations).Any())
                        throw new Failure("invalid_request", "unsupported native traversal relation");
                    double nodes = Wire.Num(query, "maxNodes", 1, 2000, true), depth = Wire.Num(query, "maxDepth", 0, 2000, true);
                    if (query.ContainsKey("childrenPage") && query.ContainsKey("relationPage"))
                        throw new Failure("invalid_request", "select one root relation page");
                    string pageKey = query.ContainsKey("relationPage") ? "relationPage" : "childrenPage";
                    if (query.ContainsKey(pageKey)) {
                        var page = Wire.Object(query[pageKey]); Wire.Keys(page, pageKey == "relationPage" ? "property,cursor,limit" : "cursor,limit");
                        if (pageKey == "relationPage") Wire.Str(page, "property");
                        if (page["cursor"] != null) Wire.Str(page, "cursor");
                        if (Wire.Num(page, "limit", 1, 1999, true) >= nodes || depth < 1)
                            throw new Failure("invalid_request", "relation page requires depth and space for its root and targets");
                    }
                    break;
                case "relation":
                    Wire.Keys(query, "kind,property,cursor,limit"); Wire.Str(query, "property");
                    if (query["cursor"] != null) Wire.Str(query, "cursor");
                    Wire.Num(query, "limit", 1, 2000, true); break;
                case "inputContext":
                    Wire.Keys(query, "kind,within"); var within = Wire.Array(query["within"]);
                    if (within.Length > 16 || within.Any(x => !(x is string) || String.IsNullOrWhiteSpace((string)x)) || within.Distinct().Count() != within.Length)
                        throw new Failure("invalid_request", "invalid ancestor constraint list");
                    break;
                case "capturedText":
                    Wire.Keys(query, "kind,textRef,offset,length"); Wire.Str(query, "textRef");
                    Wire.Num(query, "offset", 0, 9007199254740991, true); Wire.Num(query, "length", 2, 16384, true); break;
                case "launch": case "roots":
                    throw new Failure("mode_unsupported", "this native Windows slice provides window discovery and raw UIA reading only");
                default: throw new Failure("invalid_request", "unknown native observation query");
            }
        }
        void FinishCurrent() {
            if (operation == null) return;
            string closed = operation, cleanup = write == null ? "complete" : write.Cleanup;
            if (cleanup != "complete") retired = true;
            operation = null; original = null; write = null; steps.Clear(); stepOrder.Clear();
            try { reader.Release(); } catch { cleanup = "failed"; retired = true; }
            if (lease != null) { try { lease.Dispose(); lease = null; } catch { cleanup = "failed"; retired = true; } }
            finished[closed] = cleanup; finishedOrder.Enqueue(closed);
            while (finishedOrder.Count > 8) finished.Remove(finishedOrder.Dequeue());
        }
        public void Close() { FinishCurrent(); }
        public void Expire() { if (operation != null && Wire.Now >= deadline) FinishCurrent(); }
        public object Perform(string method, Dictionary<string, object> p, Request r) {
            if (method == "finish") {
                Wire.Keys(p, "operation"); string id = Wire.Str(p, "operation");
                if (id == operation) FinishCurrent();
                if (!finished.ContainsKey(id)) throw new Failure("stale_target", "native operation is unknown or expired");
                return Wire.Obj("cleanup", finished[id]);
            }
            if (method == "begin") {
                Wire.Keys(p, "mode,target,intent"); string mode = Wire.Str(p, "mode");
                var intent = Wire.Object(p["intent"]); Wire.Keys(intent, "kind,parameters");
                string kind = Wire.Str(intent, "kind"); var parameters = Wire.Object(intent["parameters"]);
                var target = Binding(p["target"]);
                bool writable = mode == "write";
                if (writable) {
                    if (writer == null) throw new Failure("mode_unsupported", "this native driver has no input execution provider");
                    writer.Validate(intent, target);
                } else if (mode != "read" || kind != "read" || parameters.Count != 0) throw new Failure("invalid_request", "read operation requires an empty read intent");
                if (!writable && target["capture"] != null) throw new Failure("invalid_request", "a capture binding is an input scope, not a new read scope");
                Expire();
                if (retired) throw new Failure("execution_unknown", "native cleanup failed; this runtime is retired");
                if (operation != null) throw new Failure("busy", "another native operation is active");
                r.Check();
                try { if (writable) lease = writer.Acquire(r); reader.Validate(target, r); r.Check(); }
                catch {
                    try { reader.Release(); } catch { retired = true; }
                    finally { if (lease != null) { try { lease.Dispose(); lease = null; } catch { retired = true; } } }
                    throw;
                }
                operation = Guid.NewGuid().ToString("N"); original = target; deadline = r.Deadline; highStep = 0;
                write = writable ? new NativeWriteOperation(target, intent) : null;
                return Wire.Obj("operation", operation, "target", new Dictionary<string, object>(original), "remainingMs", Math.Max(0, (int)Math.Floor(deadline - Wire.Now)));
            }
            if (method == "execute") return Execute(p, r);
            if (method == "capture") {
                if (capture == null) throw new Failure("mode_unsupported", "this native driver has no capture provider");
                Wire.Keys(p, "operation,target,kind,maxDimension"); string id = Wire.Str(p, "operation");
                var target = Binding(p["target"]); string kind = Wire.Str(p, "kind"); int dimension = (int)Wire.Num(p, "maxDimension", 1, 4096, true);
                if (operation == null || id != operation) throw new Failure("stale_target", "native operation is unknown or expired");
                if (write != null || !NativeWriteOperation.Same(target, original) || target["node"] != null || target["capture"] != null ||
                    !new[] { "window", "desktop" }.Contains(kind) || (target["surface"] != null) != (kind == "window"))
                    throw new Failure("invalid_request", "capture requires the original window or desktop read scope");
                r.Deadline = Math.Min(r.Deadline, deadline);
                try { r.Check(); var result = capture.Capture(target, kind, dimension, r); r.Check(); return result; }
                catch (Failure error) { if (error.Code == "cancelled" || error.Code == "deadline_exceeded") FinishCurrent(); throw; }
                finally { Expire(); }
            }
            if (method != "inspect") throw new Failure("invalid_request", "v5 never dispatches legacy read or input commands");
            Wire.Keys(p, "operation,target,query"); string token = Wire.Str(p, "operation");
            var requested = Binding(p["target"]); var query = Wire.Object(p["query"]); Query(query);
            if (operation == null || operation != token) throw new Failure("stale_target", "native operation is unknown or expired");
            if (BindingKeys.Any(key => key != "node" && !Equals(original[key], requested[key])))
                throw new Failure("stale_target", "native observation cannot change its original scope");
            bool directory = new[] { "applications", "surfaces", "capturedText" }.Contains(Wire.Str(query, "kind"));
            if (requested["capture"] != null && (string)query["kind"] != "inputContext")
                throw new Failure("invalid_request", "captured image input has no accessibility node queries");
            bool desktopInput = write != null && requested["surface"] == null && requested["capture"] != null && (string)query["kind"] == "inputContext";
            if (!desktopInput && ((requested["surface"] == null) != directory || !directory && requested["node"] == null && (string)query["kind"] != "inputContext"))
                throw new Failure("invalid_request", "native query requires its corresponding directory or node scope");
            r.Deadline = Math.Min(r.Deadline, deadline);
            try {
                r.Check(); reader.Validate(requested, r); object result;
                if ((string)query["kind"] == "inputContext") {
                    if (write == null || writer == null) throw new Failure("invalid_request", "input contracts require a write operation");
                    result = writer.Inspect(write, requested, Wire.Array(query["within"]).Cast<string>().ToArray(), r);
                } else result = reader.Inspect(requested, query, r);
                r.Check(); return result;
            }
            catch (Failure error) { if (error.Code == "cancelled" || error.Code == "deadline_exceeded") FinishCurrent(); throw; }
            finally { Expire(); }
        }
        object Execute(Dictionary<string, object> p, Request r) {
            if (writer == null) throw new Failure("mode_unsupported", "this native driver has no input execution provider");
            Wire.Keys(p, "operation,step,command"); string id = Wire.Str(p, "operation"); long step = (long)Wire.Num(p, "step", 1, 9007199254740991, true);
            var command = Wire.Object(p["command"]); Wire.Keys(command, "kind,primitiveId,target,foreground");
            Wire.Str(command, "kind"); Wire.Str(command, "primitiveId"); Wire.Bool(command, "foreground"); var target = Binding(command["target"]);
            if (operation == null || id != operation || write == null) throw new Failure("stale_target", "native write operation is unknown or expired");
            Step prior;
            if (steps.TryGetValue(step, out prior)) {
                bool same = command.Keys.All(k => k == "target" ? NativeWriteOperation.Same(target, Wire.Object(prior.Command[k])) : Equals(command[k], prior.Command[k]));
                if (!same) throw new Failure("request_conflict", "step id reused with different input");
                return prior.Result;
            }
            if (step <= highStep) throw new Failure("request_conflict", "step result expired; input will not be repeated");
            if (write.Dispatched) { r.Effect = true; throw new Failure("execution_unknown", "this operation already dispatched its input; it cannot send it again"); }
            r.Deadline = Math.Min(r.Deadline, deadline); highStep = step;
            try {
                r.Check(); object result = writer.Execute(write, command, r);
                var copy = new Dictionary<string, object>(command); copy["target"] = new Dictionary<string, object>(target);
                steps[step] = new Step { Command = copy, Result = result }; stepOrder.Enqueue(step);
                while (stepOrder.Count > 32) steps.Remove(stepOrder.Dequeue());
                var code = Wire.Object(result)["errorCode"] as string;
                if (code == "cancelled" || code == "deadline_exceeded" || write.Cleanup != "complete") FinishCurrent();
                return result;
            } catch (Failure error) { if (error.Code == "cancelled" || error.Code == "deadline_exceeded") FinishCurrent(); throw; }
            finally { Expire(); }
        }
    }
}
