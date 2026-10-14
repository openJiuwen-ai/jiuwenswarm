using System.Collections.Generic;
using System.Windows.Automation;

namespace ComputerUse.Windows {
    // Platform facts only. No roles, names or application classes decide what
    // is discarded or editable. Pattern defaults require a declared pattern.
    internal static class NativeReadSchema {
        internal static readonly Dictionary<string, AutomationPattern> RequiredPatterns = new Dictionary<string, AutomationPattern> {
            { "Value.Value", ValuePattern.Pattern }, { "Value.IsReadOnly", ValuePattern.Pattern },
            { "Toggle.ToggleState", TogglePattern.Pattern }, { "ExpandCollapse.ExpandCollapseState", ExpandCollapsePattern.Pattern },
            { "Window.IsModal", WindowPattern.Pattern }, { "Window.WindowInteractionState", WindowPattern.Pattern },
            { "Window.WindowVisualState", WindowPattern.Pattern }, { "Selection.CanSelectMultiple", SelectionPattern.Pattern },
            { "Selection.IsSelectionRequired", SelectionPattern.Pattern },
            { "Table.RowOrColumnMajor", TablePattern.Pattern },
            { "Scroll.HorizontallyScrollable", ScrollPattern.Pattern }, { "Scroll.VerticallyScrollable", ScrollPattern.Pattern },
            { "Scroll.HorizontalScrollPercent", ScrollPattern.Pattern }, { "Scroll.VerticalScrollPercent", ScrollPattern.Pattern },
            { "Scroll.HorizontalViewSize", ScrollPattern.Pattern }, { "Scroll.VerticalViewSize", ScrollPattern.Pattern },
            { "RangeValue.Value", RangeValuePattern.Pattern }, { "RangeValue.IsReadOnly", RangeValuePattern.Pattern },
            { "RangeValue.Minimum", RangeValuePattern.Pattern }, { "RangeValue.Maximum", RangeValuePattern.Pattern },
            { "RangeValue.LargeChange", RangeValuePattern.Pattern }, { "RangeValue.SmallChange", RangeValuePattern.Pattern },
            { "Grid.RowCount", GridPattern.Pattern }, { "Grid.ColumnCount", GridPattern.Pattern },
            { "GridItem.Row", GridItemPattern.Pattern }, { "GridItem.Column", GridItemPattern.Pattern },
            { "GridItem.RowSpan", GridItemPattern.Pattern }, { "GridItem.ColumnSpan", GridItemPattern.Pattern },
            { "GridItem.ContainingGrid", GridItemPattern.Pattern },
            { "Selection.Selection", SelectionPattern.Pattern }, { "SelectionItem.SelectionContainer", SelectionItemPattern.Pattern },
            { "SelectionItem.IsSelected", SelectionItemPattern.Pattern },
            { "Table.RowHeaders", TablePattern.Pattern }, { "Table.ColumnHeaders", TablePattern.Pattern },
            { "TableItem.RowHeaderItems", TableItemPattern.Pattern }, { "TableItem.ColumnHeaderItems", TableItemPattern.Pattern }
        };
        internal static readonly Dictionary<string, AutomationProperty> Properties = new Dictionary<string, AutomationProperty> {
            { "RuntimeId", AutomationElement.RuntimeIdProperty }, { "BoundingRectangle", AutomationElement.BoundingRectangleProperty },
            { "IsControlElement", AutomationElement.IsControlElementProperty }, { "IsContentElement", AutomationElement.IsContentElementProperty },
            { "AccessKey", AutomationElement.AccessKeyProperty }, { "AcceleratorKey", AutomationElement.AcceleratorKeyProperty },
            { "ItemType", AutomationElement.ItemTypeProperty }, { "ItemStatus", AutomationElement.ItemStatusProperty },
            { "IsRequiredForForm", AutomationElement.IsRequiredForFormProperty }, { "Orientation", AutomationElement.OrientationProperty },
            { "Window.WindowInteractionState", WindowPattern.WindowInteractionStateProperty },
            { "Window.WindowVisualState", WindowPattern.WindowVisualStateProperty },
            { "Selection.CanSelectMultiple", SelectionPattern.CanSelectMultipleProperty },
            { "Selection.IsSelectionRequired", SelectionPattern.IsSelectionRequiredProperty },
            { "Table.RowOrColumnMajor", TablePattern.RowOrColumnMajorProperty },
            { "Scroll.HorizontallyScrollable", ScrollPattern.HorizontallyScrollableProperty },
            { "Scroll.VerticallyScrollable", ScrollPattern.VerticallyScrollableProperty },
            { "Scroll.HorizontalScrollPercent", ScrollPattern.HorizontalScrollPercentProperty },
            { "Scroll.VerticalScrollPercent", ScrollPattern.VerticalScrollPercentProperty },
            { "Scroll.HorizontalViewSize", ScrollPattern.HorizontalViewSizeProperty },
            { "Scroll.VerticalViewSize", ScrollPattern.VerticalViewSizeProperty },
            { "ControlType", AutomationElement.ControlTypeProperty }, { "Name", AutomationElement.NameProperty },
            { "LocalizedControlType", AutomationElement.LocalizedControlTypeProperty }, { "AutomationId", AutomationElement.AutomationIdProperty },
            { "HelpText", AutomationElement.HelpTextProperty }, { "IsEnabled", AutomationElement.IsEnabledProperty },
            { "HasKeyboardFocus", AutomationElement.HasKeyboardFocusProperty }, { "IsKeyboardFocusable", AutomationElement.IsKeyboardFocusableProperty },
            { "IsPassword", AutomationElement.IsPasswordProperty }, { "IsOffscreen", AutomationElement.IsOffscreenProperty },
            { "ClassName", AutomationElement.ClassNameProperty }, { "FrameworkId", AutomationElement.FrameworkIdProperty },
            { "NativeWindowHandle", AutomationElement.NativeWindowHandleProperty }, { "ProcessId", AutomationElement.ProcessIdProperty },
            { "Value.Value", ValuePattern.ValueProperty }, { "Value.IsReadOnly", ValuePattern.IsReadOnlyProperty },
            { "SelectionItem.IsSelected", SelectionItemPattern.IsSelectedProperty }, { "Window.IsModal", WindowPattern.IsModalProperty },
            { "Toggle.ToggleState", TogglePattern.ToggleStateProperty }, { "ExpandCollapse.ExpandCollapseState", ExpandCollapsePattern.ExpandCollapseStateProperty },
            { "RangeValue.Value", RangeValuePattern.ValueProperty }, { "RangeValue.IsReadOnly", RangeValuePattern.IsReadOnlyProperty },
            { "RangeValue.Minimum", RangeValuePattern.MinimumProperty }, { "RangeValue.Maximum", RangeValuePattern.MaximumProperty },
            { "RangeValue.LargeChange", RangeValuePattern.LargeChangeProperty }, { "RangeValue.SmallChange", RangeValuePattern.SmallChangeProperty },
            { "Grid.RowCount", GridPattern.RowCountProperty }, { "Grid.ColumnCount", GridPattern.ColumnCountProperty },
            { "GridItem.Row", GridItemPattern.RowProperty }, { "GridItem.Column", GridItemPattern.ColumnProperty },
            { "GridItem.RowSpan", GridItemPattern.RowSpanProperty }, { "GridItem.ColumnSpan", GridItemPattern.ColumnSpanProperty }
        };
        internal static readonly Dictionary<string, AutomationProperty> TableRelations = new Dictionary<string, AutomationProperty> {
            { "Selection.Selection", SelectionPattern.SelectionProperty },
            { "SelectionItem.SelectionContainer", SelectionItemPattern.SelectionContainerProperty },
            { "GridItem.ContainingGrid", GridItemPattern.ContainingGridProperty },
            { "Table.RowHeaders", TablePattern.RowHeadersProperty }, { "Table.ColumnHeaders", TablePattern.ColumnHeadersProperty },
            { "TableItem.RowHeaderItems", TableItemPattern.RowHeaderItemsProperty }, { "TableItem.ColumnHeaderItems", TableItemPattern.ColumnHeaderItemsProperty }
        };
    }
}
