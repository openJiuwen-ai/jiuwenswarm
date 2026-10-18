using System;
using System.Collections.Generic;
using System.Text;
using System.Threading;

namespace ComputerUse.Windows {
    internal sealed class NativeClipboardData {
        internal readonly uint Format;
        internal readonly byte[] Bytes;
        internal NativeClipboardData(uint format, byte[] bytes) { Format = format; Bytes = bytes; }
    }
    internal interface INativeClipboardPayload : IDisposable { }
    // All version checks and replacements take place under the native clipboard
    // lock. Payloads allocate every required representation before any clear.
    internal interface INativeClipboardApi : IDisposable {
        IDisposable Open(Request request);
        uint Version { get; }
        IList<NativeClipboardData> Read(Request request);
        INativeClipboardPayload Prepare(IList<NativeClipboardData> entries, Request request);
        INativeClipboardPayload PrepareText(string text, Request request);
        void Replace(INativeClipboardPayload payload, Action changed, Request request);
        void Handoff();
    }
    internal interface INativeClipboardProvider { NativeClipboardTransaction Save(Request request); }
    internal enum NativeClipboardRestore { Restored, OwnershipLost, Failed }
    internal static class NativeClipboardText {
        internal static IList<NativeClipboardData> Formats(string text, uint locale, Encoding ansi, Encoding oem) {
            // CloseClipboard synthesizes missing text formats and changes the
            // sequence. Publish this complete family before recording ownership.
            return new[] { new NativeClipboardData(13, Encoding.Unicode.GetBytes(text + "\0")),
                new NativeClipboardData(1, ansi.GetBytes(text + "\0")), new NativeClipboardData(7, oem.GetBytes(text + "\0")),
                new NativeClipboardData(16, BitConverter.GetBytes(locale)) };
        }
    }
    internal sealed class NativeClipboardTransaction {
        readonly INativeClipboardApi api;
        readonly uint initial;
        readonly INativeClipboardPayload original;
        uint? owned;
        bool finished;
        bool rolledBack, rollbackFailed;
        NativeClipboardRestore finishResult;
        internal bool Prepared { get; private set; }
        internal NativeClipboardTransaction(INativeClipboardApi api, Request r) {
            this.api = api;
            try {
                IList<NativeClipboardData> entries;
                using (api.Open(r)) {
                    entries = api.Read(r);
                    // Delayed rendering may legitimately advance the version
                    // while materializing old formats under this same lock.
                    initial = api.Version;
                }
                original = api.Prepare(entries, r);
            } catch { api.Dispose(); throw; }
        }
        internal void Install(string text, Request r) {
            if (Prepared || finished) throw new Failure("stale_target", "clipboard transaction has already been used");
            NativePaste.Validate(text);
            using (var payload = api.PrepareText(text, r))
            using (api.Open(r)) {
                if (api.Version != initial) throw new Failure("input_unavailable", "clipboard changed before paste; no paste sent");
                try {
                    api.Replace(payload, () => { Prepared = true; r.Effect = true; }, r);
                    owned = api.Version;
                } catch {
                    // A partial text family would change again on close. Roll
                    // back while still holding the original lock, independently
                    // of cancellation; never adopt a later outside revision.
                    if (Prepared) {
                        try { api.Replace(original, () => { }, new Request(CancellationToken.None, 400)); rolledBack = true; }
                        catch { rollbackFailed = true; }
                    }
                    throw;
                }
            }
        }
        internal void Validate(Request r) {
            if (!Prepared || finished || rolledBack || rollbackFailed || !owned.HasValue) throw new Failure("stale_target", "clipboard payload is not available for this paste");
            using (api.Open(r)) {
                if (api.Version != owned.Value) throw new Failure("input_unavailable", "clipboard changed before the paste chord; no further input sent");
            }
        }
        internal NativeClipboardRestore Finish(bool afterInput) {
            if (finished) return finishResult;
            finished = true;
            var result = rollbackFailed ? NativeClipboardRestore.Failed : NativeClipboardRestore.Restored;
            // Cancellation does not shorten the asynchronous handoff or
            // skip resource cleanup. This is not consumption confirmation.
            try { if (Prepared && afterInput) api.Handoff(); } catch { result = NativeClipboardRestore.Failed; }
            try {
                if (Prepared && !rolledBack && !rollbackFailed) {
                    using (api.Open(new Request(CancellationToken.None, 600))) {
                        if (!owned.HasValue) result = NativeClipboardRestore.Failed;
                        else if (api.Version != owned.Value) { if (result != NativeClipboardRestore.Failed) result = NativeClipboardRestore.OwnershipLost; }
                        else api.Replace(original, () => { }, new Request(CancellationToken.None, 400));
                    }
                }
            } catch { result = NativeClipboardRestore.Failed; }
            try { original.Dispose(); } catch { result = NativeClipboardRestore.Failed; }
            try { api.Dispose(); } catch { result = NativeClipboardRestore.Failed; }
            finishResult = result; return result;
        }
    }
}
