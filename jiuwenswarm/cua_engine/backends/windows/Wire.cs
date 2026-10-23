using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Web.Script.Serialization;

namespace ComputerUse.Windows {
    internal sealed class Failure : Exception {
        internal readonly string Code, Next;
        internal Failure(string code, string detail, string next = "read the target again before continuing") : base(detail) { Code = code; Next = next; }
    }
    internal static class Wire {
        internal static readonly long Epoch = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
        internal static readonly Stopwatch Clock = Stopwatch.StartNew();
        internal static double Now { get { return Epoch + Clock.Elapsed.TotalMilliseconds; } }
        internal static Dictionary<string, object> Obj(params object[] pairs) {
            var d = new Dictionary<string, object>();
            for (int i = 0; i < pairs.Length; i += 2) d.Add((string)pairs[i], pairs[i + 1]);
            return d;
        }
        internal static JavaScriptSerializer Serializer() { return new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024, RecursionLimit = 256 }; }
        internal static string Json(object value) { return Serializer().Serialize(value); }
        internal static Dictionary<string, object> Object(object value) {
            var d = value as Dictionary<string, object>;
            if (d == null) throw new Failure("invalid_request", "expected an object");
            return d;
        }
        internal static void Keys(Dictionary<string, object> d, string required, string optional = "") {
            var r = required.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries);
            var allowed = new HashSet<string>(r.Concat(optional.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries)));
            if (r.Any(k => !d.ContainsKey(k)) || d.Keys.Any(k => !allowed.Contains(k))) throw new Failure("invalid_request", "invalid object fields");
        }
        internal static string Str(Dictionary<string, object> d, string key, bool empty = false) {
            object value; string s;
            if (!d.TryGetValue(key, out value) || (s = value as string) == null || (!empty && String.IsNullOrWhiteSpace(s))) throw new Failure("invalid_request", "invalid " + key);
            return s;
        }
        internal static double Num(Dictionary<string, object> d, string key, double min, double max, bool integer = false) {
            object value;
            if (!d.TryGetValue(key, out value) || !(value is int || value is long || value is decimal || value is double)) throw new Failure("invalid_request", "invalid " + key);
            double n = Convert.ToDouble(value);
            if (Double.IsNaN(n) || Double.IsInfinity(n) || n < min || n > max || (integer && n != Math.Floor(n))) throw new Failure("invalid_request", "invalid " + key);
            return n;
        }
        internal static bool Bool(Dictionary<string, object> d, string key) {
            object value; if (!d.TryGetValue(key, out value) || !(value is bool)) throw new Failure("invalid_request", "invalid " + key); return (bool)value;
        }
        internal static object[] Array(object value) {
            var list = value as IEnumerable; if (list == null || value is string || value is IDictionary) throw new Failure("invalid_request", "expected array");
            return list.Cast<object>().ToArray();
        }
        internal static object Canonical(object value) {
            var d = value as Dictionary<string, object>;
            if (d != null) return new SortedDictionary<string, object>(d.ToDictionary(p => p.Key, p => Canonical(p.Value)));
            if (value is object[]) return ((object[])value).Select(Canonical).ToArray(); return value;
        }
        internal static Dictionary<string, object> Stamp(double start) { return Obj("id", Guid.NewGuid().ToString(), "startedAt", start, "completedAt", Now); }
    }
    internal sealed class Request {
        internal readonly CancellationToken Token;
        internal double Deadline;
        internal bool Effect, Dispatched;
        internal string Target = "request";
        internal Request(CancellationToken token, int timeout) { Token = token; Deadline = Wire.Now + timeout; }
        internal void Check() {
            if (Token.IsCancellationRequested) throw new Failure("cancelled", "request cancelled; no input is replayed");
            if (Wire.Now >= Deadline) throw new Failure("deadline_exceeded", "request deadline exceeded; no input is replayed");
        }
        internal void Wait(int ms) { Check(); Token.WaitHandle.WaitOne(ms); Check(); }
        internal void Sending() { Check(); Effect = true; Dispatched = true; }
    }
}
