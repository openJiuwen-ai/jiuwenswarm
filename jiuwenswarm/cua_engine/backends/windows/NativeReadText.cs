using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Text;

namespace ComputerUse.Windows {
    // Text ranges are independent facts, not offsets inferred from Name,
    // keyboard focus, CompareEndpoints' sign, or DocumentRange.GetText.
    internal sealed class NativeReadText {
        readonly UiaTextPattern pattern;
        readonly Action check;
        readonly Action<string> trace;
        readonly Action validate;
        internal readonly string Source;
        UiaTextRange document;
        internal NativeReadText(UiaElement element, bool nativeText, bool legacyCandidate, Action check, Action<string> trace = null) {
            this.check = check; this.trace = trace; check(); object raw;
            if (nativeText && element.TryGetCurrentPattern(TextPattern.Pattern, out raw)) {
                pattern = (UiaTextPattern)raw; Source = "IUIAutomation.TextPattern";
            } else {
                // Windows' COM and managed clients have different default
                // proxies. Some legacy HWND editors expose TextPattern only
                // through the .NET proxy. Supplement text facts, never the
                // tree or actions, and only for the exact same HWND/runtime.
                int handle = legacyCandidate ? element.Current.NativeWindowHandle : 0;
                if (handle == 0) throw new NativeFieldUnavailable("unsupported", "UIA does not expose TextPattern");
                var expected = element.GetRuntimeId();
                var peer = AutomationElement.FromHandle(new IntPtr(handle)); check();
                validate = () => {
                    check();
                    if (expected.Length == 0 || !expected.SequenceEqual(element.GetRuntimeId())
                        || !expected.SequenceEqual(peer.GetRuntimeId()) || peer.Current.NativeWindowHandle != handle
                        || element.Current.NativeWindowHandle != handle || peer.Current.ProcessId != Native.PID(new IntPtr(handle)))
                        throw new NativeFieldUnavailable("error", "Legacy text proxy identity does not match the native UIA object");
                    if (peer.GetCurrentPropertyValue(AutomationElement.IsPasswordProperty, true) as bool? != false)
                        throw new NativeFieldUnavailable("unsupported", "Legacy text protection state is not readable");
                    check();
                };
                validate();
                if (!peer.TryGetCurrentPattern(TextPattern.Pattern, out raw)) throw new NativeFieldUnavailable("unsupported", "Neither native nor legacy UIA text proxy exposes TextPattern");
                pattern = new UiaTextPattern((TextPattern)raw); Source = "System.Windows.Automation.TextPattern; exact HWND/runtime read-only supplement";
            }
            check();
        }
        void Trace(string value) { if (trace != null) trace("text " + value); }
        UiaTextRange Document {
            get {
                if (document == null) { check(); document = pattern.DocumentRange; check(); }
                if (document == null) throw new NativeFieldUnavailable("absent", "TextPattern returned no document range");
                return document;
            }
        }
        internal static object ReadOnly(UiaTextRange range) {
            var value = range.GetAttributeValue(TextPattern.IsReadOnlyAttribute);
            if (ReferenceEquals(value, AutomationElement.NotSupported)) throw new NativeFieldUnavailable("unsupported");
            return ReferenceEquals(value, TextPattern.MixedAttributeValue) ? "mixed" : value;
        }
        internal object Field(string field, int limit) {
            CheckIdentity();
            var result = ReadField(field, limit);
            CheckIdentity(); return result;
        }
        void CheckIdentity() { check(); if (validate != null) validate(); }
        object ReadField(string field, int limit) {
            if (field == "Text.Source") return Source;
            if (field == "Text.DocumentRange") {
                string value = Document.GetText(limit); check();
                if (value == null) throw new NativeFieldUnavailable("absent", "UIA returned no document text");
                return new NativeCapturedText.Sample(value, value.Length < limit);
            }
            if (field == "Text.IsReadOnly") { var value = ReadOnly(Document); check(); return value; }
            if (field == "Text.SupportedTextSelection") { var value = pattern.SupportedTextSelection.ToString(); check(); return value; }
            if (field == "Text.Selection" || field == "Text.SelectionBounds") {
                Trace("GetSelection"); return Ranges(pattern.GetSelection(), field == "Text.SelectionBounds");
            }
            if (field == "Text.VisibleRanges") { Trace("GetVisibleRanges"); return Ranges(pattern.GetVisibleRanges(), true); }
            throw new NativeFieldUnavailable("unsupported", "unknown TextPattern field");
        }
        object Ranges(UiaTextRange[] ranges, bool geometry) {
            check();
            if (ranges == null) throw new NativeFieldUnavailable("absent", "TextPattern returned no range array");
            var result = new List<object>(); int textUnits = 0;
            const int maxRanges = 16, totalTextUnits = 16384;
            for (int i = 0; i < ranges.Length && i < maxRanges; i++) {
                check(); var range = ranges[i]; var values = Wire.Obj(); var status = Wire.Obj();
                try {
                    int limit = Math.Max(0, totalTextUnits - textUnits);
                    Trace("range " + i + " GetText " + limit);
                    string text = range.GetText(limit + 1); check();
                    if (text == null) throw new NativeFieldUnavailable("absent", "UIA returned no range text");
                    int end = Math.Min(text.Length, limit);
                    if (end > 0 && Char.IsHighSurrogate(text[end - 1])) end--;
                    values["text"] = text.Substring(0, end); textUnits += end;
                    if (text.Length > limit) status["text"] = Wire.Obj("state", "truncated", "detail", "Shared range text budget exhausted; this is not the complete range.");
                } catch (Exception error) { check(); status["text"] = NativeReadBackend.Status(error); }
                try { check(); Trace("range " + i + " CompareEndpoints"); values["degenerate"] = range.CompareEndpoints(TextPatternRangeEndpoint.Start, range, TextPatternRangeEndpoint.End) == 0; check(); }
                catch (Exception error) { check(); status["degenerate"] = NativeReadBackend.Status(error); }
                try { check(); Trace("range " + i + " IsReadOnly"); values["readOnly"] = ReadOnly(range); check(); }
                catch (Exception error) { check(); status["readOnly"] = NativeReadBackend.Status(error); }
                // Geometry is an explicit probe. Some client-side providers
                // block here even while selection text remains available.
                // A normal tree read must not request it for every editor.
                if (geometry) try {
                    check(); Trace("range " + i + " GetBoundingRectangles"); var rects = range.GetBoundingRectangles(); check();
                    var boxes = new List<object>();
                    foreach (var rect in rects) { if (boxes.Count == 64) break; boxes.Add(Bounds(rect)); }
                    values["bounds"] = boxes;
                    if (rects.Length > 64) status["bounds"] = Wire.Obj("state", "truncated", "detail", "At most 64 native rectangles per range.");
                } catch (Exception error) { check(); status["bounds"] = NativeReadBackend.Status(error); }
                result.Add(Wire.Obj("values", values, "fieldStatus", status));
            }
            var output = Wire.Obj("ranges", result, "total", ranges.Length, "state", result.Count == ranges.Length ? "complete" : "partial");
            if (geometry) output["coordinateSpace"] = "screenPhysicalPixels";
            return output;
        }
        internal UiaElement[] Children() { CheckIdentity(); var result = Document.GetChildren(); CheckIdentity(); return result; }
        internal static object Bounds(Rect rect) {
            if (rect.IsEmpty) return null;
            if (Double.IsNaN(rect.X) || Double.IsNaN(rect.Y) || Double.IsNaN(rect.Width) || Double.IsNaN(rect.Height)
                || Double.IsInfinity(rect.X) || Double.IsInfinity(rect.Y) || Double.IsInfinity(rect.Width) || Double.IsInfinity(rect.Height))
                throw new NativeFieldUnavailable("error", "UIA rectangle contains a non-finite coordinate");
            return Wire.Obj("x", rect.X, "y", rect.Y, "width", rect.Width, "height", rect.Height);
        }
    }
}
