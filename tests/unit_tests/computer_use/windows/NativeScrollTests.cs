// Real production scroll contracts with a deliberately mutable provider.
using System;
using System.Collections.Generic;
using System.Threading;
using System.Text.Json;

namespace ComputerUse.Windows {
    internal sealed class ScrollProbe : INativeScrollProvider {
        internal bool Alive = true, Enabled = true, Scrollable = true;
        internal int Sent, Reads;
        internal double Percent = 0, ViewSize = 20;
        internal Action OnPrepare, OnSend;
        internal string Direction;
        public NativeScrollState Read(Dictionary<string, object> target, string[] within, string direction, Request r) {
            r.Check(); Reads++;
            if (!Alive) throw new Failure("stale_target", "scrolling object changed");
            return new NativeScrollState { Native = this, Role = "ControlType.List", Label = "Items", Enabled = Enabled,
                Scrollable = Scrollable, Percent = Percent, ViewSize = ViewSize };
        }
        public Action Prepare(NativeScrollState state, string direction, double pages, Request r) {
            if (OnPrepare != null) OnPrepare(); r.Check();
            return () => { Sent++; Direction = direction; Percent = NativeScroll.FractionalPosition(state, direction, pages); if (OnSend != null) OnSend(); };
        }
    }
    internal static class NativeScrollTests {
        static int assertions;
        static void Assert(bool value, string why) { assertions++; if (!value) throw new Exception(why); }
        static Request Req() { return new Request(CancellationToken.None, 10000); }
        static Dictionary<string, object> Target() { return Wire.Obj("session", "session", "instance", "instance", "rootWindow", "window", "surface", "surface", "node", "node", "capture", null); }
        static Dictionary<string, object> Intent(string direction, double pages) { return Wire.Obj("kind", "scroll", "parameters", Wire.Obj("direction", direction, "pages", pages)); }
        static Dictionary<string, object> Facts(NativeScroll unit, NativeWriteOperation op) { return Wire.Object(unit.Inspect(op, Target(), new string[0], Req())); }
        static Dictionary<string, object> Command(Dictionary<string, object> facts) {
            var primitive = Wire.Object(Wire.Array(facts["primitives"])[0]);
            return Wire.Obj("kind", "native_scroll", "primitiveId", primitive["id"], "target", Target(), "foreground", false);
        }
        static Dictionary<string, object> Run(ScrollProbe probe, double pages = 1, string direction = "down") {
            var unit = new NativeScroll(probe); var op = new NativeWriteOperation(Target(), Intent(direction, pages));
            return Wire.Object(unit.Execute(op, Command(Facts(unit, op)), Req()));
        }
        static int Main() {
            var probe = new ScrollProbe(); var receipt = Run(probe, 2.5);
            Assert(probe.Sent == 3 && probe.Percent == 62.5, "large pages and fractional remainder");
            Assert((string)receipt["dispatch"] == "dispatched" && receipt["errorCode"] == null, "dispatch receipt");
            probe = new ScrollProbe { Percent = 80 }; Run(probe, .5, "left");
            Assert(probe.Percent == 67.5 && probe.Direction == "left", "fractional direction");
            Assert(NativeScroll.FractionalPosition(new NativeScrollState { Percent = 99, ViewSize = 20 }, "down", .5) == 100, "clamp at end");
            Assert(NativeScroll.FractionalPosition(new NativeScrollState { Percent = 1, ViewSize = 20 }, "up", .5) == 0, "clamp at start");
            probe = new ScrollProbe(); probe.OnPrepare = () => probe.Alive = false; receipt = Run(probe);
            Assert(probe.Sent == 0 && (string)receipt["dispatch"] == "notDispatched", "revalidate after pattern acquisition");
            Assert((string)receipt["errorCode"] == "stale_target", "scope failure before dispatch");
            probe = new ScrollProbe(); probe.OnSend = () => probe.Alive = false; receipt = Run(probe, 2);
            Assert(probe.Sent == 1 && (string)receipt["dispatch"] == "partial", "no second page after identity loss");
            Assert(receipt["retryReason"] == null && receipt["errorCode"] != null, "partial recipe cannot be retried");
            probe = new ScrollProbe(); probe.OnSend = () => { throw new Exception("provider failed after effect"); }; receipt = Run(probe, 2);
            Assert(probe.Sent == 1 && (string)receipt["dispatch"] == "unknown", "unknown provider effect");
            Assert(receipt["retryReason"] == null, "no fallback after uncertain scroll");
            probe = new ScrollProbe { Scrollable = false }; receipt = Run(probe);
            Assert(probe.Sent == 0, "unsupported axis sends nothing");
            var unit = new NativeScroll(new ScrollProbe()); var op = new NativeWriteOperation(Target(), Intent("down", 1));
            var facts = Facts(unit, op); var command = Command(facts); command["foreground"] = true;
            receipt = Wire.Object(unit.Execute(op, command, Req()));
            Assert((string)receipt["errorCode"] == "invalid_request", "object scroll cannot activate");
            // Also exercise the production runtime dispatch and idempotent step journal.
            probe = new ScrollProbe(); var objects = new ObjectProbe();
            var runtime = new NativeRuntime("session", new ReadProbeBackend(), new NativeExecution(objects, scroll: new NativeScroll(probe)));
            string token = (string)Wire.Object(runtime.Perform("begin", Wire.Obj("mode", "write", "target", Target(), "intent", Intent("down", 1)), Req()))["operation"];
            facts = Wire.Object(runtime.Perform("inspect", Wire.Obj("operation", token, "target", Target(), "query", Wire.Obj("kind", "inputContext", "within", new string[0])), Req()));
            var request = Wire.Obj("operation", token, "step", 1, "command", Command(facts));
            runtime.Perform("execute", request, Req()); runtime.Perform("execute", request, Req());
            Assert(probe.Sent == 1, "duplicate step does not scroll again");
            runtime.Perform("finish", Wire.Obj("operation", token), Req());
            Assert(objects.Acquired == objects.Released, "desktop lock released");
            Console.WriteLine(JsonSerializer.Serialize(Wire.Obj("assertions", assertions, "facts", facts, "receipt", receipt)));
            return 0;
        }
    }
}
