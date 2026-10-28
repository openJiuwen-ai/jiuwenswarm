using System;
using System.Collections.Generic;
using System.Linq;

namespace ComputerUse.Windows {
    internal struct NativePointerEvent {
        internal readonly uint Flags;
        internal readonly int X, Y, Data;
        internal NativePointerEvent(uint flags, int x = 0, int y = 0, int data = 0) { Flags = flags; X = x; Y = y; Data = data; }
    }
    internal sealed class NativePointerState {
        internal object Native;
        internal bool Ready, Foreground;
    }
    internal interface INativePointerProvider {
        NativePointerCapture Capture(Dictionary<string, object> target, Request r);
        NativePointerState Read(Dictionary<string, object> target, NativePointerPoint point, string kind, bool dragging, Request r);
        void RequireIdle(Request r);
        void Activate(NativePointerState state, Request r);
        int Send(NativePointerEvent[] events);
    }
    internal sealed class NativePointerPlan {
        internal NativePointerCapture Capture;
        internal NativePointerPoint[] Points;
        internal string Kind;
        internal uint Down, Up, Wheel;
        internal int Count, Amount;
        internal NativePointerEvent Move(int index) {
            var p = Capture.Normalize(Points[index]); return new NativePointerEvent(0xc001, p.X, p.Y);
        }
    }
    // One requested physical sequence. Common route selection and retry policy
    // remain in Python; this unit owns dispatch accounting and paired release.
    internal sealed class NativePointer {
        readonly INativePointerProvider provider;
        readonly bool foregroundAllowed;
        internal bool SupportsDesktop { get; private set; }
        internal NativePointer(INativePointerProvider provider, string mode, bool assist) {
            this.provider = provider; foregroundAllowed = mode == "exclusive" || assist; SupportsDesktop = mode == "exclusive";
        }
        internal static bool Intent(Dictionary<string, object> intent, Dictionary<string, object> target) {
            return target != null && target["capture"] != null && new[] { "click", "scroll", "drag" }.Contains((string)intent["kind"]);
        }
        internal void Validate(Dictionary<string, object> intent, Dictionary<string, object> target) {
            if (target["node"] != null) throw new Failure("invalid_request", "image input cannot select an accessibility element");
            if (target["surface"] == null && !SupportsDesktop) throw new Failure("mode_unsupported", "desktop pointer input requires exclusive mode");
            string kind = (string)intent["kind"]; var p = Wire.Object(intent["parameters"]);
            if (kind == "click") {
                Wire.Keys(p, "x,y", "button,count");
                if (p.ContainsKey("button") && !new[] { "left", "middle", "right" }.Contains(Wire.Str(p, "button"))) throw new Failure("invalid_request", "invalid pointer button");
                if (p.ContainsKey("count")) Wire.Num(p, "count", 1, 2, true);
            } else if (kind == "scroll") {
                Wire.Keys(p, "x,y,direction,pages");
                if (!new[] { "up", "down", "left", "right" }.Contains(Wire.Str(p, "direction"))) throw new Failure("invalid_request", "invalid scroll direction");
                Wire.Num(p, "pages", 0.00001, 100);
            } else if (kind == "drag") Wire.Keys(p, "fromX,fromY,toX,toY");
            else throw new Failure("invalid_request", "unsupported pointer intent");
            foreach (var key in kind == "drag" ? new[] { "fromX", "fromY", "toX", "toY" } : new[] { "x", "y" }) Wire.Num(p, key, 0, 4096);
        }
        NativePointerPlan Plan(NativeWriteOperation operation, Dictionary<string, object> target, Request r) {
            Validate(operation.Intent, target);
            var image = provider.Capture(target, r); var p = Wire.Object(operation.Intent["parameters"]); string kind = (string)operation.Intent["kind"];
            var start = image.Point(Convert.ToDouble(p[kind == "drag" ? "fromX" : "x"]), Convert.ToDouble(p[kind == "drag" ? "fromY" : "y"]));
            var points = new List<NativePointerPoint> { start };
            if (kind == "drag") {
                var end = image.Point(Convert.ToDouble(p["toX"]), Convert.ToDouble(p["toY"]));
                for (int i = 1; i <= 24; i++) points.Add(new NativePointerPoint(start.X + (int)Math.Round((end.X-start.X)*i/24.0), start.Y + (int)Math.Round((end.Y-start.Y)*i/24.0)));
            }
            foreach (var point in points) image.RequirePoint(point); // Validate the complete trajectory before any preparation.
            string button = p.ContainsKey("button") ? (string)p["button"] : "left";
            var plan = new NativePointerPlan { Capture = image, Points = points.ToArray(), Kind = kind,
                Down = button == "left" ? 2u : button == "right" ? 8u : 32u,
                Up = button == "left" ? 4u : button == "right" ? 16u : 64u, Count = p.ContainsKey("count") ? Convert.ToInt32(p["count"]) : 1 };
            if (kind == "scroll") {
                string direction = (string)p["direction"];
                plan.Wheel = direction == "left" || direction == "right" ? 0x1000u : 0x800u;
                // Keep the existing Windows API's approximate three-detent page.
                plan.Amount = (int)Math.Round(120 * Math.Max(1, Convert.ToDouble(p["pages"]) * 3)) * (direction == "up" || direction == "right" ? 1 : -1);
            }
            return plan;
        }
        internal object Inspect(NativeWriteOperation operation, Dictionary<string, object> target, string[] within, Request r) {
            if (within.Length != 0 || target["node"] != null) throw new Failure("invalid_request", "image input cannot select element ancestors");
            var plan = Plan(operation, target, r); var state = provider.Read(target, plan.Points[0], plan.Kind, false, r); r.Check();
            var contract = new NativeInputContract { Id = "primitive:" + Guid.NewGuid().ToString("N"), Unit = "pointer_sequence", Supported = true,
                Target = new Dictionary<string, object>(target), Within = new string[0], Pointer = plan };
            operation.Contracts.Add(contract.Id, contract);
            return Wire.Obj("kind", "inputContext", "binding", new Dictionary<string, object>(target), "observed", "observation:" + Guid.NewGuid().ToString("N"),
                "targetLabel", target["surface"] == null ? "Captured desktop" : "Captured window", "identity", "yes", "withinScope", "yes", "enabled", "unknown",
                "blockedBy", new string[0], "nativeRole", null, "actionsComplete", false, "within", within,
                "primitives", new[] { Wire.Obj("id", contract.Id, "unit", contract.Unit, "channel", "pointer", "supported", "yes", "readiness", state.Ready ? "yes" : "no",
                    "pending", state.Ready ? new string[0] : new[] { "foreground" }, "backgroundPreparation", "no") },
                "input", Wire.Obj("foregroundSurface", state.Foreground ? target["surface"] : null, "keyboardSurface", null, "focusedNode", null,
                    "focusAncestors", new string[0], "editor", null, "editorOwner", null, "selectionKnown", "unknown", "geometryCurrent", "yes",
                    "windowRoute", "unknown", "pointerRoute", state.Ready ? "yes" : "no"));
        }
        NativePointerState Guard(NativeInputContract contract, int point, bool dragging, Request r) {
            var plan = contract.Pointer;
            if (!ReferenceEquals(provider.Capture(contract.Target, r), plan.Capture)) throw new Failure("snapshot_expired", "the original image expired");
            var state = provider.Read(contract.Target, plan.Points[point], plan.Kind, dragging, r); r.Check(); return state;
        }
        void Send(NativeWriteOperation operation, NativePointerPlan plan, NativePointerEvent[] events, Request r, Dictionary<string, object> result, ref bool down) {
            r.Sending(); operation.Dispatched = true; result["dispatch"] = "unknown";
            bool prior = down;
            if (events.Any(e => (e.Flags & plan.Down) != 0)) down = true; // A thrown call may have sent its prefix.
            int sent = provider.Send(events);
            if (sent >= 0 && sent <= events.Length) {
                down = prior;
                foreach (var e in events.Take(sent)) {
                    if ((e.Flags & plan.Down) != 0) down = true;
                    if ((e.Flags & plan.Up) != 0) down = false;
                }
            }
            if (sent != events.Length) throw new Failure("execution_unknown", "SendInput accepted " + sent + "/" + events.Length + " pointer events; input was not replayed");
        }
        internal object Execute(NativeWriteOperation operation, Dictionary<string, object> command, Request r) {
            var target = Wire.Object(command["target"]); var preparation = new List<string>(); bool down = false;
            NativeInputContract contract = null;
            var result = Wire.Obj("target", new Dictionary<string, object>(target), "dispatch", "notDispatched", "detail", "No pointer input dispatched.",
                "preparation", preparation, "confirmation", "unavailable", "cleanup", "complete", "errorCode", null, "retryReason", null, "context", null);
            try {
                if (!operation.Contracts.TryGetValue((string)command["primitiveId"], out contract) || contract.Unit != "pointer_sequence" || contract.Pointer == null ||
                        !contract.Supported || !NativeWriteOperation.Same(target, contract.Target)) throw new Failure("stale_target", "the pointer contract expired or belongs to another image");
                bool preparing = (string)command["kind"] == "prepare_receiver";
                if (!preparing && ((string)command["kind"] != "pointer_sequence" || (bool)command["foreground"])) throw new Failure("invalid_request", "pointer dispatch cannot activate a window");
                Validate(operation.Intent, target); var plan = contract.Pointer;
                if (preparing) {
                    var state = Guard(contract, 0, false, r);
                    if (!state.Ready && (bool)command["foreground"] && target["surface"] != null) {
                        if (!foregroundAllowed) throw new Failure("mode_unsupported", "host policy does not permit foreground assistance");
                        provider.RequireIdle(r); state = Guard(contract, 0, false, r);
                        if (!state.Ready) {
                            r.Check(); preparation.Add("foreground"); r.Effect = true; provider.Activate(state, r);
                            double until = Math.Min(r.Deadline, Wire.Now + 400);
                            while (!(state = Guard(contract, 0, false, r)).Ready && Wire.Now < until) r.Wait(20);
                        }
                    }
                    if (!state.Ready) throw new Failure("input_unavailable", "the captured point is not reachable; no requested input sent");
                    result["confirmation"] = "confirmed"; result["detail"] = "The captured pointer target is reachable.";
                } else {
                    provider.RequireIdle(r);
                    if (!Guard(contract, 0, false, r).Ready) throw new Failure("input_unavailable", "the captured point no longer reaches the selected window");
                    if (plan.Kind == "drag") {
                        Send(operation, plan, new[] { plan.Move(0), new NativePointerEvent(plan.Down) }, r, result, ref down);
                        for (int i = 1; i < plan.Points.Length; i++) {
                            r.Wait(12);
                            if (!Guard(contract, i, true, r).Ready) throw new Failure("input_unavailable", "the original window lost the drag receiver");
                            Send(operation, plan, new[] { plan.Move(i) }, r, result, ref down);
                        }
                        Send(operation, plan, new[] { new NativePointerEvent(plan.Up) }, r, result, ref down);
                    } else if (plan.Kind == "scroll") {
                        Send(operation, plan, new[] { plan.Move(0), new NativePointerEvent(plan.Wheel, data: plan.Amount) }, r, result, ref down);
                    } else {
                        for (int i = 0; i < plan.Count; i++) {
                            if (i > 0) {
                                r.Wait(60); provider.RequireIdle(r);
                                if (!Guard(contract, 0, false, r).Ready) throw new Failure("input_unavailable", "the target changed after the first click; remaining click was not sent");
                            }
                            Send(operation, plan, new[] { plan.Move(0), new NativePointerEvent(plan.Down), new NativePointerEvent(plan.Up) }, r, result, ref down);
                        }
                    }
                    result["dispatch"] = "dispatched"; result["detail"] = "Pointer sequence sent; read to verify its effect.";
                    r.Check(); // The final click/drop can legitimately destroy its target.
                }
            } catch (Exception error) {
                var failure = error as Failure;
                result["errorCode"] = failure != null ? failure.Code : operation.Dispatched ? "execution_unknown" : "input_unavailable";
                result["detail"] = String.IsNullOrWhiteSpace(error.Message) ? "Native pointer operation failed." : error.Message;
                if (!operation.Dispatched && (string)result["errorCode"] == "input_unavailable") result["retryReason"] = (string)command["kind"] == "prepare_receiver" ? "preparationUnavailable" : "routeUnavailable";
            } finally {
                // Cleanup has no cancellable Request or target lookup. Even a
                // destroyed window cannot strand our button in the down state.
                if (down) {
                    try { if (provider.Send(new[] { new NativePointerEvent(contract.Pointer.Up) }) != 1) operation.Cleanup = "failed"; }
                    catch { operation.Cleanup = "failed"; }
                }
            }
            result["cleanup"] = operation.Cleanup;
            if (operation.Cleanup != "complete") {
                result["retryReason"] = null; result["errorCode"] = result["errorCode"] ?? "execution_unknown";
                result["detail"] = (string)result["detail"] + " Pointer release was not confirmed; reconnect before more input.";
            }
            return result;
        }
    }
}
