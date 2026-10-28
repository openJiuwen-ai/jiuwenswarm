using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Text;
using UIA = NativeUIA;

namespace ComputerUse.Windows {
    // The tree and retained input objects use the native COM client. Managed
    // traversal can omit cross-process hosted fragments. The read layer may
    // supplement legacy HWND text through a separately identity-checked proxy.
    // Callers run on the driver's windowless MTA threads, retained for the
    // worker lifetime. The existing process supervisor bounds blocked providers.
    internal static class UiaClient {
        [ThreadStatic] static UIA.IUIAutomation client;
        internal static UIA.IUIAutomation Current {
            get { return client ?? (client = (UIA.IUIAutomation)Activator.CreateInstance(
                typeof(UIA.IUIAutomation).Assembly.GetType("NativeUIA.CUIAutomation8Class", true))); }
        }
        internal sealed class Pattern {
            internal int Id, Available;
            internal string ProgrammaticName;
        }
        internal static readonly Pattern[] Patterns = PatternIds();
        // LookupById is inherited from AutomationIdentifier and does not
        // initialize ControlType's registrations when no managed UIA element
        // has been used. Build the map from the public constants explicitly.
        static readonly Dictionary<int, ControlType> ControlTypes = typeof(ControlType)
            .GetFields(BindingFlags.Public | BindingFlags.Static)
            .Where(f => f.FieldType == typeof(ControlType))
            .Select(f => (ControlType)f.GetValue(null)).ToDictionary(t => t.Id);
        static Pattern[] PatternIds() {
            var ids = typeof(UIA.UIA_PatternIds).GetFields(BindingFlags.Public | BindingFlags.Static)
                .ToDictionary(f => f.Name, f => Convert.ToInt32(f.GetRawConstantValue()));
            var result = new List<Pattern>();
            foreach (var f in typeof(UIA.UIA_PropertyIds).GetFields(BindingFlags.Public | BindingFlags.Static)) {
                const string start = "UIA_Is", end = "PatternAvailablePropertyId";
                if (!f.Name.StartsWith(start) || !f.Name.EndsWith(end)) continue;
                string name = f.Name.Substring(start.Length, f.Name.Length - start.Length - end.Length); int id;
                if (ids.TryGetValue("UIA_" + name + "PatternId", out id)) result.Add(new Pattern {
                    Id = id, Available = Convert.ToInt32(f.GetRawConstantValue()), ProgrammaticName = name + "PatternIdentifiers.Pattern" });
            }
            return result.OrderBy(p => p.Id).ToArray();
        }
        internal static T Call<T>(Func<T> read) {
            try { return read(); }
            catch (COMException error) {
                if ((uint)error.HResult == 0x80040201) throw new ElementNotAvailableException(error.Message);
                throw;
            }
        }
        internal static UiaElement Wrap(UIA.IUIAutomationElement e) { return e == null ? null : new UiaElement(e); }
        internal static UiaElement Parent(UiaElement e) { return Wrap(Call(() => Current.RawViewWalker.GetParentElement(e.Native))); }
        internal static UiaElement FirstChild(UiaElement e) { return Wrap(Call(() => Current.RawViewWalker.GetFirstChildElement(e.Native))); }
        internal static UiaElement NextSibling(UiaElement e) { return Wrap(Call(() => Current.RawViewWalker.GetNextSiblingElement(e.Native))); }
        internal static UiaElement[] Elements(UIA.IUIAutomationElementArray array) {
            if (array == null) return new UiaElement[0];
            return Enumerable.Range(0, array.Length).Select(i => Wrap(array.GetElement(i))).ToArray();
        }
        internal static bool SameCom(object a, object b) {
            if (a == null || b == null || !Marshal.IsComObject(a) || !Marshal.IsComObject(b)) return false;
            IntPtr x = Marshal.GetIUnknownForObject(a), y = IntPtr.Zero;
            try { y = Marshal.GetIUnknownForObject(b); return x == y; }
            finally { Marshal.Release(x); if (y != IntPtr.Zero) Marshal.Release(y); }
        }
        internal static object Value(object value, int property = 0) {
            if (value == null) return null;
            if (Marshal.IsComObject(value)) {
                if (SameCom(value, Current.ReservedNotSupportedValue)) return AutomationElement.NotSupported;
                if (SameCom(value, Current.ReservedMixedAttributeValue)) return TextPattern.MixedAttributeValue;
                var element = value as UIA.IUIAutomationElement;
                if (element != null) return Wrap(element);
                var elements = value as UIA.IUIAutomationElementArray;
                if (elements != null) return Elements(elements);
            }
            if (property == AutomationElement.ControlTypeProperty.Id) {
                int id = Convert.ToInt32(value); ControlType type;
                return ControlTypes.TryGetValue(id, out type) ? (object)type : "ControlType." + id;
            }
            if (property == AutomationElement.BoundingRectangleProperty.Id) {
                var box = (value as Array);
                if (box == null || box.Length != 4) throw new InvalidOperationException("Invalid UIA bounding rectangle");
                var v = box.Cast<object>().Select(Convert.ToDouble).ToArray();
                return new Rect(v[0], v[1], v[2], v[3]);
            }
            if (property == TogglePattern.ToggleStateProperty.Id) return (ToggleState)Convert.ToInt32(value);
            if (property == ExpandCollapsePattern.ExpandCollapseStateProperty.Id) return (ExpandCollapseState)Convert.ToInt32(value);
            if (property == WindowPattern.WindowInteractionStateProperty.Id) return (WindowInteractionState)Convert.ToInt32(value);
            if (property == WindowPattern.WindowVisualStateProperty.Id) return (WindowVisualState)Convert.ToInt32(value);
            if (property == AutomationElement.OrientationProperty.Id) return (OrientationType)Convert.ToInt32(value);
            if (property == TablePattern.RowOrColumnMajorProperty.Id) return (RowOrColumnMajor)Convert.ToInt32(value);
            var array = value as Array;
            if (array != null && array.Cast<object>().All(item => item is UIA.IUIAutomationElement))
                return array.Cast<UIA.IUIAutomationElement>().Select(Wrap).ToArray();
            return value;
        }
        internal static UIA.IUIAutomationCacheRequest Cache(IEnumerable<int> fields, bool children = false) {
            var cache = Current.CreateCacheRequest();
            cache.TreeScope = (UIA.TreeScope)(children ? 2 : 1);
            cache.TreeFilter = Current.RawViewCondition;
            cache.AutomationElementMode = (UIA.AutomationElementMode)1; // Full: input uses the retained native object.
            foreach (int field in fields.Distinct()) cache.AddProperty(field);
            return cache;
        }
    }
    internal sealed class UiaElement {
        internal readonly UIA.IUIAutomationElement Native;
        internal UiaElement(UIA.IUIAutomationElement native) { Native = native; }
        internal static UiaElement FromHandle(IntPtr h) { return UiaClient.Wrap(UiaClient.Call(() => UiaClient.Current.ElementFromHandle(h))); }
        internal static UiaElement RootElement { get { return UiaClient.Wrap(UiaClient.Current.GetRootElement()); } }
        internal static UiaElement FocusedElement { get { return UiaClient.Wrap(UiaClient.Call(() => UiaClient.Current.GetFocusedElement())); } }
        internal int[] GetRuntimeId() {
            var value = UiaClient.Call(() => Native.GetRuntimeId());
            return value == null ? new int[0] : value.Cast<object>().Select(Convert.ToInt32).ToArray();
        }
        internal object GetCurrentPropertyValue(AutomationProperty field, bool ignoreDefault = false) {
            return GetCurrentPropertyValue(field.Id, ignoreDefault);
        }
        internal object GetCurrentPropertyValue(int field, bool ignoreDefault = false) {
            return UiaClient.Value(UiaClient.Call(() => Native.GetCurrentPropertyValueEx(field, ignoreDefault ? 1 : 0)), field);
        }
        internal object GetCachedPropertyValue(AutomationProperty field, bool ignoreDefault = false) {
            return GetCachedPropertyValue(field.Id, ignoreDefault);
        }
        internal object GetCachedPropertyValue(int field, bool ignoreDefault = false) {
            return UiaClient.Value(UiaClient.Call(() => Native.GetCachedPropertyValueEx(field, ignoreDefault ? 1 : 0)), field);
        }
        internal UiaElement Snapshot(UIA.IUIAutomationCacheRequest cache) { return UiaClient.Wrap(UiaClient.Call(() => Native.BuildUpdatedCache(cache))); }
        internal UiaElement[] CachedChildren { get { return UiaClient.Elements(UiaClient.Call(() => Native.GetCachedChildren())); } }
        internal UiaClient.Pattern[] GetSupportedPatterns(bool cached = false) {
            var source = cached ? this : Snapshot(UiaClient.Cache(UiaClient.Patterns.Select(p => p.Available)));
            return UiaClient.Patterns.Where(p => UiaClient.Call(() => source.Native.GetCachedPropertyValueEx(p.Available, 1)) as bool? == true).ToArray();
        }
        internal bool TryGetCurrentPattern(AutomationPattern pattern, out object result) {
            try { result = UiaClient.Call(() => Native.GetCurrentPattern(pattern.Id)); }
            catch (COMException error) {
                if ((uint)error.HResult != 0x80040204) throw; // UIA_E_NOTSUPPORTED
                result = null;
            }
            if (result == null) return false;
            if (pattern == TextPattern.Pattern) result = new UiaTextPattern((UIA.IUIAutomationTextPattern)result);
            return true;
        }
        internal void SetFocus() { UiaClient.Call(() => { Native.SetFocus(); return true; }); }
        internal Info Current { get { return new Info(this); } }
        internal sealed class Info {
            readonly UiaElement e;
            internal Info(UiaElement element) { e = element; }
            internal int NativeWindowHandle { get { return Convert.ToInt32(e.GetCurrentPropertyValue(AutomationElement.NativeWindowHandleProperty)); } }
            internal string Name { get { return (string)e.GetCurrentPropertyValue(AutomationElement.NameProperty); } }
        }
    }
    // Text helpers translate COM arrays/endpoints without changing their
    // meaning. Native text ranges remain objects, never inferred offsets.
    internal sealed class UiaTextPattern {
        readonly UIA.IUIAutomationTextPattern pattern;
        readonly TextPattern legacy;
        internal UiaTextPattern(UIA.IUIAutomationTextPattern value) { pattern = value; }
        internal UiaTextPattern(TextPattern value) { legacy = value; }
        internal UiaTextRange DocumentRange { get {
            if (legacy != null) { var value = legacy.DocumentRange; return value == null ? null : new UiaTextRange(value); }
            var range = pattern.DocumentRange; return range == null ? null : new UiaTextRange(range);
        } }
        internal SupportedTextSelection SupportedTextSelection { get { return legacy == null ? (SupportedTextSelection)pattern.SupportedTextSelection : legacy.SupportedTextSelection; } }
        static UiaTextRange[] Ranges(UIA.IUIAutomationTextRangeArray values) {
            if (values == null) return null;
            return Enumerable.Range(0, values.Length).Select(i => new UiaTextRange(values.GetElement(i))).ToArray();
        }
        static UiaTextRange[] Ranges(TextPatternRange[] values) { return values == null ? null : values.Select(r => new UiaTextRange(r)).ToArray(); }
        internal UiaTextRange[] GetSelection() { return legacy == null ? Ranges(pattern.GetSelection()) : Ranges(legacy.GetSelection()); }
        internal UiaTextRange[] GetVisibleRanges() { return legacy == null ? Ranges(pattern.GetVisibleRanges()) : Ranges(legacy.GetVisibleRanges()); }
    }
    internal sealed class UiaTextRange {
        readonly UIA.IUIAutomationTextRange range;
        readonly TextPatternRange legacy;
        internal UiaTextRange(UIA.IUIAutomationTextRange value) { range = value; }
        internal UiaTextRange(TextPatternRange value) { legacy = value; }
        internal string GetText(int limit) { return legacy == null ? range.GetText(limit) : legacy.GetText(limit); }
        internal object GetAttributeValue(AutomationTextAttribute field) { return legacy == null ? UiaClient.Value(range.GetAttributeValue(field.Id)) : legacy.GetAttributeValue(field); }
        internal int CompareEndpoints(TextPatternRangeEndpoint a, UiaTextRange other, TextPatternRangeEndpoint b) {
            if (legacy != null) return legacy.CompareEndpoints(a, other.legacy, b);
            return range.CompareEndpoints((UIA.TextPatternRangeEndpoint)a, other.range, (UIA.TextPatternRangeEndpoint)b);
        }
        internal Rect[] GetBoundingRectangles() {
            if (legacy != null) return legacy.GetBoundingRectangles();
            var raw = range.GetBoundingRectangles();
            if (raw == null || raw.Length % 4 != 0) throw new InvalidOperationException("Invalid UIA text rectangle array");
            var values = raw.Cast<object>().Select(Convert.ToDouble).ToArray();
            return Enumerable.Range(0, values.Length / 4).Select(i => new Rect(values[i*4], values[i*4+1], values[i*4+2], values[i*4+3])).ToArray();
        }
        internal UiaElement[] GetChildren() {
            if (legacy == null) return UiaClient.Elements(range.GetChildren());
            // A legacy text supplement grants no native object/input identity.
            // Do not merge unrelated client objects by labels or coordinates.
            if (legacy.GetChildren().Length != 0) throw new NativeFieldUnavailable("unsupported", "Legacy text proxy exposes embedded objects without native COM identities; use RawViewChildren.");
            return new UiaElement[0];
        }
        internal void Select() {
            if (legacy != null) throw new InvalidOperationException("The legacy text supplement is read-only");
            range.Select();
        }
    }
}
