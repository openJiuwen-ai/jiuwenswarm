using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;

namespace ComputerUse.Windows {
    internal sealed class Driver {
        readonly string session, mode;
        readonly object gate = new object(), output = new object();
        readonly BlockingCollection<Action> work = new BlockingCollection<Action>();
        readonly Dictionary<long, string> requests = new Dictionary<long, string>(), replies = new Dictionary<long, string>();
        readonly Queue<long> order = new Queue<long>();
        readonly Func<string, string, INativeRuntime> createRuntime;
        readonly Func<object> checkpoint;
        readonly Func<bool> recycle;
        object latestCheckpoint;
        INativeRuntime native; const int version = 5;
        long high, active; volatile bool hello, stopping, negotiating; int bytes;
        CancellationTokenSource cancellation;
        internal Driver(Func<string, string, INativeRuntime> createRuntime, string session = null, Func<object> checkpoint = null, Func<bool> recycle = null) {
            if (createRuntime == null) throw new ArgumentNullException("createRuntime");
            this.createRuntime = createRuntime;
            this.session = session ?? Guid.NewGuid().ToString(); this.checkpoint = checkpoint;
            this.recycle = recycle;
            mode = Environment.GetEnvironmentVariable("COMPUTER_USE_DESKTOP") ?? "shared";
        }
        void Write(string line) { lock (output) { Console.Out.WriteLine(line); Console.Out.Flush(); } }
        void Reply(long id, object payload, bool remember = false) {
            var response = Wire.Object(payload); response["version"] = version; response["session"] = session; response["id"] = id; string line = Wire.Json(response);
            if (checkpoint != null) {
                response["windowCheckpoint"] = latestCheckpoint ?? new object[0];
                response["recycleReadWorker"] = recycle != null && recycle(); line = Wire.Json(response);
            }
            lock (gate) {
                if (remember) {
                    replies[id] = line; order.Enqueue(id); bytes += Encoding.UTF8.GetByteCount(line);
                    while (order.Count > 64 || bytes > 8 * 1024 * 1024) { long old = order.Dequeue(); bytes -= Encoding.UTF8.GetByteCount(replies[old]); replies.Remove(old); requests.Remove(old); }
                }
                if (!stopping) Write(line);
            }
        }
        object Error(string code, string detail, string effect = "none", Request r = null) {
            var d = Wire.Obj("code", code, "target", r == null ? "request" : r.Target, "effect", effect, "detail", detail, "next", "observe current state before continuing; never automatically replay uncertain input");
            return Wire.Obj("error", d);
        }
        void Stop() { lock (gate) { if (stopping) return; stopping = true; if (cancellation != null) cancellation.Cancel(); work.CompleteAdding(); } }
        void Receive(string line) {
            long? id = null;
            try {
                if (line.StartsWith("\uFEFF", StringComparison.Ordinal)) throw new Exception("UTF-8 BOM is not allowed");
                var p = Wire.Object(Wire.Serializer().DeserializeObject(line));
                if (p.ContainsKey("cancel")) {
                    Wire.Keys(p, "version,session,cancel"); if (Wire.Num(p, "version", version, version, true) != version || !hello || Wire.Str(p, "session") != session) throw new Exception("invalid cancellation session");
                    long cancelled = (long)Wire.Num(p, "cancel", 1, 9007199254740991, true); lock (gate) { if (active == cancelled && cancellation != null) cancellation.Cancel(); } return;
                }
                id = (long)Wire.Num(p, "id", 0, 9007199254740991, true);
                int requestedVersion = (int)Wire.Num(p, "version", 0, Int32.MaxValue, true);
                if (requestedVersion != version) { Reply(id.Value, Error("version_mismatch", "this Windows driver requires protocol 5")); return; }
                if (id == 0) {
                    Wire.Keys(p, "version,id,method,params"); if (hello || negotiating) throw new Failure("invalid_request", "this session is already initializing or initialized"); if (Wire.Str(p, "method") != "hello") throw new Failure("invalid_request", "id zero is reserved for hello");
                    var options = Wire.Object(p["params"]); Wire.Keys(options, "desktopMode");
                    if (!(mode == "shared" || mode == "exclusive") || Wire.Str(options, "desktopMode") != mode) { Reply(0, Error("mode_mismatch", "host desktop mode differs from requested mode")); return; }
                    // All UIA construction and use belongs to the same
                    // windowless MTA worker, including future COM providers.
                    negotiating = true;
                    work.Add(() => {
                        try {
                            native = createRuntime(session, mode); hello = true; negotiating = false;
                            Reply(0, Wire.Obj("result", Wire.Obj("platform", "windows", "desktopMode", mode, "maxTimeoutMs", 120000,
                                "capabilities", native.Capabilities)));
                        } catch (Exception error) { negotiating = false; Reply(0, Error("execution_failed", "native initialization failed: " + error.Message)); }
                    }); return;
                }
                Wire.Keys(p, "version,session,id,method,params,timeoutMs");
                if (!hello || Wire.Str(p, "session") != session) { Reply(id.Value, Error("session_mismatch", "request does not belong to this live session")); return; }
                string method = Wire.Str(p, "method"); var parameters = Wire.Object(p["params"]); int timeout = (int)Wire.Num(p, "timeoutMs", 1, 120000, true);
                string canonical = Wire.Json(Wire.Canonical(p)); long current = id.Value;
                lock (gate) {
                    string earlier;
                    if (requests.TryGetValue(current, out earlier)) {
                        if (earlier != canonical) Reply(current, Error("request_conflict", "request id used with different content"));
                        else if (replies.ContainsKey(current)) Write(replies[current]);
                        else Reply(current, Error("in_progress", "request already executing; no duplicate dispatched", "possible"));
                        return;
                    }
                    if (current <= high) { Reply(current, Error("request_expired", "request result no longer retained; no duplicate dispatched", "possible")); return; }
                    high = current; requests[current] = canonical;
                    if (active != 0) { Reply(current, Error("busy", "another request is executing"), true); return; }
                    active = current; cancellation = new CancellationTokenSource();
                    var request = new Request(cancellation.Token, timeout);
                    work.Add(() => Execute(current, method, parameters, request));
                }
            } catch (Exception error) { if (id.HasValue) Reply(id.Value, Error("invalid_request", error.Message)); else { Console.Error.WriteLine("protocol: " + error.Message); Stop(); } }
        }
        void Execute(long id, string method, Dictionary<string, object> p, Request r) {
            object response;
            try {
                r.Check();
                if (!new[] { "begin", "inspect", "capture", "execute", "finish" }.Contains(method))
                    throw new Failure("invalid_request", "method '" + method + "' is not supported by protocol v5");
                var result = native.Perform(method, p, r);
                // Units journal cancellation after possible dispatch in their
                // receipt. Do not replace that receipt with a generic error.
                response = Wire.Obj("result", result);
            } catch (Exception error) {
                var failure = error as Failure; var code = failure == null ? (error is System.Windows.Automation.ElementNotAvailableException ? "stale_target" : "execution_failed") : failure.Code;
                response = Error(code, error.Message, r.Dispatched ? "partial" : r.Effect ? "possible" : "none", r);
                if (failure != null) Wire.Object(Wire.Object(response)["error"])["next"] = failure.Next;
            }
            // Publish only after input cleanup and lease release; the client may
            // immediately send its next command when it receives this response.
            if (checkpoint != null) latestCheckpoint = checkpoint();
            lock (gate) { active = 0; if (cancellation != null) cancellation.Dispose(); cancellation = null; Reply(id, response, true); }
        }
        internal void Run() {
            Console.CancelKeyPress += (s, e) => { e.Cancel = true; Stop(); };
            var reader = new Thread(() => {
                try {
                    using (var input = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false, true), false, 8192)) {
                        var buffer = new char[8192]; var pending = new StringBuilder(); int count;
                        while (!stopping && (count = input.Read(buffer, 0, buffer.Length)) > 0) {
                            for (int i = 0; i < count; i++) {
                                if (buffer[i] == '\n') { var line = pending.ToString().TrimEnd('\r'); pending.Clear(); Receive(line); }
                                else { pending.Append(buffer[i]); if (pending.Length > 64 * 1024 * 1024) throw new Exception("request frame too large"); }
                            }
                        }
                        if (pending.Length != 0) Console.Error.WriteLine("protocol: incomplete final frame");
                    }
                } catch (Exception e) { Console.Error.WriteLine("stdin: " + e.Message); } finally { Stop(); }
            }); reader.IsBackground = true; reader.Start();
            try { while (!work.IsCompleted) { Action action; if (work.TryTake(out action, 25)) action(); if (native != null) native.Expire(); } }
            finally { if (native != null) native.Close(); }
        }
    }
}
