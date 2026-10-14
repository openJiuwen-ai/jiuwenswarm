using System;
using System.Collections.Generic;
using System.Linq;

namespace ComputerUse.Windows {
    internal sealed class NativeScrollState {
        internal object Native;
        internal string Role, Label;
        internal bool? Enabled, Scrollable;
        internal double? Percent, ViewSize;
    }
    internal interface INativeScrollProvider {
        NativeScrollState Read(Dictionary<string, object> target, string[] within, string direction, Request request);
        Action Prepare(NativeScrollState state, string direction, double pages, Request request);
    }
    // One addressed ScrollPattern recipe. It never chooses an ancestor,
    // activates a window, changes focus or substitutes wheel input after sending.
    internal sealed class NativeScroll {
        readonly INativeScrollProvider provider;
        internal NativeScroll(INativeScrollProvider provider) { this.provider = provider; }
        internal static void Validate(Dictionary<string, object> intent) {
            if ((string)intent["kind"] != "scroll") throw new Failure("invalid_request", "native scrolling requires a scroll intent");
            var p = Wire.Object(intent["parameters"]); Wire.Keys(p, "direction,pages");
            if (!new[] { "up", "down", "left", "right" }.Contains(Wire.Str(p, "direction")))
                throw new Failure("invalid_request", "invalid scroll direction");
            Wire.Num(p, "pages", 0.00001, 100);
        }
        internal static double FractionalPosition(NativeScrollState state, string direction, double pages) {
            if (!state.Percent.HasValue || !state.ViewSize.HasValue || Double.IsNaN(state.Percent.Value)
                    || Double.IsNaN(state.ViewSize.Value) || state.Percent < 0 || state.Percent > 100
                    || state.ViewSize <= 0 || state.ViewSize >= 100)
                throw new Failure("input_unavailable", "fractional scrolling requires the provider's current scroll percentage and viewport size");
            double delta = pages * state.ViewSize.Value / (100 - state.ViewSize.Value) * 100;
            return Math.Max(0, Math.Min(100, state.Percent.Value + (direction == "up" || direction == "left" ? -delta : delta)));
        }
        internal object Inspect(NativeWriteOperation operation, Dictionary<string, object> target, string[] within, Request r) {
            Validate(operation.Intent);
            if (target["node"] == null || target["capture"] != null)
                throw new Failure("invalid_request", "native scrolling needs an observed scrolling element");
            var p = Wire.Object(operation.Intent["parameters"]);
            var state = provider.Read(target, within, (string)p["direction"], r);
            bool available = state.Scrollable == true;
            if (available && Convert.ToDouble(p["pages"]) % 1 != 0) {
                try { FractionalPosition(state, (string)p["direction"], 0.5); }
                catch (Failure) { available = false; }
            }
            var contract = new NativeInputContract { Id = "primitive:" + Guid.NewGuid().ToString("N"), Unit = "native_scroll",
                Target = new Dictionary<string, object>(target), Within = (string[])within.Clone(), Role = state.Role, Supported = available };
            operation.Contracts.Add(contract.Id, contract);
            return Wire.Obj("kind", "inputContext", "binding", new Dictionary<string, object>(target), "observed", "observation:" + Guid.NewGuid().ToString("N"),
                "targetLabel", String.IsNullOrWhiteSpace(state.Label) ? "Scrolling control" : state.Label,
                "identity", "yes", "withinScope", "yes", "enabled", state.Enabled == true ? "yes" : state.Enabled == false ? "no" : "unknown",
                "blockedBy", new string[0], "within", within, "nativeRole", state.Role, "actionsComplete", true,
                "primitives", new[] { Wire.Obj("id", contract.Id, "unit", contract.Unit, "channel", "object", "supported", available ? "yes" : state.Scrollable == null ? "unknown" : "no",
                    "readiness", available && state.Enabled == true ? "yes" : "no", "pending", state.Enabled == true ? new string[0] : new[] { "enabled" }, "backgroundPreparation", "no") },
                "input", Wire.Obj("foregroundSurface", null, "keyboardSurface", null, "focusedNode", null, "focusAncestors", new string[0],
                    "editor", null, "editorOwner", null, "selectionKnown", "unknown", "geometryCurrent", "unknown", "windowRoute", "unknown"));
        }
        static void Check(NativeScrollState state, NativeInputContract contract) {
            if (state.Role != contract.Role || state.Enabled != true || state.Scrollable != true)
                throw new Failure("input_unavailable", "the addressed control no longer supports enabled scrolling in this direction");
        }
        internal object Execute(NativeWriteOperation operation, Dictionary<string, object> command, Request r) {
            var target = Wire.Object(command["target"]);
            double remaining = 0;
            var result = Wire.Obj("target", new Dictionary<string, object>(target), "dispatch", "notDispatched", "detail", "No native scrolling dispatched.",
                "preparation", new string[0], "confirmation", "unavailable", "cleanup", "complete", "errorCode", null, "retryReason", null, "context", null);
            try {
                NativeInputContract contract;
                if (!operation.Contracts.TryGetValue((string)command["primitiveId"], out contract) || contract.Unit != "native_scroll"
                        || !contract.Supported || !NativeWriteOperation.Same(contract.Target, target))
                    throw new Failure("stale_target", "the scrolling contract expired or belongs to another element");
                if ((string)command["kind"] != "native_scroll" || (bool)command["foreground"])
                    throw new Failure("invalid_request", "native scrolling cannot activate or prepare a window");
                Validate(operation.Intent); var p = Wire.Object(operation.Intent["parameters"]);
                string direction = (string)p["direction"]; remaining = Convert.ToDouble(p["pages"]);
                while (remaining > 0) {
                    var state = provider.Read(target, contract.Within, direction, r); Check(state, contract);
                    double amount = Math.Min(1, remaining);
                    var mutate = provider.Prepare(state, direction, amount, r);
                    // A pattern getter can execute provider code. Recheck the
                    // exact target/scope before every page, including page two.
                    Check(provider.Read(target, contract.Within, direction, r), contract);
                    r.Sending(); operation.Dispatched = true; result["dispatch"] = "unknown";
                    mutate(); result["dispatch"] = "dispatched";
                    remaining -= amount; r.Check();
                }
                result["detail"] = "Native scrolling returned; read the control to verify the new viewport.";
            } catch (Exception error) {
                if ((string)result["dispatch"] == "dispatched" && remaining > 0) result["dispatch"] = "partial";
                var failure = error as Failure;
                result["errorCode"] = failure != null ? failure.Code : operation.Dispatched ? "execution_unknown" : "input_unavailable";
                result["detail"] = String.IsNullOrWhiteSpace(error.Message) ? "Native scrolling failed." : error.Message;
                if (!operation.Dispatched && (string)result["errorCode"] == "input_unavailable") result["retryReason"] = "routeUnavailable";
            }
            return result;
        }
    }
}
