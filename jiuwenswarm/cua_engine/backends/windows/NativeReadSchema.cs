using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using UIA = NativeUIA;

namespace ComputerUse.Windows {
    // The wire field list belongs to this driver. Numeric identifiers come
    // from the host Windows type library, imported by Build/ImportUia.cs.
    internal static class NativeReadSchema {
        static readonly Dictionary<string, int> PropertyIds = Identifiers(typeof(UIA.UIA_PropertyIds));
        static readonly Dictionary<string, int> PatternIds = Identifiers(typeof(UIA.UIA_PatternIds));
        internal static readonly Dictionary<string, int> Properties = PropertyMap(
            "RuntimeId", "BoundingRectangle", "IsControlElement", "IsContentElement", "AccessKey", "AcceleratorKey",
            "ItemType", "ItemStatus", "IsRequiredForForm", "Orientation", "Window.WindowInteractionState",
            "Window.WindowVisualState", "Selection.CanSelectMultiple", "Selection.IsSelectionRequired",
            "Table.RowOrColumnMajor", "Scroll.HorizontallyScrollable", "Scroll.VerticallyScrollable",
            "Scroll.HorizontalScrollPercent", "Scroll.VerticalScrollPercent", "Scroll.HorizontalViewSize",
            "Scroll.VerticalViewSize", "ControlType", "Name", "LocalizedControlType", "AutomationId", "HelpText",
            "IsEnabled", "HasKeyboardFocus", "IsKeyboardFocusable", "IsPassword", "IsOffscreen", "ClassName",
            "FrameworkId", "NativeWindowHandle", "ProcessId", "Value.Value", "Value.IsReadOnly",
            "SelectionItem.IsSelected", "Window.IsModal", "Toggle.ToggleState", "ExpandCollapse.ExpandCollapseState",
            "RangeValue.Value", "RangeValue.IsReadOnly", "RangeValue.Minimum", "RangeValue.Maximum",
            "RangeValue.LargeChange", "RangeValue.SmallChange", "Grid.RowCount", "Grid.ColumnCount", "GridItem.Row",
            "GridItem.Column", "GridItem.RowSpan", "GridItem.ColumnSpan");
        internal static readonly Dictionary<string, int> TableRelations = PropertyMap(
            "Selection.Selection", "SelectionItem.SelectionContainer", "GridItem.ContainingGrid", "Table.RowHeaders",
            "Table.ColumnHeaders", "TableItem.RowHeaderItems", "TableItem.ColumnHeaderItems");
        internal static readonly Dictionary<string, int> RequiredPatterns = PatternMap(Properties.Keys.Concat(TableRelations.Keys));

        static Dictionary<string, int> Identifiers(Type catalog) {
            return catalog.GetFields(BindingFlags.Public | BindingFlags.Static)
                .ToDictionary(field => field.Name, field => Convert.ToInt32(field.GetRawConstantValue()), StringComparer.Ordinal);
        }
        static Dictionary<string, int> PropertyMap(params string[] names) {
            var result = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (string name in names) {
                int id;
                // This names the SDK constant, not an element or diagnostic
                // ProgrammaticName. The explicit list above is the whitelist.
                if (PropertyIds.TryGetValue("UIA_" + name.Replace(".", "") + "PropertyId", out id)) result.Add(name, id);
            }
            return result;
        }
        static Dictionary<string, int> PatternMap(IEnumerable<string> names) {
            var result = new Dictionary<string, int>(StringComparer.Ordinal);
            foreach (string name in names.Distinct()) {
                int separator = name.IndexOf('.');
                if (separator < 0) continue;
                int id;
                // A missing pattern identifier remains a requirement that
                // cannot be met (ID 0), rather than bypassing its check.
                PatternIds.TryGetValue("UIA_" + name.Substring(0, separator) + "PatternId", out id);
                result.Add(name, id);
            }
            return result;
        }
    }
}
