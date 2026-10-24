// Portable host for the production Driver and NativeRuntime. UIA and the
// Framework JSON implementation are not available here: real UIA/Win32 sources
// are compiled separately against net48 and require Windows GUI acceptance.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading;

namespace System.Windows.Automation {
    public sealed class ElementNotAvailableException : Exception { }
}
namespace System.Web.Script.Serialization {
    public sealed class JavaScriptSerializer {
        public int MaxJsonLength { get; set; }
        public int RecursionLimit { get; set; }
        public string Serialize(object value) { return JsonSerializer.Serialize(value); }
        public object DeserializeObject(string value) {
            using (var doc = JsonDocument.Parse(value)) return Decode(doc.RootElement);
        }
        static object Decode(JsonElement value) {
            switch (value.ValueKind) {
                case JsonValueKind.Object: return value.EnumerateObject().ToDictionary(p => p.Name, p => Decode(p.Value));
                case JsonValueKind.Array: return value.EnumerateArray().Select(Decode).ToArray();
                case JsonValueKind.String: return value.GetString();
                case JsonValueKind.Number: long integer; return value.TryGetInt64(out integer) ? (object)integer : value.GetDouble();
                case JsonValueKind.True: return true;
                case JsonValueKind.False: return false;
                default: return null;
            }
        }
    }
}
namespace ComputerUse.Windows {
    internal sealed class DriverProbeBackend : INativeReadBackend {
        readonly string session, mode;
        readonly Action<string> record;
        readonly int ownerThread = Thread.CurrentThread.ManagedThreadId;
        int reads;
        internal DriverProbeBackend(string session, string mode, Action<string> record) {
            this.session = session; this.mode = mode; this.record = record;
        }
        void CheckThread() { if (Thread.CurrentThread.ManagedThreadId != ownerThread) throw new Exception("UIA provider left its construction thread"); }
        public void Validate(Dictionary<string, object> target, Request r) { CheckThread(); r.Check(); }
        public void Release() { CheckThread(); record("released"); }
        public object Inspect(Dictionary<string, object> target, Dictionary<string, object> query, Request r) {
            CheckThread();
            reads++; record("read:" + reads);
            if (mode == "slow") { record("waiting"); while (true) r.Wait(10); }
            if ((string)query["kind"] != "applications") throw new Failure("snapshot_expired", "fixture has no captured text");
            return Wire.Obj("kind", "applications", "session", session, "platform", "windows", "observed", Wire.Stamp(Wire.Now),
                "stopReasons", new string[0], "applications", new[] {
                    Wire.Obj("id", "application", "displayName", "Protocol fixture", "isRunning", true, "path", null, "identifier", null) });
        }
    }
    internal static class NativeDriverProtocolTests {
        static int Main(string[] args) {
            Console.OutputEncoding = new UTF8Encoding(false);
            string mode = args[0], journal = args[1]; int creations = 0;
            Action<string> record = message => File.AppendAllText(journal, message + "\n");
            new Driver((session, desktopMode) => {
                creations++; record("factory:" + creations);
                if (mode == "fail-once" && creations == 1) throw new Failure("input_unavailable", "fixture initialization failed");
                return new NativeRuntime(session, new DriverProbeBackend(session, mode, record));
            }).Run();
            record("closed"); return 0;
        }
    }
}
