using System;
using System.Collections.Generic;
using System.Text;

namespace ComputerUse.Windows {
    // A selected keyboard paste recipe. Receiver policy stays in Python;
    // the unit cannot become typing, activation or a second paste attempt.
    internal static class NativePaste {
        internal static void Validate(string text) {
            if (Encoding.UTF8.GetByteCount(text) > 1024 * 1024 || text.IndexOf('\0') >= 0)
                throw new Failure("invalid_request", "paste requires at most 1 MiB of text without NUL");
            for (int n = 0; n < text.Length; n++) {
                if (Char.IsHighSurrogate(text[n])) { if (++n >= text.Length || !Char.IsLowSurrogate(text[n])) throw new Failure("invalid_request", "text contains an unpaired surrogate"); }
                else if (Char.IsLowSurrogate(text[n])) throw new Failure("invalid_request", "text contains an unpaired surrogate");
            }
        }
        internal static void Send(NativeKeyboard keyboard, INativeKeyboardTextProvider receiverProvider, INativeClipboardProvider clipboard,
                NativeWriteOperation operation, NativeInputContract contract, Request r, Dictionary<string, object> result, List<string> preparation) {
            var state = keyboard.Guard(contract, r);
            if (!state.Ready) throw new Failure("input_unavailable", "the selected paste recipient is not ready");
            var sequence = receiverProvider.BeginSequence(state, r);
            Action receiver = () => {
                if (!keyboard.Guard(contract, r).Ready) throw new Failure("input_unavailable", "the original paste recipient is no longer ready");
                sequence.Validate(r);
            };
            var lease = clipboard.Save(r);
            try {
                receiver(); lease.Install((string)Wire.Object(operation.Intent["parameters"])["text"], r);
                keyboard.SendBatch(operation, NativeKeys.Chord("ctrl+v", "shared"), r, result, () => {
                    // UIA validation can run application callbacks, including
                    // clipboard writes. Check our payload after those calls.
                    receiver(); lease.Validate(r);
                });
                result["detail"] = "Paste chord sent; clipboard consumption and application effect require observation.";
            } finally {
                if (lease.Prepared) preparation.Add("clipboard");
                var cleanup = lease.Finish(operation.Dispatched);
                if (cleanup == NativeClipboardRestore.Failed) operation.Cleanup = "failed";
                else if (cleanup == NativeClipboardRestore.OwnershipLost)
                    result["detail"] = (string)result["detail"] + " Newer clipboard content was preserved.";
            }
        }
    }
}
