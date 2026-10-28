using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;

namespace ComputerUse.Windows {
    internal static class NativeText {
        internal static void Validate(string text) {
            if (Encoding.UTF8.GetByteCount(text) > 1024 * 1024 || text.Any(c => Char.IsControl(c) || c == '\u2028' || c == '\u2029'))
                throw new Failure("invalid_request", "typing requires at most 1 MiB of single-line text; use explicit paste for multiline content");
            for (int n = 0; n < text.Length; n++) {
                if (Char.IsHighSurrogate(text[n])) { if (++n >= text.Length || !Char.IsLowSurrogate(text[n])) throw new Failure("invalid_request", "text contains an unpaired surrogate"); }
                else if (Char.IsLowSurrogate(text[n])) throw new Failure("invalid_request", "text contains an unpaired surrogate");
            }
        }
        internal static IEnumerable<NativeKeyEvent[]> Characters(string text) {
            for (int n = 0; n < text.Length; n++) {
                var events = new List<NativeKeyEvent> { new NativeKeyEvent(text[n], false, true), new NativeKeyEvent(text[n], true, true) };
                if (Char.IsHighSurrogate(text[n])) { n++; events.Add(new NativeKeyEvent(text[n], false, true)); events.Add(new NativeKeyEvent(text[n], true, true)); }
                yield return events.ToArray();
            }
        }
    }
    internal interface INativeKeyboardSequence { void Validate(Request request); }
    internal sealed class NativeTextSelection { internal bool Empty, Full; }
    internal interface INativeKeyboardTextProvider {
        INativeKeyboardSequence BeginSequence(NativeKeyboardState state, Request request);
        NativeTextSelection Selection(NativeKeyboardState state, Request request);
        void SelectAll(NativeKeyboardState state, Request request);
    }
    // A continuous recipe for the already selected target. It does not choose
    // an editor, a route or a foreground policy, and never substitutes paste.
    internal static class NativeTypedText {
        internal static void Send(NativeKeyboard keyboard, INativeKeyboardTextProvider provider, NativeWriteOperation operation,
                NativeInputContract contract, Request r, Dictionary<string, object> result, List<string> preparation) {
            string text = (string)Wire.Object(operation.Intent["parameters"])["text"];
            var state = keyboard.Guard(contract, r);
            if (!state.Ready) throw new Failure("input_unavailable", "the selected text recipient is not ready");
            var sequence = provider.BeginSequence(state, r);
            Func<NativeKeyboardState> receiver = () => {
                var current = keyboard.Guard(contract, r);
                if (!current.Ready) throw new Failure("input_unavailable", "the original text recipient is no longer ready");
                sequence.Validate(r); return current;
            };
            Func<NativeTextSelection> selection = () => {
                var current = receiver(); var selected = provider.Selection(current, r);
                receiver(); return selected;
            };
            bool started = false; int delivered = 0, total = text.Count(c => !Char.IsLowSurrogate(c));
            Action guard = () => {
                if (contract.Replacement && !started) {
                    var selected = selection();
                    if (!selected.Empty && !selected.Full) throw new Failure("input_unavailable", "the complete control contents are no longer selected; replacement stopped");
                } else receiver();
            };
            try {
                if (contract.Replacement) {
                    var selected = selection();
                    if (!selected.Empty && !selected.Full) {
                        var current = receiver();
                        r.Sending(); operation.Dispatched = true; result["dispatch"] = "unknown";
                        preparation.Add("nativeSelection"); provider.SelectAll(current, r);
                        double until = Math.Min(r.Deadline, Wire.Now + 400);
                        do {
                            selected = selection();
                            if (selected.Empty || selected.Full) break;
                            if (Wire.Now >= until) throw new Failure("input_unavailable", "native full selection did not become observable; replacement stopped");
                            r.Wait(20);
                        } while (true);
                    }
                    guard();
                    if (text.Length == 0) {
                        selected = selection();
                        if (!selected.Empty) keyboard.SendBatch(operation, NativeKeys.Chord("BackSpace", "shared"), r, result, () => {
                            var latest = selection();
                            if (latest.Empty || !latest.Full) throw new Failure("input_unavailable", "the original full selection changed before deletion; no deletion sent");
                        });
                        else result["confirmation"] = "confirmed";
                    }
                }
                foreach (var events in NativeText.Characters(text)) {
                    keyboard.SendBatch(operation, events, r, result, guard);
                    started = true; delivered++;
                    if (delivered < total) r.Wait(10); // Yield to normal asynchronous input processing; not an acknowledgement.
                }
                result["detail"] = operation.Dispatched ? "Keyboard text recipe sent; read to verify its effect." : "The text operation needed no input.";
            } catch {
                if (delivered > 0 && delivered < total && (string)result["dispatch"] == "dispatched") result["dispatch"] = "partial";
                throw;
            }
        }
    }
}
