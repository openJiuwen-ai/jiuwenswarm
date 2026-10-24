// Production desktop contracts/dispatch with fake OS boundaries. Real Win32
// delivery is separately accepted through the public Python API on Windows.
using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Text.Json;

namespace ComputerUse.Windows {
    internal static class NativeDesktopTests {
        static int assertions;
        static void Assert(bool value, string reason) { assertions++; if (!value) throw new Exception(reason); }
        static Request Request() { return new Request(CancellationToken.None, 5000); }
        static Dictionary<string, object> Target(string capture = "desktop") {
            return Wire.Obj("session", "session", "instance", null, "rootWindow", null, "surface", null, "node", null, "capture", capture);
        }
        static object Begin(NativeRuntime runtime, string kind, object parameters, string capture = "desktop") {
            return runtime.Perform("begin", Wire.Obj("mode", "write", "target", Target(capture), "intent", Wire.Obj("kind", kind, "parameters", parameters)), Request());
        }
        static void Refused(string code, Action action) {
            try { action(); } catch (Failure error) { Assert(error.Code == code, "Wrong failure: " + error.Code); return; }
            throw new Exception("Expected " + code);
        }
        static Dictionary<string, object> Run(NativeRuntime runtime, string kind, object parameters, Action<object, string> repeat = null) {
            string operation = (string)Wire.Object(Begin(runtime, kind, parameters))["operation"];
            try {
                var facts = Wire.Object(runtime.Perform("inspect", Wire.Obj("operation", operation, "target", Target(),
                    "query", Wire.Obj("kind", "inputContext", "within", new string[0])), Request()));
                var primitive = Wire.Object(Wire.Array(facts["primitives"])[0]);
                Assert((string)primitive["readiness"] == "yes", "Desktop route not ready");
                var command = Wire.Obj("kind", primitive["unit"], "target", Target(), "primitiveId", primitive["id"], "foreground", false);
                var request = Wire.Obj("operation", operation, "step", 1, "command", command);
                var result = Wire.Object(runtime.Perform("execute", request, Request()));
                if (repeat != null) repeat(request, operation);
                return result;
            } finally { runtime.Perform("finish", Wire.Obj("operation", operation), Request()); }
        }
        static int Main() {
            var objects = new ObjectProbe { Text = "" };
            var keys = new TextProbe(objects) { Foreground = true, Route = true, Focused = true };
            var pointer = new PointerProbe();
            var canvas = new NativeCaptureGeometry(-1000, -100, 2000, 1200, 2000);
            pointer.Captures.Add("desktop", Target(null), canvas, canvas, "layout", new[] { canvas });
            var runtime = new NativeRuntime("session", new ObjectProbeRead(objects), new NativeExecution(objects,
                pointer: new NativePointer(pointer, "exclusive", false), desktopKeyboard: new NativeKeyboard(keys, "exclusive", false)));
            Assert(runtime.Capabilities.Contains("nativeDesktopInput"), "Missing desktop capability");
            Refused("invalid_request", () => Begin(runtime, "key", Wire.Obj("key", "Return"), null));
            Refused("invalid_request", () => Begin(runtime, "replace", Wire.Obj("text", "x")));
            // Desktop focus is the foreground native queue, not the UIA focus
            // announcement which can follow an autocomplete suggestion.
            keys.AfterSend = () => keys.Focused = false;
            var result = Run(runtime, "insert", Wire.Obj("text", "VS Code 中文😀"));
            Assert((string)result["dispatch"] == "dispatched" && objects.Text == "VS Code 中文😀", "Desktop text interrupted by UIA focus announcement");
            result = Run(runtime, "key", Wire.Obj("key", "super+d"), (request, operation) => {
                int batches = keys.Batches.Count;
                runtime.Perform("execute", Wire.Object(request), Request());
                Assert(keys.Batches.Count == batches, "Duplicate desktop input replayed");
            });
            Assert((string)result["dispatch"] == "dispatched" && keys.Activated == 0, "Global shortcut tried to activate a window");
            foreach (string kind in new[] { "click", "scroll", "drag" }) {
                object parameters = kind == "click" ? Wire.Obj("x", 5, "y", 5) : kind == "scroll" ? Wire.Obj("x", 5, "y", 5, "direction", "down", "pages", 1)
                    : Wire.Obj("fromX", 5, "fromY", 5, "toX", 20, "toY", 20);
                result = Run(runtime, kind, parameters);
                Assert((string)result["dispatch"] == "dispatched" && pointer.Activated == 0, "Desktop pointer needed window activation");
            }
            keys.AfterSend = () => keys.Recipient++;
            result = Run(runtime, "insert", Wire.Obj("text", "xyz"));
            Assert((string)result["dispatch"] == "partial" && objects.Text.EndsWith("x"), "Real recipient change did not stop remaining text");
            Assert((string)result["cleanup"] == "complete", "Partial typing did not clean up");
            runtime.Close();
            Assert(objects.Acquired == objects.Released, "Desktop lease leaked");
            runtime = new NativeRuntime("session", new ObjectProbeRead(objects), new NativeExecution(objects,
                new NativeKeyboard(keys, "shared", true), new NativePointer(pointer, "shared", true)));
            Assert(!runtime.Capabilities.Contains("nativeDesktopInput"), "Shared advertised desktop input");
            Refused("mode_unsupported", () => Begin(runtime, "key", Wire.Obj("key", "Return")));
            Refused("mode_unsupported", () => Begin(runtime, "click", Wire.Obj("x", 5, "y", 5)));
            Console.WriteLine(JsonSerializer.Serialize(new { assertions = assertions })); return 0;
        }
    }
}
