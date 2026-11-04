using System;
using System.Collections.Generic;
using System.Linq;

namespace ComputerUse.Windows {
    // The native adapter supplies facts and exactly one addressed mutation.
    // Python chooses which declared action represents the user's intention.
    internal sealed class NativeObjectState {
        internal object Native;
        internal string Role, Label;
        internal bool? Enabled, Replaceable;
        internal bool ActionsComplete;
        internal string[] Actions = new string[0];
    }
    internal interface INativeObjectProvider {
        IDisposable Acquire(Request request);
        NativeObjectState Read(Dictionary<string, object> target, string[] within, Request request);
        Action Prepare(NativeObjectState state, string unit, string action, string text, Request request);
    }
    internal sealed class NativeInputContract {
        internal string Id, Unit, Action, Role;
        internal bool Supported, Text, Replacement;
        internal Dictionary<string, object> Target;
        internal string[] Within;
        internal NativePointerPlan Pointer;
    }
    internal sealed class NativeWriteOperation {
        internal readonly Dictionary<string, object> Original, Intent;
        internal Dictionary<string, object> Selected;
        internal string[] Within;
        internal bool Dispatched;
        internal string Cleanup = "complete";
        internal readonly Dictionary<string, NativeInputContract> Contracts = new Dictionary<string, NativeInputContract>();
        internal NativeWriteOperation(Dictionary<string, object> original, Dictionary<string, object> intent) {
            Original = new Dictionary<string, object>(original);
            Intent = Wire.Obj("kind", intent["kind"], "parameters", new Dictionary<string, object>(Wire.Object(intent["parameters"])));
        }
        internal static bool Same(Dictionary<string, object> a, Dictionary<string, object> b) {
            return a.Count == b.Count && a.All(p => b.ContainsKey(p.Key) && Equals(p.Value, b[p.Key]));
        }
        internal void Bind(Dictionary<string, object> target, string[] within) {
            if (Dispatched) throw new Failure("execution_unknown", "the operation already dispatched its input; no new contract can be acquired");
            if (!Original.All(p => p.Key == "node" || Equals(p.Value, target[p.Key])) ||
                Original["node"] != null && !Same(Original, target))
                throw new Failure("stale_target", "the input target differs from the original request");
            if (target["node"] == null && within.Length != 0) throw new Failure("invalid_request", "a window input target cannot have element ancestors");
            if (within.Contains((string)target["node"])) throw new Failure("invalid_request", "ancestor constraints must not include the target itself");
            if (Selected != null && (!Same(Selected, target) || !Within.SequenceEqual(within)))
                throw new Failure("stale_target", "the operation already selected another control or ancestor scope");
            if (Selected == null) { Selected = new Dictionary<string, object>(target); Within = (string[])within.Clone(); }
        }
    }
    internal sealed class NativeExecution {
        static readonly string[] ObjectCapabilities = { "nativeBoundContracts", "nativeUnitReadinessV2", "nativeActionIntents", "nativeWindowScope", "nativeTextReplacement", "desktopCoordination" };
        internal string[] Capabilities { get { return ObjectCapabilities.Concat(keyboard == null ? new string[0] : new[] { "nativeKeyboardIntents" })
            .Concat(keyboard != null && keyboard.SupportsText ? new[] { "nativeTextInput" } : new string[0])
            .Concat(pointer == null ? new string[0] : new[] { "nativePointer" })
            .Concat(desktopKeyboard != null && pointer != null && pointer.SupportsDesktop ? new[] { "nativeDesktopInput" } : new string[0])
            .Concat(scroll == null ? new string[0] : new[] { "nativeScrollPattern" }).ToArray(); } }
        readonly INativeObjectProvider provider;
        readonly NativeKeyboard keyboard;
        readonly NativeKeyboard desktopKeyboard;
        readonly NativePointer pointer;
        readonly NativeScroll scroll;
        internal NativeExecution(INativeObjectProvider provider, NativeKeyboard keyboard = null, NativePointer pointer = null, NativeScroll scroll = null, NativeKeyboard desktopKeyboard = null) {
            this.provider = provider; this.keyboard = keyboard; this.pointer = pointer; this.scroll = scroll; this.desktopKeyboard = desktopKeyboard;
        }
        internal IDisposable Acquire(Request r) { return provider.Acquire(r); }
        internal void Validate(Dictionary<string, object> intent, Dictionary<string, object> target) {
            if (NativePointer.Intent(intent, target) && pointer != null) pointer.Validate(intent, target);
            else if (target["surface"] == null) {
                if (target["capture"] == null) throw new Failure("invalid_request", "desktop input requires an observed desktop capture");
                if (desktopKeyboard == null) throw new Failure("mode_unsupported", "desktop input requires exclusive mode and a desktop keyboard provider");
                if (!new[] { "key", "insert" }.Contains((string)intent["kind"])) throw new Failure("invalid_request", "desktop keyboard supports key and insert intents");
                desktopKeyboard.ValidateIntent(intent);
            }
            else if (target["capture"] != null) throw new Failure("mode_unsupported", "this native unit does not implement the captured-image intent");
            else if ((string)intent["kind"] == "scroll" && scroll != null) NativeScroll.Validate(intent);
            else if (KeyboardIntent(intent) && keyboard != null) keyboard.ValidateIntent(intent);
            else ValidateIntent(intent);
        }
        static bool KeyboardIntent(Dictionary<string, object> intent) { return new[] { "focus", "key", "insert", "paste" }.Contains((string)intent["kind"]); }
        internal static void ValidateIntent(Dictionary<string, object> intent) {
            string kind = Wire.Str(intent, "kind"); var p = Wire.Object(intent["parameters"]);
            if (kind == "replace") {
                Wire.Keys(p, "text"); string text = Wire.Str(p, "text", true);
                NativeText.Validate(text);
            } else if (kind == "action") {
                Wire.Keys(p, "action"); if (Wire.Str(p, "action").Length > 16384) throw new Failure("invalid_request", "native action name is too long");
            } else if (kind == "openMenu") Wire.Keys(p, "");
            else if (kind == "click") {
                Wire.Keys(p, "", "button,count");
                if (p.ContainsKey("button") && !new[] { "left", "right", "middle" }.Contains(Wire.Str(p, "button"))) throw new Failure("invalid_request", "invalid click button");
                if (p.ContainsKey("count")) Wire.Num(p, "count", 1, 2, true);
                if (p.ContainsKey("button") && (string)p["button"] != "left" || p.ContainsKey("count") && Convert.ToInt32(p["count"]) != 1)
                    throw new Failure("mode_unsupported", "this object unit cannot synthesize right or repeated pointer clicks");
            } else throw new Failure("mode_unsupported", "this Windows native slice provides addressed actions and whole-value replacement only");
        }
        internal object Inspect(NativeWriteOperation operation, Dictionary<string, object> target, string[] within, Request r) {
            operation.Bind(target, within); operation.Contracts.Clear();
            if (NativePointer.Intent(operation.Intent, target) && pointer != null) return pointer.Inspect(operation, target, within, r);
            if (target["surface"] == null && desktopKeyboard != null) return desktopKeyboard.Inspect(operation, target, within, r);
            if ((string)operation.Intent["kind"] == "scroll" && scroll != null) return scroll.Inspect(operation, target, within, r);
            if (KeyboardIntent(operation.Intent) && keyboard != null) return keyboard.Inspect(operation, target, within, r);
            if (target["node"] == null) throw new Failure("invalid_request", "object input needs an observed native element");
            var state = provider.Read(target, within, r); r.Check();
            var primitives = new List<object>();
            string kind = (string)operation.Intent["kind"];
            Action<string, string, bool?> declare = (unit, action, supported) => {
                var contract = new NativeInputContract { Id = "primitive:" + Guid.NewGuid().ToString("N"), Target = new Dictionary<string, object>(target), Within = (string[])within.Clone(), Unit = unit, Action = action, Role = state.Role };
                operation.Contracts.Add(contract.Id, contract);
                var item = Wire.Obj("id", contract.Id, "unit", unit, "channel", "object", "supported", supported == true ? "yes" : supported == false ? "no" : "unknown",
                    "readiness", supported == true && state.Enabled == true ? "yes" : "no", "pending", state.Enabled == true ? new string[0] : new[] { "enabled" }, "backgroundPreparation", "no");
                if (action != null) item["action"] = Wire.Obj("name", action, "description", action);
                primitives.Add(item);
            };
            if (kind == "replace") declare("native_text_edit", null, state.Replaceable);
            else if (state.ActionsComplete) foreach (string action in state.Actions) declare("native_action", action, true);
            var result = Wire.Obj("kind", "inputContext", "binding", new Dictionary<string, object>(target), "observed", "observation:" + Guid.NewGuid().ToString("N"),
                "targetLabel", String.IsNullOrWhiteSpace(state.Label) ? "Native control" : state.Label, "identity", "yes", "withinScope", "yes",
                "enabled", state.Enabled == true ? "yes" : state.Enabled == false ? "no" : "unknown", "blockedBy", new string[0],
                "primitives", primitives, "nativeRole", state.Role, "actionsComplete", state.ActionsComplete, "within", within,
                "input", Wire.Obj("foregroundSurface", null, "keyboardSurface", null, "focusedNode", null, "focusAncestors", new string[0],
                    "editor", null, "editorOwner", null, "selectionKnown", "unknown", "geometryCurrent", "unknown", "windowRoute", "unknown"));
            if (kind == "replace" && keyboard != null && keyboard.SupportsText) {
                result = Wire.Object(keyboard.Inspect(operation, target, within, r));
                result["primitives"] = primitives.Concat(Wire.Array(result["primitives"])).ToArray();
            }
            return result;
        }
        static void CheckState(NativeObjectState state, NativeInputContract contract) {
            if (state.Enabled != true || state.Role != contract.Role) throw new Failure("input_unavailable", "the selected control's enabled state or role changed");
            if (contract.Unit == "native_action" && (!state.ActionsComplete || state.Actions.Count(x => x == contract.Action) != 1) ||
                contract.Unit == "native_text_edit" && state.Replaceable != true)
                throw new Failure("input_unavailable", "the selected native capability is no longer available");
        }
        internal object Execute(NativeWriteOperation operation, Dictionary<string, object> command, Request r) {
            NativeInputContract selected;
            if (pointer != null && operation.Contracts.TryGetValue((string)command["primitiveId"], out selected) && selected.Unit == "pointer_sequence")
                return pointer.Execute(operation, command, r);
            if (scroll != null && (string)command["kind"] == "native_scroll") return scroll.Execute(operation, command, r);
            var keyboardUnit = Wire.Object(command["target"])["surface"] == null ? desktopKeyboard : keyboard;
            if (keyboardUnit != null && new[] { "key_sequence", "prepare_receiver", "clipboard_paste" }.Contains((string)command["kind"])) return keyboardUnit.Execute(operation, command, r);
            var target = Wire.Object(command["target"]); string unit = Wire.Str(command, "kind"), id = Wire.Str(command, "primitiveId");
            var result = Wire.Obj("target", new Dictionary<string, object>(target), "dispatch", "notDispatched", "detail", "No native input dispatched.",
                "preparation", new string[0], "confirmation", "unavailable", "cleanup", "complete", "errorCode", null, "retryReason", null, "context", null);
            try {
                NativeInputContract contract;
                if (!operation.Contracts.TryGetValue(id, out contract) || contract.Unit != unit || !NativeWriteOperation.Same(contract.Target, target))
                    throw new Failure("stale_target", "the native contract expired or belongs to another target");
                if ((bool)command["foreground"]) throw new Failure("invalid_request", "an addressed native object action does not request foreground preparation");
                string kind = (string)operation.Intent["kind"], text = null;
                if (unit == "native_action") {
                    if (!new[] { "click", "openMenu", "action" }.Contains(kind) || kind == "action" && (string)Wire.Object(operation.Intent["parameters"])["action"] != contract.Action)
                        throw new Failure("invalid_request", "the native action does not match the original intent");
                } else if (unit == "native_text_edit" && kind == "replace") text = (string)Wire.Object(operation.Intent["parameters"])["text"];
                else throw new Failure("invalid_request", "the native unit cannot implement this intent");
                var state = provider.Read(target, contract.Within, r); CheckState(state, contract);
                var mutate = provider.Prepare(state, unit, contract.Action, text, r);
                // Pattern acquisition may run provider callbacks. Revalidate
                // the original target after it, immediately before mutation.
                var current = provider.Read(target, contract.Within, r); CheckState(current, contract);
                r.Sending(); operation.Dispatched = true; result["dispatch"] = "unknown";
                mutate(); result["dispatch"] = "dispatched";
                result["detail"] = "Native object operation returned; read the control to verify its effect.";
                r.Check();
            } catch (Exception error) {
                var failure = error as Failure; result["errorCode"] = failure != null ? failure.Code : operation.Dispatched ? "execution_unknown" : "input_unavailable";
                result["detail"] = error.Message.Length == 0 ? "Native object operation failed." : error.Message;
                if (!operation.Dispatched && (string)result["errorCode"] == "input_unavailable") result["retryReason"] = "routeUnavailable";
            }
            return result;
        }
    }
}
