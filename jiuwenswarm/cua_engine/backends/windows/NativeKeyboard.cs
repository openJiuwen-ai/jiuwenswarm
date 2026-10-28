using System;
using System.Collections.Generic;
using System.Linq;

namespace ComputerUse.Windows {
    internal struct NativeKeyEvent {
        internal ushort Code;
        internal bool Up, Extended, Unicode;
        internal NativeKeyEvent(ushort code, bool up, bool unicode = false) { Code = code; Up = up; Unicode = unicode; Extended = !unicode && (code >= 33 && code <= 46 || code == 91); }
    }
    internal static class NativeKeys {
        static readonly Dictionary<string, ushort> Known = new Dictionary<string, ushort> {
            { "Return", 13 }, { "Tab", 9 }, { "Escape", 27 }, { "BackSpace", 8 }, { "Delete", 46 }, { "space", 32 },
            { "Left", 37 }, { "Right", 39 }, { "Up", 38 }, { "Down", 40 }, { "Home", 36 }, { "End", 35 }, { "Page_Up", 33 }, { "Page_Down", 34 }
        };
        // Validate the entire chord before any focus preparation or key down.
        internal static NativeKeyEvent[] Chord(string key, string mode) {
            if (key.Length > 128) throw new Failure("invalid_request", "key chord is too long");
            var parts = key.Split('+'); string last = parts.Last(); ushort code;
            if (key == "super") code = 91;
            else if (!Known.TryGetValue(last, out code)) {
                int number;
                if (last.Length == 1 && (last[0] >= 'a' && last[0] <= 'z' || last[0] >= '0' && last[0] <= '9')) code = (ushort)Char.ToUpperInvariant(last[0]);
                else if (last.StartsWith("F") && Int32.TryParse(last.Substring(1), out number) && number >= 1 && number <= 12 && last == "F" + number) code = (ushort)(111 + number);
                else throw new Failure("invalid_request", "unsupported key " + last);
            }
            var modifiers = new List<ushort>();
            foreach (string part in parts.Take(parts.Length - 1)) {
                ushort modifier = part == "ctrl" ? (ushort)17 : part == "shift" ? (ushort)16 : part == "alt" ? (ushort)18 : part == "super" ? (ushort)91 : (ushort)0;
                if (modifier == 0 || modifiers.Contains(modifier)) throw new Failure("invalid_request", "unsupported or duplicate modifier");
                modifiers.Add(modifier);
            }
            if (mode == "shared" && (code == 91 || modifiers.Contains(91) || modifiers.Contains(18) && new ushort[] { 9, 27 }.Contains(code) || modifiers.Contains(17) && code == 27))
                throw new Failure("mode_unsupported", "system-wide shortcut requires exclusive mode");
            return modifiers.Select(m => new NativeKeyEvent(m, false)).Concat(new[] { new NativeKeyEvent(code, false), new NativeKeyEvent(code, true) })
                .Concat(modifiers.AsEnumerable().Reverse().Select(m => new NativeKeyEvent(m, true))).ToArray();
        }
    }
    internal sealed class NativeKeyboardState {
        internal object Native;
        internal string Role, Label;
        internal bool? Enabled, Focusable, TextWritable;
        internal bool TextSelection;
        internal bool Foreground, WindowRoute, Focused;
        internal bool Ready { get { return Enabled == true && Foreground && WindowRoute && Focused; } }
    }
    internal interface INativeKeyboardProvider {
        NativeKeyboardState Read(Dictionary<string, object> target, string[] within, Request request, bool text = false);
        void RequireIdle(NativeKeyEvent[] events, Request request);
        void Activate(NativeKeyboardState state, Request request);
        void Focus(NativeKeyboardState state, Request request);
        // No request/deadline argument: paired releases must survive cancellation
        // and target destruction. Return the native accepted-event count.
        int Send(NativeKeyEvent[] events);
    }
    // Declared keyboard units. No native fallback policy, target
    // search, activation during dispatch, or restoration of previous foreground.
    internal sealed class NativeKeyboard {
        readonly INativeKeyboardProvider provider;
        readonly string mode;
        readonly bool foregroundAllowed;
        readonly INativeKeyboardTextProvider textProvider;
        readonly INativeClipboardProvider clipboard;
        internal bool SupportsText { get { return textProvider != null; } }
        internal bool SupportsPaste { get { return SupportsText && clipboard != null; } }
        internal NativeKeyboard(INativeKeyboardProvider provider, string mode, bool foregroundAssist, INativeClipboardProvider clipboard = null) {
            if (mode != "shared" && mode != "exclusive") throw new ArgumentException("invalid desktop mode");
            this.provider = provider; this.mode = mode; foregroundAllowed = mode == "exclusive" || foregroundAssist;
            textProvider = provider as INativeKeyboardTextProvider;
            this.clipboard = clipboard;
        }
        internal void ValidateIntent(Dictionary<string, object> intent) {
            string kind = (string)intent["kind"]; var p = Wire.Object(intent["parameters"]);
            if (kind == "focus") Wire.Keys(p, "");
            else if (kind == "key") { Wire.Keys(p, "key"); NativeKeys.Chord(Wire.Str(p, "key"), mode); }
            else if (SupportsText && (kind == "insert" || kind == "replace")) { Wire.Keys(p, "text"); NativeText.Validate(Wire.Str(p, "text", true)); }
            else if (SupportsPaste && kind == "paste") { Wire.Keys(p, "text"); NativePaste.Validate(Wire.Str(p, "text", true)); }
            else throw new Failure("mode_unsupported", "this keyboard unit supports focus and key intents");
        }
        static string Unit(NativeWriteOperation operation) {
            string kind = (string)operation.Intent["kind"];
            return kind == "focus" ? "prepare_receiver" : kind == "paste" ? "clipboard_paste" : "key_sequence";
        }
        internal object Inspect(NativeWriteOperation operation, Dictionary<string, object> target, string[] within, Request r) {
            ValidateIntent(operation.Intent); operation.Bind(target, within);
            bool text = new[] { "insert", "replace", "paste" }.Contains((string)operation.Intent["kind"]);
            bool replacement = (string)operation.Intent["kind"] == "replace";
            var state = provider.Read(target, within, r, text); r.Check();
            bool supported = target["node"] == null || state.Focusable == true || state.Focused;
            if (text) supported = supported && (target["node"] == null || state.TextWritable == true);
            if (replacement) supported = supported && target["node"] != null && state.TextSelection;
            var contract = new NativeInputContract { Id = "primitive:" + Guid.NewGuid().ToString("N"), Unit = Unit(operation),
                Target = new Dictionary<string, object>(target), Within = (string[])within.Clone(), Role = state.Role, Supported = supported, Text = text, Replacement = replacement };
            operation.Contracts.Add(contract.Id, contract);
            var pending = new List<string>();
            if (state.Enabled != true) pending.Add("enabled");
            if (!state.Foreground) pending.Add("foreground");
            if (!state.WindowRoute || !state.Focused) pending.Add("receiver");
            return Wire.Obj("kind", "inputContext", "binding", new Dictionary<string, object>(target), "observed", "observation:" + Guid.NewGuid().ToString("N"),
                "targetLabel", String.IsNullOrWhiteSpace(state.Label) ? "Native keyboard target" : state.Label, "identity", "yes", "withinScope", "yes",
                "enabled", state.Enabled == true ? "yes" : state.Enabled == false ? "no" : "unknown", "blockedBy", new string[0],
                "nativeRole", state.Role, "actionsComplete", false, "within", within,
                "primitives", new[] { Wire.Obj("id", contract.Id, "unit", contract.Unit, "channel", "keyboard", "supported", supported ? "yes" : "no",
                    "readiness", supported && state.Ready ? "yes" : "no", "pending", pending, "backgroundPreparation", state.Foreground && state.Focusable == true ? "yes" : "no") },
                "input", Wire.Obj("foregroundSurface", state.Foreground ? target["surface"] : null, "keyboardSurface", state.WindowRoute ? target["surface"] : null,
                    "focusedNode", state.Focused ? target["node"] : null, "focusAncestors", new string[0], "editor", null, "editorOwner", null,
                    "selectionKnown", "unknown", "geometryCurrent", "unknown", "windowRoute", state.WindowRoute ? "yes" : "no"));
        }
        internal NativeKeyboardState Guard(NativeInputContract contract, Request r) {
            var state = provider.Read(contract.Target, contract.Within, r, contract.Text); r.Check();
            if (state.Role != contract.Role || state.Enabled != true) throw new Failure("input_unavailable", "the original keyboard target role or enabled state changed");
            if (contract.Text && contract.Target["node"] != null && state.TextWritable != true || contract.Replacement && !state.TextSelection)
                throw new Failure("input_unavailable", "the selected control no longer declares the required text capability");
            return state;
        }
        internal void SendBatch(NativeWriteOperation operation, NativeKeyEvent[] events, Request r, Dictionary<string, object> result, Action guard) {
            provider.RequireIdle(events, r); guard();
            r.Sending(); operation.Dispatched = true; result["dispatch"] = "unknown";
            bool complete = false;
            try {
                int sent = provider.Send(events);
                if (sent != events.Length) throw new Failure("execution_unknown", "SendInput accepted " + sent + "/" + events.Length + " events; input was not replayed");
                complete = true; result["dispatch"] = "dispatched";
            } finally {
                if (!complete) {
                    var releases = events.Where(e => !e.Up).Reverse().Select(e => new NativeKeyEvent(e.Code, true, e.Unicode)).ToArray();
                    try { if (provider.Send(releases) != releases.Length) operation.Cleanup = "failed"; }
                    catch { operation.Cleanup = "failed"; }
                }
            }
        }
        bool Prepare(NativeInputContract contract, bool foreground, Request r, List<string> preparation) {
            var state = Guard(contract, r);
            if (state.Ready) return true;
            if (!state.Foreground) {
                if (!foreground) return false;
                if (!foregroundAllowed) throw new Failure("mode_unsupported", "host policy does not permit foreground assistance");
                provider.RequireIdle(new NativeKeyEvent[0], r);
                r.Check(); preparation.Add("foreground"); r.Effect = true;
                provider.Activate(state, r);
                var until = Math.Min(r.Deadline, Wire.Now + 400);
                while (!(state = Guard(contract, r)).Foreground && Wire.Now < until) r.Wait(20);
                if (!state.Foreground) return false;
            }
            if (contract.Target["node"] != null && !state.Ready && state.Focusable == true) {
                provider.RequireIdle(new NativeKeyEvent[0], r);
                // Idle/provider callbacks may change focus and identity too.
                state = Guard(contract, r);
                if (!state.Foreground) return false;
                if (!state.Ready && state.Focusable == true) {
                    r.Check(); preparation.Add("nativeFocus"); r.Effect = true; provider.Focus(state, r);
                }
            }
            double deadline = Math.Min(r.Deadline, Wire.Now + 400);
            while (true) {
                state = Guard(contract, r);
                if (state.Ready) return true;
                if (Wire.Now >= deadline) return false;
                r.Wait(20);
            }
        }
        internal object Execute(NativeWriteOperation operation, Dictionary<string, object> command, Request r) {
            var target = Wire.Object(command["target"]); var preparation = new List<string>();
            var result = Wire.Obj("target", new Dictionary<string, object>(target), "dispatch", "notDispatched", "detail", "No requested keyboard input dispatched.",
                "preparation", preparation, "confirmation", "unavailable", "cleanup", "complete", "errorCode", null, "retryReason", null, "context", null);
            try {
                NativeInputContract contract;
                if (!operation.Contracts.TryGetValue((string)command["primitiveId"], out contract) || !contract.Supported ||
                    contract.Unit != Unit(operation) || !NativeWriteOperation.Same(contract.Target, target))
                    throw new Failure("stale_target", "the keyboard contract expired or belongs to another target");
                bool preparing = (string)command["kind"] == "prepare_receiver";
                if (!preparing && ((string)command["kind"] != contract.Unit || contract.Unit == "prepare_receiver" || (bool)command["foreground"]))
                    throw new Failure("invalid_request", "keyboard dispatch cannot activate a window or change its unit");
                ValidateIntent(operation.Intent);
                if (contract.Unit == "clipboard_paste" && ((string)Wire.Object(operation.Intent["parameters"])["text"]).Length == 0) {
                    Guard(contract, r); result["confirmation"] = "confirmed";
                    result["detail"] = "Empty paste; no input, clipboard change or focus preparation was needed.";
                } else if (preparing) {
                    if (Prepare(contract, (bool)command["foreground"], r, preparation)) {
                        result["confirmation"] = "confirmed"; result["detail"] = "The selected receiver is ready for keyboard input.";
                    } else {
                        result["errorCode"] = "input_unavailable"; result["retryReason"] = "preparationUnavailable";
                        result["detail"] = "Preparation did not establish the selected keyboard receiver; no requested key was sent.";
                    }
                } else {
                    if (contract.Unit == "clipboard_paste") NativePaste.Send(this, textProvider, clipboard, operation, contract, r, result, preparation);
                    else if (contract.Text) NativeTypedText.Send(this, textProvider, operation, contract, r, result, preparation);
                    else {
                        var events = NativeKeys.Chord((string)Wire.Object(operation.Intent["parameters"])["key"], mode);
                        SendBatch(operation, events, r, result, () => {
                            if (!Guard(contract, r).Ready) throw new Failure("input_unavailable", "the selected control or working window is not the keyboard receiver");
                        });
                        result["detail"] = "Key sequence sent; read to verify its effect.";
                    }
                    r.Check(); // Return/Escape may have legitimately closed the target.
                }
            } catch (Exception error) {
                var failure = error as Failure; result["errorCode"] = failure != null ? failure.Code : operation.Dispatched ? "execution_unknown" : "input_unavailable";
                result["detail"] = String.IsNullOrWhiteSpace(error.Message) ? "Native keyboard operation failed." : error.Message;
                if (!operation.Dispatched && (string)result["errorCode"] == "input_unavailable")
                    result["retryReason"] = (string)command["kind"] == "prepare_receiver" ? "preparationUnavailable" : "routeUnavailable";
            }
            result["cleanup"] = operation.Cleanup;
            if (operation.Cleanup != "complete") {
                result["retryReason"] = null;
                result["errorCode"] = result["errorCode"] ?? "execution_unknown";
                result["detail"] = (string)result["detail"] + " Input or clipboard cleanup could not be confirmed; reconnect before more input.";
            }
            return result;
        }
    }
}
