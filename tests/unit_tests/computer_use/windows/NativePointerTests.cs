// Exercises production contracts, geometry, event plans and cleanup with fake
// OS boundaries. It does not validate HWND routing or real SendInput delivery.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using System.Text.Json;

namespace ComputerUse.Windows {
    internal sealed class PointerProbe : INativePointerProvider {
        internal readonly NativePointerCaptures Captures = new NativePointerCaptures();
        internal bool Ready = true, Foreground, Alive = true, Held, GeometryChanged, LayoutChanged, Modal, Grab = true;
        internal int Activated, Reads;
        internal Action<Request> OnRead, OnIdle;
        internal Func<NativePointerEvent[], int> OnSend;
        internal readonly List<NativePointerEvent[]> Batches = new List<NativePointerEvent[]>();
        internal readonly List<NativePointerPoint> Points = new List<NativePointerPoint>();
        public NativePointerCapture Capture(Dictionary<string, object> target, Request r) { r.Check(); return Captures.Get(target); }
        public NativePointerState Read(Dictionary<string, object> target, NativePointerPoint p, string kind, bool dragging, Request r) {
            Reads++; Points.Add(p); if (OnRead != null) OnRead(r); r.Check(); Capture(target, r);
            if (!Alive) throw new Failure("stale_target", "fixture window retired");
            if (Modal) throw new Failure("blocked_by_modal", "fixture modal");
            if (LayoutChanged || GeometryChanged && !(dragging && Grab)) throw new Failure("snapshot_expired", "fixture geometry changed");
            return new NativePointerState { Native = this, Ready = Ready, Foreground = Foreground };
        }
        public void RequireIdle(Request r) { if (OnIdle != null) OnIdle(r); r.Check(); if (Held) throw new Failure("input_unavailable", "fixture user input held"); }
        public void Activate(NativePointerState state, Request r) { Activated++; r.Check(); Foreground = Ready = true; }
        public int Send(NativePointerEvent[] events) { Batches.Add(events); return OnSend == null ? events.Length : OnSend(events); }
    }
    internal sealed class PointerCaptureProbe : INativeCaptureBackend {
        readonly NativePointerCaptures captures;
        readonly CaptureProbe images = new CaptureProbe();
        internal PointerCaptureProbe(NativePointerCaptures captures) { this.captures = captures; }
        public object Capture(Dictionary<string, object> target, string kind, int maxDimension, Request r) {
            var result = Wire.Object(images.Capture(target, kind, maxDimension, r));
            if (kind == "window") {
                var image = Wire.Object(result["image"]); image["id"] = Guid.NewGuid().ToString("N");
                image["width"] = image["height"] = 2;
                image["png"] = "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAADklEQVR4nGP4DwYMEAoAU7oL9ZisIGcAAAAASUVORK5CYII=";
                var canvas = new NativeCaptureGeometry(-500, 0, 1000, 800, 1);
                captures.Add((string)image["id"], target, new NativeCaptureGeometry(-200, 50, 2, 2, 2), canvas, "layout", new[] { canvas });
            }
            return result;
        }
    }
    internal static class NativePointerTests {
        static int assertions;
        static void Assert(bool ok, string detail) { assertions++; if (!ok) throw new Exception(detail); }
        static void Refused(string code, Action action) {
            try { action(); } catch (Failure error) { Assert(error.Code == code, "Expected " + code + ", got " + error.Code); return; }
            throw new Exception("Expected " + code);
        }
        static Request Request(int ms = 5000) { return new Request(CancellationToken.None, ms); }
        static Dictionary<string, object> Target(bool capture = true) {
            return Wire.Obj("session", "session", "instance", "instance", "rootWindow", "window", "surface", "surface", "node", null, "capture", capture ? "capture" : null);
        }
        static NativeRuntime Runtime(PointerProbe pointer, ObjectProbe objects, bool assist = false, KeyboardProbe keyboard = null) {
            var canvas = new NativeCaptureGeometry(-1000, -100, 2000, 1200, 1);
            pointer.Captures.Add("capture", Target(false), new NativeCaptureGeometry(-200, 50, 400, 300, 400), canvas, "layout", new[] { canvas });
            return new NativeRuntime("session", new ObjectProbeRead(objects), new NativeExecution(objects,
                keyboard == null ? null : new NativeKeyboard(keyboard, "shared", assist), new NativePointer(pointer, "shared", assist)), new PointerCaptureProbe(pointer.Captures));
        }
        static string Begin(NativeRuntime runtime, string kind = "click", Dictionary<string, object> parameters = null, Dictionary<string, object> target = null) {
            return (string)Wire.Object(runtime.Perform("begin", Wire.Obj("mode", "write", "target", target ?? Target(),
                "intent", Wire.Obj("kind", kind, "parameters", parameters ?? Wire.Obj("x", 10, "y", 20))), Request()))["operation"];
        }
        static Dictionary<string, object> Inspect(NativeRuntime runtime, string operation, Dictionary<string, object> target = null) {
            return Wire.Object(runtime.Perform("inspect", Wire.Obj("operation", operation, "target", target ?? Target(),
                "query", Wire.Obj("kind", "inputContext", "within", new string[0])), Request()));
        }
        static Dictionary<string, object> Command(Dictionary<string, object> facts, bool prepare = false, bool foreground = false) {
            var p = Wire.Object(Wire.Array(facts["primitives"])[0]);
            return Wire.Obj("kind", prepare ? "prepare_receiver" : "pointer_sequence", "primitiveId", p["id"], "target", facts["binding"], "foreground", foreground);
        }
        static Dictionary<string, object> Execute(NativeRuntime runtime, string operation, object command, int step = 1, Request r = null) {
            return Wire.Object(runtime.Perform("execute", Wire.Obj("operation", operation, "step", step, "command", command), r ?? Request()));
        }
        static void Finish(NativeRuntime runtime, string operation) { runtime.Perform("finish", Wire.Obj("operation", operation), Request()); }
        static void Geometry() {
            var desktop = new NativeCaptureGeometry(-3840, -2160, 7680, 4320, 1);
            var image = new NativeCaptureGeometry(-2000, -1000, 3840, 2160, 1024);
            var store = new NativePointerCaptures(2);
            store.Add("capture", Target(false), image, desktop, "layout", new[] { desktop });
            var record = store.Get(Target()); var p = record.Point(100, 100);
            Assert(p.X == -1625 && p.Y == -625, "scaled image lost its negative physical origin");
            foreach (int x in new[] { -3840, -2001, 0, 3839 }) foreach (int y in new[] { -2160, -1, 0, 2159 }) {
                var normalized = record.Normalize(new NativePointerPoint(x, y));
                Assert((int)Math.Floor(normalized.X * 7680.0 / 65536) - 3840 == x &&
                    (int)Math.Floor(normalized.Y * 4320.0 / 65536) - 2160 == y, "absolute normalization changed the physical pixel");
            }
            Refused("invalid_request", () => record.Point(-1, 0));
            Refused("invalid_request", () => record.Point(image.Width, 0));
            Refused("invalid_request", () => record.Point(Double.NaN, 0));
            var wrong = Target(); wrong["surface"] = "other"; Refused("stale_target", () => store.Get(wrong));
            wrong = Target(); wrong["instance"] = "other"; Refused("stale_target", () => store.Get(wrong));
            store.Add("second", Target(false), image, desktop, "layout", new[] { desktop });
            store.Add("third", Target(false), image, desktop, "layout", new[] { desktop });
            Refused("snapshot_expired", () => store.Get(Target()));
            var cropped = new NativeCaptureGeometry(108, 100, 784, 592, 392);
            var frame = new NativeCaptureGeometry(100, 100, 800, 600, 392);
            store.Add("cropped", Target(false), cropped, desktop, "layout", new[] { desktop }, frame);
            var croppedTarget=Target();croppedTarget["capture"]="cropped";
            var croppedRecord=store.Get(croppedTarget);
            Assert(croppedRecord.Point(0,0).X==108 && croppedRecord.Point(10,10).X==128,
                "WGC image coordinates must use its cropped physical origin and scale");
            Assert(croppedRecord.WindowBounds.X==100 && croppedRecord.WindowBounds.NativeWidth==800,
                "Window move checks must preserve the full Win32 bounds, not the cropped image");
            var gap = new NativePointerCapture(Target(), new NativeCaptureGeometry(0, 0, 300, 100, 300),
                new NativeCaptureGeometry(0, 0, 300, 100, 1), "gap", new[] {
                    new NativeCaptureGeometry(0, 0, 100, 100, 1), new NativeCaptureGeometry(200, 0, 100, 100, 1) });
            Refused("invalid_request", () => gap.Point(150, 50));
        }
        static object Decode(JsonElement v) {
            switch (v.ValueKind) {
                case JsonValueKind.Object: return v.EnumerateObject().ToDictionary(p => p.Name, p => Decode(p.Value));
                case JsonValueKind.Array: return v.EnumerateArray().Select(Decode).ToArray();
                case JsonValueKind.String: return v.GetString();
                case JsonValueKind.Number: return v.GetDouble();
                case JsonValueKind.True: return true;
                case JsonValueKind.False: return false;
                default: return null;
            }
        }
        static int Serve(string journal, string setting) {
            var pointer = new PointerProbe { Ready = setting == "ready" }; var objects = new ObjectProbe();
            var runtime = Runtime(pointer, objects, assist: setting == "assist");
            try { string line; while ((line = Console.ReadLine()) != null) {
                File.AppendAllText(journal, line + "\n");
                using (var parsed = JsonDocument.Parse(line)) {
                    var request = Wire.Object(Decode(parsed.RootElement)); if (!request.ContainsKey("method")) continue;
                    var reply = Wire.Obj("version", 5, "session", "session", "id", request["id"]);
                    try { reply["result"] = (string)request["method"] == "hello"
                        ? Wire.Obj("platform", "windows", "desktopMode", "shared", "maxTimeoutMs", 120000, "capabilities", runtime.Capabilities)
                        : runtime.Perform((string)request["method"], Wire.Object(request["params"]), Request(Convert.ToInt32(request["timeoutMs"])));
                    } catch (Failure e) { reply["error"] = Wire.Obj("code", e.Code, "effect", "none", "target", "fixture", "detail", e.Message, "next", e.Next); }
                    File.WriteAllText(journal + ".effects.json", JsonSerializer.Serialize(new { batches = pointer.Batches.Count, activated = pointer.Activated,
                        objectSent = objects.Sent, acquired = objects.Acquired, released = objects.Released }));
                    Console.WriteLine(JsonSerializer.Serialize(reply)); Console.Out.Flush();
                }
            } } finally { runtime.Close(); }
            return 0;
        }
        static int Main(string[] args) {
            if (args.Length == 3 && args[0] == "--serve") return Serve(args[1], args[2]);
            Geometry();
            var p = new PointerProbe(); var objects = new ObjectProbe(); var runtime = Runtime(p, objects);
            Assert(runtime.Capabilities.Contains("nativePointer") && !runtime.Capabilities.Contains("nativeDesktopInput") && !runtime.Capabilities.Contains("nativeElementPointer"), "unsupported capability declared");
            Refused("invalid_request", () => Begin(runtime, parameters: Wire.Obj("x", 1, "y", 1, "count", 3)));
            Refused("invalid_request", () => Begin(runtime, "scroll", Wire.Obj("x", 1, "y", 1, "direction", "down", "pages", 0)));
            Refused("invalid_request", () => Begin(runtime, parameters: Wire.Obj("x", Double.PositiveInfinity, "y", 1)));
            Refused("mode_unsupported", () => Begin(runtime, "key", Wire.Obj("key", "Return")));
            var wrong = Target(); wrong["node"] = "node"; Refused("invalid_request", () => Begin(runtime, target: wrong));
            Assert(objects.Acquired == 0, "invalid input acquired desktop ownership");
            string op = Begin(runtime); var facts = Inspect(runtime, op); var command = Command(facts); var receipt = Execute(runtime, op, command);
            Assert((string)receipt["dispatch"] == "dispatched" && (string)receipt["confirmation"] == "unavailable", "insertion was claimed as confirmed GUI effect");
            Assert(p.Batches.Count == 1 && p.Batches[0].Select(e => e.Flags).SequenceEqual(new uint[] { 0xc001, 2, 4 }), "click is not a paired absolute physical sequence");
            Assert(p.Activated == 0 && objects.Reads == 0, "ready pointer acquired foreground or UIA");
            Assert(ReferenceEquals(receipt, Execute(runtime, op, command)) && p.Batches.Count == 1, "duplicate step replayed click");
            Refused("execution_unknown", () => Execute(runtime, op, command, 2)); Finish(runtime, op);
            foreach (string changed in new[] { "geometry", "layout", "modal", "held", "identity" }) {
                op = Begin(runtime); command = Command(Inspect(runtime, op)); int before = p.Batches.Count;
                p.GeometryChanged = changed == "geometry"; p.LayoutChanged = changed == "layout"; p.Modal = changed == "modal"; p.Held = changed == "held"; p.Alive = changed != "identity";
                receipt = Execute(runtime, op, command);
                Assert((string)receipt["dispatch"] == "notDispatched" && p.Batches.Count == before && receipt["errorCode"] != null, changed + " did not prevent dispatch");
                Finish(runtime, op); p.GeometryChanged = p.LayoutChanged = p.Modal = p.Held = false; p.Alive = true;
            }
            op = Begin(runtime); command = Command(Inspect(runtime, op)); p.OnIdle = r => p.Alive = false;
            receipt = Execute(runtime, op, command); Assert((string)receipt["errorCode"] == "stale_target" && (string)receipt["dispatch"] == "notDispatched", "target was only checked at entry");
            Finish(runtime, op); p.Alive = true; p.OnIdle = null;
            p.Ready = false; op = Begin(runtime); facts = Inspect(runtime, op);
            receipt = Execute(runtime, op, Command(facts, true, true));
            Assert((string)receipt["errorCode"] == "mode_unsupported" && p.Activated == 0, "strict shared foreground policy bypassed"); Finish(runtime, op);

            var keys = new KeyboardProbe(); var assistedPointer = new PointerProbe { Ready = false }; var assistedObjects = new ObjectProbe();
            var assisted = Runtime(assistedPointer, assistedObjects, true, keys);
            op = Begin(assisted); facts = Inspect(assisted, op); receipt = Execute(assisted, op, Command(facts, true, true));
            Assert((string)receipt["confirmation"] == "confirmed" && assistedPointer.Activated == 1 && keys.Activated == 0, "pointer preparation was routed through keyboard");
            receipt = Execute(assisted, op, Command(Inspect(assisted, op)), 2);
            Assert((string)receipt["dispatch"] == "dispatched" && assistedPointer.Activated == 1, "dispatch activated again"); Finish(assisted, op);
            p.Ready = true;
            foreach (string button in new[] { "right", "middle" }) {
                op = Begin(runtime, parameters: Wire.Obj("x", 10, "y", 20, "button", button, "count", 2));
                receipt = Execute(runtime, op, Command(Inspect(runtime, op)));
                Assert((string)receipt["dispatch"] == "dispatched" && p.Batches.Last()[1].Flags == (button == "right" ? 8u : 32u), "button/count semantics changed"); Finish(runtime, op);
            }
            foreach (string direction in new[] { "up", "down", "left", "right" }) {
                op = Begin(runtime, "scroll", Wire.Obj("x", 10, "y", 20, "direction", direction, "pages", 0.5));
                receipt = Execute(runtime, op, Command(Inspect(runtime, op))); var wheel = p.Batches.Last().Last();
                Assert((string)receipt["dispatch"] == "dispatched" && wheel.Flags == (direction == "left" || direction == "right" ? 0x1000u : 0x800u) &&
                    wheel.Data == (direction == "up" || direction == "right" ? 180 : -180), "wheel direction/amount incorrect"); Finish(runtime, op);
            }
            var drag = Wire.Obj("fromX", 10, "fromY", 20, "toX", 350, "toY", 260);
            op = Begin(runtime, "drag", drag); command = Command(Inspect(runtime, op)); int startBatch = p.Batches.Count;
            p.OnSend = events => { p.GeometryChanged = true; return events.Length; };
            receipt = Execute(runtime, op, command);
            Assert((string)receipt["dispatch"] == "dispatched" && p.Batches.Count-startBatch == 26 && p.Batches.Last().Single().Flags == 4, "owned grab could not finish a moving-window drag");
            Assert(p.Points.Last().X == 150 && p.Points.Last().Y == 310, "drag recomputed coordinates after movement");
            Finish(runtime, op); p.GeometryChanged = false; p.OnSend = null;

            foreach (int prefix in new[] { 0, 1, 2 }) {
                op = Begin(runtime); command = Command(Inspect(runtime, op)); startBatch = p.Batches.Count;
                p.OnSend = events => events.Length == 3 ? prefix : events.Length;
                receipt = Execute(runtime, op, command);
                Assert((string)receipt["dispatch"] == "unknown" && (string)receipt["cleanup"] == "complete", "short SendInput lost uncertainty");
                Assert(p.Batches.Count-startBatch == (prefix == 2 ? 2 : 1), "released an unpressed button or skipped an owned release"); Finish(runtime, op);
            }
            p.OnSend = null;
            op = Begin(runtime, "drag", drag); command = Command(Inspect(runtime, op)); var cancelled = new CancellationTokenSource();
            p.OnSend = events => { if (events.Any(e => e.Flags == 2)) cancelled.Cancel(); return events.Length; };
            receipt = Execute(runtime, op, command, r: new Request(cancelled.Token, 1000));
            Assert((string)receipt["dispatch"] == "unknown" && (string)receipt["errorCode"] == "cancelled" && (string)receipt["cleanup"] == "complete" && p.Batches.Last().Single().Flags == 4,
                "cancelled drag skipped unconditional paired release"); Finish(runtime, op); p.OnSend = null;
            op = Begin(runtime, parameters: Wire.Obj("x", 10, "y", 20, "count", 2)); command = Command(Inspect(runtime, op)); startBatch = p.Batches.Count;
            p.OnSend = events => { p.Alive = false; return events.Length; };
            receipt = Execute(runtime, op, command);
            Assert((string)receipt["dispatch"] == "unknown" && (string)receipt["errorCode"] == "stale_target" && p.Batches.Count-startBatch == 1, "second click retargeted after closing the window");
            Finish(runtime, op); p.OnSend = null; p.Alive = true;
            Assert(objects.Acquired == objects.Released, "pointer operations leaked desktop lease");
            var failedPointer = new PointerProbe(); var failedObjects = new ObjectProbe(); var failed = Runtime(failedPointer, failedObjects);
            op = Begin(failed, "drag", drag); command = Command(Inspect(failed, op));
            failedPointer.OnSend = events => { if (events.Length > 1) throw new Exception("lost reply after button down"); return 0; };
            receipt = Execute(failed, op, command);
            Assert((string)receipt["cleanup"] == "failed" && (string)receipt["dispatch"] == "unknown", "uncertain cleanup was hidden");
            Finish(failed, op); Refused("execution_unknown", () => Begin(failed));
            Assert(failedObjects.Acquired == failedObjects.Released, "cleanup failure lost the lease");
            Console.WriteLine(JsonSerializer.Serialize(new { assertions = assertions, facts = facts, receipt = receipt,
                scope = "Production Windows pointer unit with fake OS providers; no Windows GUI execution" })); return 0;
        }
    }
}
