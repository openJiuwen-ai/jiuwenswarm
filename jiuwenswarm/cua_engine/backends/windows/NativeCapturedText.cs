using System;
using System.Collections.Generic;

namespace ComputerUse.Windows {
    // Immutable strings belong to the driver session, not the live UIA node.
    // No continuation calls the provider again or requires a surviving window.
    internal sealed class NativeCapturedText {
        internal const int PreviewUnits = 16384;
        readonly int unitLimit, entryLimit;
        readonly Dictionary<string, string> texts = new Dictionary<string, string>();
        readonly Queue<string> order = new Queue<string>();
        int retainedUnits;

        internal sealed class Sample {
            internal readonly string Text;
            internal readonly bool Complete;
            internal Sample(string text, bool complete) { Text = text; Complete = complete; }
        }
        internal sealed class Budget {
            internal int Units, Entries;
            internal readonly int UnitLimit, EntryLimit;
            internal Budget(int units, int entries) { UnitLimit = units; EntryLimit = entries; }
            // One extra UTF-16 unit distinguishes a complete result from a
            // provider-limited prefix. Never request unbounded GetText(-1).
            internal int RequestLimit {
                get { return Math.Max(PreviewUnits + 1, Entries < EntryLimit ? UnitLimit - Units + 1 : 0); }
            }
        }
        internal NativeCapturedText(int units = 4 * 1024 * 1024, int entries = 64) {
            if (units < PreviewUnits || units > 4 * 1024 * 1024 || entries < 1 || entries > 64)
                throw new ArgumentOutOfRangeException("Invalid immutable text budget");
            unitLimit = units; entryLimit = entries;
        }
        internal Budget BeginBatch() { return new Budget(unitLimit, entryLimit); }
        static void ValidateText(string value, bool complete) {
            for (int i = 0; i < value.Length; i++) {
                if (Char.IsHighSurrogate(value[i])) {
                    if (i + 1 == value.Length && !complete) break;
                    if (++i >= value.Length || !Char.IsLowSurrogate(value[i]))
                        throw new InvalidOperationException("Native text contains an unpaired UTF-16 surrogate");
                } else if (Char.IsLowSurrogate(value[i]))
                    throw new InvalidOperationException("Native text contains an unpaired UTF-16 surrogate");
            }
        }
        internal void Observe(string field, string text, bool complete, Budget budget,
                              Dictionary<string, object> values, Dictionary<string, object> statuses) {
            if (text == null) throw new InvalidOperationException("Native text is unavailable");
            ValidateText(text, complete);
            if (complete && text.Length <= PreviewUnits) { values[field] = text; return; }
            int length = Math.Min(PreviewUnits, text.Length);
            if (length > 0 && Char.IsHighSurrogate(text[length - 1])) length--;
            values[field] = text.Substring(0, length);
            var status = Wire.Obj("state", "truncated");
            if (complete) status["totalUnits"] = text.Length;
            if (complete && text.Length <= budget.UnitLimit - budget.Units && budget.Entries < budget.EntryLimit) {
                while (order.Count > 0 && (order.Count >= entryLimit || retainedUnits + text.Length > unitLimit)) {
                    string old = order.Dequeue(); retainedUnits -= texts[old].Length; texts.Remove(old);
                }
                string reference = "text:" + Guid.NewGuid().ToString("N");
                texts.Add(reference, text); order.Enqueue(reference); retainedUnits += text.Length;
                budget.Units += text.Length; budget.Entries++;
                status["textRef"] = reference;
            } else status["detail"] = complete
                ? "Immutable text capture budget exhausted; preview only."
                : "UIA text reached the bounded provider read limit; complete length is unknown, preview only.";
            statuses[field] = status;
        }
        internal object Page(string reference, long offset, int length) {
            string text;
            if (!texts.TryGetValue(reference, out text))
                throw new Failure("snapshot_expired", "immutable text capture expired; read the original element again");
            if (offset < 0 || offset > text.Length || length < 2 || length > PreviewUnits ||
                offset < text.Length && Char.IsLowSurrogate(text[(int)offset]))
                throw new Failure("invalid_request", "text range is outside the capture or splits a UTF-16 surrogate pair");
            int start = (int)offset, end = Math.Min(text.Length, start + length);
            if (end < text.Length && Char.IsHighSurrogate(text[end - 1])) end--;
            return Wire.Obj("text", text.Substring(start, end - start), "offset", start,
                "total", text.Length, "next", end < text.Length ? (object)end : null);
        }
    }
}
