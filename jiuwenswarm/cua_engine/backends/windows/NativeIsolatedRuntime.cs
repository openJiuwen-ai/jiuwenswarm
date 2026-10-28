using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;

namespace ComputerUse.Windows {
    // Public request journaling lives in Driver. A replaceable worker owns all
    // UIA objects. Only verified Win32 window identities survive a read timeout;
    // element, text, image and operation tokens are never silently reconstructed.
    internal sealed class NativeIsolatedRuntime : INativeRuntime {
        readonly string session, mode;
        NativeChildProcess child;
        readonly Dictionary<string, Dictionary<string, object>> windows = new Dictionary<string, Dictionary<string, object>>();
        readonly Dictionary<string, long> generations = new Dictionary<string, long>();
        readonly HashSet<string> completedReads = new HashSet<string>();
        readonly Queue<string> completedOrder = new Queue<string>();
        string operation;
        bool writing, retired, recycleRequested;
        double readDeadline;
        long sequence;
        string[] capabilities;
        public string[] Capabilities { get { return capabilities.Concat(new[] { "nativeReadIsolation", "nativeWindowReadiness" }).ToArray(); } }
        internal NativeIsolatedRuntime(string session, string mode) {
            this.session = session; this.mode = mode;
            Start(new Request(CancellationToken.None, 8000));
        }
        bool Valid(Dictionary<string, object> item, long generation) {
            try {
                new Window { Handle = new IntPtr(Convert.ToInt64(item["handle"])), Pid = Convert.ToUInt32(item["pid"]),
                    Started = Int64.Parse((string)item["started"]), Generation = generation }.Validate();
                return true;
            } catch { return false; }
        }
        void Start(Request request) {
            if (retired) throw new Failure("execution_unknown", "native worker cleanup could not be confirmed; reconnect before further operations");
            if (child != null) return;
            foreach (string id in windows.Keys.ToArray()) if (!Valid(windows[id], generations[id])) { windows.Remove(id); generations.Remove(id); }
            child = new NativeChildProcess("--native-worker"); sequence = 0;
            try {
                child.Send(Wire.Obj("session", session, "windows", windows.Values.ToArray()));
                var ready = Receive(request, Wire.Now + 5000);
                if (!ready.ContainsKey("ready") || !Equals(ready["ready"], true)) throw new Exception("native worker bootstrap failed");
                child.Send(Wire.Obj("version",5,"id",0,"method","hello","params",Wire.Obj("desktopMode",mode)));
                var hello = Receive(request, Wire.Now + 5000);
                capabilities = Wire.Array(Wire.Object(hello["result"])["capabilities"]).Cast<string>().ToArray();
            } catch { RetireWorker(); throw; }
        }
        Dictionary<string, object> Receive(Request request, double limit) {
            while (true) {
                request.Check();
                if (Wire.Now >= limit) throw new Failure("observation_unavailable", "native provider did not return within the isolated read budget");
                string line = child.Poll(20);
                if (line != null) return Wire.Object(Wire.Serializer().DeserializeObject(line));
            }
        }
        void Checkpoint(object raw, Dictionary<IntPtr,long> before) {
            foreach (object value in Wire.Array(raw)) {
                var item = Wire.Object(value); string id = (string)item["surface"];
                var handle = new IntPtr(Convert.ToInt64(item["handle"]));
                long generation;
                if (!generations.TryGetValue(id, out generation) && !before.TryGetValue(handle, out generation)) generation = 0;
                // A destroy during this request is ambiguous even if the HWND
                // has since been reused. Never import it into a fresh worker.
                if (!Valid(item, generation)) { windows.Remove(id); generations.Remove(id); continue; }
                windows[id] = item; generations[id] = generation;
            }
        }
        void RetireWorker() {
            if (child == null) return;
            if (!child.Stop()) { retired = true; throw new Failure("execution_unknown", "isolated native worker termination was not confirmed"); }
            child.Dispose(); child = null;
        }
        void EndInterruptedRead() {
            // Never acknowledge cleanup before the old process is confirmed
            // gone. Failure leaves this session retired, not safely resumable.
            RetireWorker();
            if (operation != null) {
                completedReads.Add(operation); completedOrder.Enqueue(operation); operation=null;
                while (completedOrder.Count>64) completedReads.Remove(completedOrder.Dequeue());
            }
            recycleRequested=false;
        }
        public object Perform(string method, Dictionary<string, object> parameters, Request request) {
            if (method == "finish" && completedReads.Contains(Wire.Str(parameters,"operation"))) return Wire.Obj("cleanup","complete");
            if (recycleRequested) {
                if (method=="finish" && Wire.Str(parameters,"operation")==operation) {
                    EndInterruptedRead(); return Wire.Obj("cleanup","complete");
                }
                // A directory stage may request applications then surfaces in
                // one read lease. Drain only their Win32 metadata; the Python
                // caller finishes this lease before reading trees or pixels.
                var query=method=="inspect" ? Wire.Object(parameters["query"]) : null;
                if (query==null || !new[]{"applications","surfaces"}.Contains(Wire.Str(query,"kind")))
                    throw new Failure("stale_target","UIA enrichment failed; finish this read lease before collecting more observations");
            }
            Start(request);
            bool writeRequest = writing || method == "begin" && Equals(parameters["mode"], "write");
            long id = ++sequence;
            var before = Native.GenerationSnapshot();
            child.Send(Wire.Obj("version",5,"session",session,"id",id,"method",method,"params",parameters,
                "timeoutMs",Math.Max(1,Math.Min(120000,(int)(request.Deadline-Wire.Now)))));
            Dictionary<string,object> response;
            try {
                // Input retains its own deadline/cleanup contract. This limit
                // applies only to observations and never replays a command.
                response = Receive(request, writeRequest ? request.Deadline : Math.Min(request.Deadline,Wire.Now+5000));
            } catch (Exception error) {
                if (writeRequest) {
                    request.Effect = method == "execute";
                    try { child.Send(Wire.Obj("version",5,"session",session,"cancel",id)); } catch { }
                    // Let native units release paired keys before terminating.
                    if (child.Stop(750)) { child.Dispose(); child=null; } else retired=true;
                    retired=true;
                    throw new Failure("execution_unknown","native write worker stopped responding; no command was replayed; reconnect and observe");
                }
                EndInterruptedRead();
                var failure = error as Failure;
                if (failure != null && (failure.Code=="cancelled" || failure.Code=="deadline_exceeded")) throw;
                throw new Failure("observation_unavailable","read worker was stopped after a blocked or failed provider call; the selected window is retained, old element/text/image references require a fresh read. " + error.Message);
            }
            if (Convert.ToInt64(response["id"]) != id || !Equals(response["session"],session)) { RetireWorker(); throw new Failure("observation_unavailable","native worker response identity mismatch"); }
            Checkpoint(response["windowCheckpoint"], before);
            if (response.ContainsKey("recycleReadWorker") && Equals(response["recycleReadWorker"],true)) {
                if (writeRequest) { retired=true; RetireWorker(); throw new Failure("execution_unknown","native write worker unexpectedly requested read recovery"); }
                recycleRequested=true;
            }
            object raw;
            if (response.TryGetValue("error",out raw)) {
                var error = Wire.Object(raw); request.Effect = !Equals(error["effect"],"none");
                throw new Failure((string)error["code"],(string)error["detail"],(string)error["next"]);
            }
            var result = response["result"];
            if (method=="begin") { operation=(string)Wire.Object(result)["operation"]; writing=Equals(parameters["mode"],"write"); readDeadline=request.Deadline; }
            if (method=="finish") { operation=null; writing=false; }
            return result;
        }
        public void Expire() { if (recycleRequested && Wire.Now>=readDeadline) EndInterruptedRead(); }
        public void Close() { if(child!=null) { child.Stop(750); child.Dispose(); child=null; } }
    }
}
