"""Pure native fact and control semantics shared by reading and execution.

Names describe data; no role alone grants an input route. Unknown roles remain
available in NativeFacts and are shown by their original name.
"""
import re
from collections.abc import Collection

# Missing/failed observations differ from a known absent or unsupported field.
# This sentinel stays inside Python; it never enters a wire or display value.
UNKNOWN = object()
TEXT_FIELD_ROLE = "textField"
PROTECTED_FIELD = "protected"
BOOLEAN_FIELDS = frozenset(('focused', 'modal', 'enabled', 'sensitive', 'busy', 'editable', 'readOnly', 'hasPopup',
                            'focusWritable', 'valueWritable', 'textWritable', 'textReadOnly'))
EDITING_FIELDS = ('enabled', 'sensitive', 'editable', 'readOnly', 'textReadOnly', 'focusWritable', 'valueWritable',
                  'textWritable', 'interfaces')
SCALAR_EDITORS = frozenset(('textField', 'comboBox', 'incrementor'))
EDITOR_BOUNDARIES = SCALAR_EDITORS | {'textArea'}
DOCUMENT_EDITORS = frozenset(('textArea', 'webArea', 'document'))
EDITOR_ROLES = EDITOR_BOUNDARIES | DOCUMENT_EDITORS | {'unknown'}
LAYOUT_ROLES = frozenset(('group', 'unknown'))
COMPOSITE_OWNERS = EDITOR_BOUNDARIES | LAYOUT_ROLES | {'cell'}
# A visual member role does not itself authorize input. Projection preserves
# independent information; pointer selection additionally checks native actions
# and editing/focus declarations, then binds the exact observed hit object.
CONTROL_LABEL_ROLES = frozenset(('staticText', 'heading'))
CONTROL_LAYOUT_ROLES = frozenset(('group', 'unknown', 'cell'))
TABLE_FIELDS = frozenset(('rowRange', 'columnRange', 'rowCount', 'columnCount', 'index', 'cellPosition',
                          'selectedChildCount', 'cellRow', 'cellColumn', 'cellRowSpan', 'cellColumnSpan'))
POINTER_CONTROL_OWNERS = frozenset(
    ('button', 'checkBox', 'radioButton', 'switch', 'toggle', 'popUpButton', 'menuButton', 'tab', 'link'))


def control_member_role(role):
    if role in CONTROL_LABEL_ROLES:
        return 'label'
    if role == 'image':
        return 'icon'
    if role in CONTROL_LAYOUT_ROLES:
        return 'layout'
    return None


LINUX_ROLES = {
    "frame": "window",
    "dialog": "window",
    "window": "window",
    "alert": "window",
    "file chooser": "window",
    "font chooser": "window",
    "color chooser": "window",
    "application": "application",
    "push button": "button",
    "button": "button",
    "push button menu": "menuButton",
    "toggle button": "checkBox",
    "check box": "checkBox",
    "radio button": "radioButton",
    "text": TEXT_FIELD_ROLE,
    "entry": TEXT_FIELD_ROLE,
    "password text": TEXT_FIELD_ROLE,
    "label": "staticText",
    "static": "staticText",
    "heading": "heading",
    "document web": "webArea",
    "document text": "document",
    "document spreadsheet": "document",
    "document frame": "document",
    "document presentation": "document",
    "table": "table",
    "table cell": "cell",
    "table row": "row",
    "table column header": "column",
    "table row header": "row",
    "list": "list",
    "list box": "list",
    "list item": "row",
    "tree": "outline",
    "tree table": "outline",
    "tree item": "row",
    "scroll pane": "scrollArea",
    "scroll bar": "scrollBar",
    "slider": "slider",
    "spin button": "incrementor",
    "combo box": "comboBox",
    "menu": "menu",
    "popup menu": "menu",
    "menu bar": "menuBar",
    "menu item": "menuItem",
    "check menu item": "menuItem",
    "radio menu item": "menuItem",
    "tool bar": "toolbar",
    "page tab list": "tabGroup",
    "page tab": "tab",
    "separator": "splitter",
    "image": "image",
    "icon": "image",
    "link": "link",
    "progress bar": "progressIndicator",
    "panel": "group",
    "section": "group",
    "filler": "group",
    "viewport": "group",
    "embedded": "group",
    "grouping": "group",
}
WINDOWS_ROLES = dict(Edit="textField", Document="document", Text="staticText", Pane="group", Group="group",
                     DataGrid="table", Table="table", DataItem="row", ListItem="row", Tree="outline", TreeItem="row",
                     Tab="tabGroup", TabItem="tab", Hyperlink="link", SplitButton="menuButton", Spinner="incrementor",
                     ProgressBar="progressIndicator", ToolBar="toolbar", Separator="splitter")

# Negotiated Windows read additions. They stay plain facts and are never used
# to route input or infer that a pattern/viewport describes a complete UI.
WINDOWS_READ_V2_FIELDS = {
    'Text.Selection': 'textSelectionRanges',
    'Text.VisibleRanges': 'textVisibleRanges',
    'Text.SelectionBounds': 'textSelectionGeometry',
    'UIA.Collection': 'collectionCoverage',
    'UIA.ReadSource': 'readSource',
    'UIA.ReferenceScope': 'referenceScope',
    'BoundingRectangle': 'nativeBounds',
    'IsOffscreen': 'offscreen',
    'IsControlElement': 'isControlElement',
    'IsContentElement': 'isContentElement',
    'FrameworkId': 'framework',
    'ClassName': 'nativeClass',
    'AccessKey': 'accessKey',
    'AcceleratorKey': 'acceleratorKey',
    'ItemType': 'itemType',
    'ItemStatus': 'itemStatus',
    'IsRequiredForForm': 'requiredForForm',
    'Selection.CanSelectMultiple': 'canSelectMultiple',
    'Selection.IsSelectionRequired': 'selectionRequired',
    'Scroll.HorizontallyScrollable': 'horizontallyScrollable',
    'Scroll.VerticallyScrollable': 'verticallyScrollable',
    'Scroll.HorizontalScrollPercent': 'horizontalScrollPercent',
    'Scroll.VerticalScrollPercent': 'verticalScrollPercent',
    'Scroll.HorizontalViewSize': 'horizontalViewSize',
    'Scroll.VerticalViewSize': 'verticalViewSize',
    'Window.WindowVisualState': 'windowVisualState',
    'Window.WindowInteractionState': 'windowInteractionState',
    'Table.RowOrColumnMajor': 'tableOrientation',
}

# Viewport/geometry queries may enter expensive provider layout code. Normal
# reads keep selection text; explicit native field queries can request these.
WINDOWS_ON_DEMAND_FIELDS = {'Text.VisibleRanges', 'Text.SelectionBounds'}

# Raw field names are kept at the boundary. Normalized names never replace
# them in the stored batch, and unfamiliar fields remain visible annotations.
FIELDS = {
    "macos":
    dict(
        AXRole="role", AXSubrole="subrole", AXRoleDescription="roleDescription", AXTitle="name",
        AXDescription="description", AXValue="value", AXIdentifier="identifier", AXHelp="help", AXURL="url",
        AXEnabled="enabled", AXSelected="selected", AXExpanded="expanded", AXFocused="focused", AXModal="modal",
        AXElementBusy="busy", AXHasPopup="hasPopup", AXSelectedText="selectedText",
        AXSelectedTextRange="selectionRange", AXPlaceholderValue="placeholder", AXDisclosing="disclosing",
        AXDisclosureLevel="disclosureLevel", AXEditable="editable", AXOrientation="orientation",
        AXRowIndexRange="rowRange", AXColumnIndexRange="columnRange", AXRowCount="rowCount",
        AXColumnCount="columnCount", AXIndex="index", **{
            "settable.AXFocused": "focusWritable", "settable.AXValue": "valueWritable", "settable.AXSelectedText":
            "textWritable"
        }),
    "windows":
    dict(
        ControlType="role", Name="name", LocalizedControlType="roleDescription", AutomationId="identifier",
        HelpText="help", **{
            "Win32.ClientRectangle": "clientBounds", "RangeValue.Value": "value", "Text.DocumentRange": "value",
            "Value.Value": "value", "SelectionItem.IsSelected": "selected", "Window.IsModal": "modal",
            "Toggle.ToggleState": "toggleState", "ExpandCollapse.ExpandCollapseState": "expandCollapseState",
            "RangeValue.Minimum": "rangeMinimum", "RangeValue.Maximum": "rangeMaximum", "RangeValue.SmallChange":
            "rangeSmallChange", "RangeValue.LargeChange": "rangeLargeChange", "RangeValue.IsReadOnly": "rangeReadOnly",
            "Text.IsReadOnly": "textReadOnly", "Text.SupportedTextSelection": "textSelection", "Grid.RowCount":
            "rowCount", "Grid.ColumnCount": "columnCount", "GridItem.Row": "cellRow", "GridItem.Column": "cellColumn",
            "GridItem.RowSpan": "cellRowSpan", "GridItem.ColumnSpan": "cellColumnSpan"
        }, IsEnabled="enabled", HasKeyboardFocus="focused", IsKeyboardFocusable="focusWritable",
        IsPassword=PROTECTED_FIELD, SupportedPatterns="interfaces", **{"Value.IsReadOnly":
                                                                       "readOnly"}, **WINDOWS_READ_V2_FIELDS),
    "linux":
    dict(
        role="role", name="name", description="description", accessibleId="identifier", enabled="enabled",
        selected="selected", expanded="expanded", focused="focused", modal="modal", editable="editable",
        readOnly="readOnly", focusable="focusWritable", sensitive="sensitive", showing="showing", visible="visible",
        checked="checked", indeterminate="indeterminate", multiLine="multiLine", attributes="attributes",
        interfaces="interfaces", **{
            "Value.currentValue": "value", "Text.text": "value", "Text.characterCount": "characterCount",
            "Text.caretOffset": "caretOffset", "Text.selections": "nativeSelections", "Table.nRows": "rowCount",
            "Table.nColumns": "columnCount", "TableCell.rowColumnSpan": "cellPosition", "Selection.nSelectedChildren":
            "selectedChildCount"
        }),
}
PROPERTIES = {platform: {value: key for key, value in fields.items()} for platform, fields in FIELDS.items()}
RELATIONS = {
    "macos":
    dict(AXChildren="children", AXParent="parent", AXTitleUIElement="labelledBy", AXServesAsTitleForUIElements="labels",
         AXDisclosedByRow="disclosedBy", AXWindow="window", AXVerticalScrollBar="verticalScrollBar",
         AXHorizontalScrollBar="horizontalScrollBar", AXRows="rows", AXVisibleRows="visibleRows", AXColumns="columns",
         AXVisibleColumns="visibleColumns", AXVisibleCells="visibleCells", AXSelectedCells="selectedCells",
         AXSelectedRows="selectedRows", AXSelectedColumns="selectedColumns", AXRowHeaderUIElements="rowHeaders",
         AXColumnHeaderUIElements="columnHeaders", AXDisclosedRows="disclosedRows", AXHeader="header", AXOwns="owns",
         AXLinkedUIElements="linked", AXSelectedChildren="selectedChildren"),
    "windows":
    dict(
        RawViewChildren="children", RawViewParent="parent", LabeledBy="labelledBy", **{
            "Selection.Selection": "selectedChildren", "SelectionItem.SelectionContainer": "selectionContainer",
            "GridItem.ContainingGrid": "table", "Table.RowHeaders": "rowHeaders", "Table.ColumnHeaders":
            "columnHeaders", "TableItem.RowHeaderItems": "rowHeaders", "TableItem.ColumnHeaderItems": "columnHeaders",
            "Text.Children": "embeddedChildren", "ItemContainer.Items": "logicalItems", "Grid.Cells": "cells"
        }),
    "linux": {
        "children": "children", "parent": "parent", "labelled-by": "labelledBy", "label-for": "labels",
        "Selection.selectedChildren": "selectedChildren", "TableCell.table": "table", "TableCell.rowHeaderCells":
        "rowHeaders", "TableCell.columnHeaderCells": "columnHeaders", "Table.cells": "cells",
        "Table.viewportCandidates": "viewportCandidates"
    },
}

# These relationships supply useful visible content even when Children omits
# it. Following them collects facts, never grants containment or input rights.
# Viewport members precede full rows so large virtual tables stay useful.
READ_RELATIONS = {
    'macos': ('AXVisibleRows', 'AXVisibleCells', 'AXRows', 'AXRowHeaderUIElements', 'AXColumnHeaderUIElements'),
    'windows': ('Selection.Selection', 'Table.RowHeaders', 'Table.ColumnHeaders', 'TableItem.RowHeaderItems',
                'TableItem.ColumnHeaderItems', 'Text.Children'),
    'linux': ('Table.viewportCandidates', 'Selection.selectedChildren', 'TableCell.rowHeaderCells',
              'TableCell.columnHeaderCells', 'Table.cells'),
}

# Searchable contents may be exposed separately from Children. Selection,
# headers, labels and ownership are useful observations but are not extra
# descendants for path matching. These edges never establish input authority.
CONTENT_RELATIONS = {
    'macos': ('AXVisibleRows', 'AXVisibleCells', 'AXRows'),
    'windows': (),
    # Viewport samples supplement observation, but are not a complete search
    # domain. Table.cells remains authoritative for bounded path uniqueness.
    'linux': ('Table.cells', ),
}


def followed_relations(platform, capabilities, *, content_only=False):
    if 'nativeFollowRelations' not in capabilities:
        return ()
    relations = CONTENT_RELATIONS if content_only else READ_RELATIONS
    gates = {
        'Table.cells': 'nativeTableContent', 'Table.viewportCandidates': 'nativeTableViewport', 'Text.Children':
        'nativeUiaReadV2'
    }
    return tuple(name for name in relations[platform] if name not in gates or gates.get(name) in capabilities)


def role_name(platform, value):
    if not isinstance(value, str):
        return "unknown"
    if platform == "linux":
        return LINUX_ROLES.get(value, "unknown")
    name = value.removeprefix("AX") if platform == "macos" else value.removeprefix("ControlType.")
    if platform == "windows" and name in WINDOWS_ROLES:
        return WINDOWS_ROLES[name]
    normalized = name[:1].lower() + name[1:]
    return normalized if re.fullmatch(r"[a-z][a-zA-Z0-9]*", normalized) else "unknown"


def action_for_intent(platform: str | None, role: str | None, actions: Collection[str], complete: bool, intent: str, *,
                      has_submenu=False) -> str | None:
    if platform == 'windows':
        # These are actual UIA pattern methods, not actions inferred from a
        # control type. A selectable item's ordinary click selects it; Invoke
        # may instead open the item. Other actions remain explicitly callable.
        if not complete:
            return None
        normalized = role_name(platform, role)
        if intent == 'openMenu':
            names = ('ExpandCollapse.Expand', ) if normalized in ('comboBox', 'menuButton', 'menuItem') else ()
        elif intent == 'click':
            names = (('SelectionItem.Select', 'Toggle.Toggle', 'Invoke.Invoke') if normalized in ('row', 'tab',
                                                                                                  'radioButton') else
                     ('Invoke.Invoke', 'Toggle.Toggle', 'SelectionItem.Select'))
            if normalized in ('comboBox', 'menuButton', 'menuItem'):
                names += ('ExpandCollapse.Expand', )
        else:
            return None
        for name in names:
            if name in actions:
                return name if sum(action == name for action in actions) == 1 else None
        return None
    if platform == 'linux':
        # AT-SPI names are provider-defined, not positional default-action IDs.
        # Map established GTK/Qt names only where their role has click semantics;
        # Entry.activate submits text, and row.activate may open a document.
        if not complete:
            return None

        def unique(*names):
            for name in names:
                matches = [a for a in actions if a.casefold() == name]
                if matches:
                    return matches[0] if len(matches) == 1 else None
            return None

        normalized = role_name(platform, role)
        if intent == 'openMenu':
            return unique('showmenu', 'show menu')
        if intent != 'click':
            return None
        if normalized not in ('button', 'menuButton', 'checkBox', 'radioButton', 'comboBox', 'menuItem', 'link', 'tab'):
            return None
        primary = unique('click', 'press')
        if primary is not None:
            return primary
        # A duplicate primary name is ambiguous, not evidence of its absence.
        if any(a.casefold() in ('click', 'press') for a in actions):
            return None
        if normalized in ('checkBox', 'radioButton'):
            return unique('toggle', 'activate')
        if normalized in ('button', 'menuButton', 'menuItem', 'link', 'tab'):
            return unique('activate')
        return None
    if platform != "macos":
        return None
    if intent == "openMenu":
        if "AXShowMenu" in actions:
            return "AXShowMenu"
        if role in ('AXMenuBarItem', 'AXMenuItem') and has_submenu and 'AXPress' in actions:
            return 'AXPress'
        return None
    if intent == "click":
        if "AXPress" in actions:
            return "AXPress"
        # Absence of Press must be known, not inferred from a partial catalogue.
        if complete and role in ("AXPopUpButton", "AXMenuButton", "AXComboBox") and "AXShowMenu" in actions:
            return "AXShowMenu"
    return None


def field_value(platform, node, semantic):
    """Read a typed fact, never interpreting false/zero as missing data."""
    key = PROPERTIES[platform].get(semantic)
    if key is None:
        return None
    state = node['fieldStatus'].get(key, {}).get('state')
    if state in ('absent', 'unsupported'):
        return None
    if state is not None or key not in node['values']:
        return UNKNOWN
    value = node['values'][key]
    if semantic in BOOLEAN_FIELDS:
        return value if isinstance(value, bool) else UNKNOWN
    if semantic == 'interfaces':
        return tuple(value) if (isinstance(value, list) and len(value) <= 64
                                and all(isinstance(item, str) and item for item in value)) else UNKNOWN
    return value if value is None or isinstance(value, str) else UNKNOWN


def normalize_control_values(platform, values):
    # Linux's existing native contract accepts either ENABLED or SENSITIVE.
    # Keep both raw facts intact; this derived value is only control semantics.
    if platform != 'linux':
        return values
    enabled, sensitive = values.get('enabled'), values.get('sensitive')
    usable = (True if enabled is True or sensitive is True else
              False if enabled is False and sensitive is False else UNKNOWN)
    return {**values, 'enabled': usable}


def control_values(platform, node):
    return normalize_control_values(platform, {name: field_value(platform, node, name) for name in EDITING_FIELDS})


def editing_evidence(role, values, *, platform=None):
    """Text receiver evidence, not focus readiness or a full-replace contract.

    A table/row with a writable value is not its active editor. Conversely,
    an editable web document need not enumerate paragraphs to be a receiver.
    This predicate is shared by display hints and ReceiverQuery.
    """
    if (values.get('enabled') is False or values.get('editable') is False or values.get('readOnly') is True):
        return False
    if role not in EDITOR_ROLES:
        return False
    if platform == 'windows':
        # TextPattern alone is read access. Its explicit document read-only
        # attribute may establish editability, but not a text mutation method.
        # Keep Value, Text and RangeValue declarations separate.
        patterns = values.get('interfaces', UNKNOWN)
        if values.get('textReadOnly') is True:
            return False
        return (True if isinstance(patterns, tuple) and
                ('ValuePatternIdentifiers.Pattern' in patterns and values.get('readOnly') is False
                 or 'TextPatternIdentifiers.Pattern' in patterns and values.get('textReadOnly') is False) else UNKNOWN)
    if platform == 'linux':
        # AT-SPI EDITABLE describes the object, not its child's editing route.
        # Qt can declare EDITABLE on a combo without exposing text methods.
        interfaces = values.get('interfaces', UNKNOWN)
        if interfaces is UNKNOWN or interfaces is None:
            return UNKNOWN
        if values.get('editable') is not True:
            return UNKNOWN
        if 'EditableText' in interfaces:
            return True
        # A declared text field can receive typed input even when it exposes
        # no readable Text range. A combo/spin owner needs a text declaration
        # of its own; EDITABLE + FOCUSABLE alone may describe its child editor.
        return ('Component' in interfaces and values.get('focusWritable') is True
                and (role in DOCUMENT_EDITORS | {'textField'} or 'Text' in interfaces))
    evidence = [values.get(key, UNKNOWN) for key in ('editable', 'valueWritable', 'textWritable')]
    if values.get('readOnly') is False:
        evidence.append(True)
    return True if any(value is True for value in evidence) else UNKNOWN if UNKNOWN in evidence else False


def direct_editor(role, values, *, platform=None):
    """Native declarations qualifying this node as an editing boundary.

    Scalar controls still need to resolve any contained editor before input.
    """
    return editing_evidence(role, values,
                            platform=platform) is True and (platform in ('linux', 'windows') or role in DOCUMENT_EDITORS
                                                            or values.get('focusWritable') is True)


def preparable_editor(role, values, *, platform=None):
    """An identified text control may expose editing only after native focus.

    This is preparation eligibility, not an editing capability or readiness.
    Generic containers/documents and explicitly read-only controls do not qualify.
    """
    return (role in EDITOR_BOUNDARIES and values.get('focusWritable') is True and values.get('enabled') is not False
            and values.get('editable') is not False and values.get('readOnly') is not True
            and values.get('textReadOnly') is not True
            and editing_evidence(role, values, platform=platform) is not True)


def composite_editor_owner(role, values, *, platform=None):
    """Whether this control can own an internal editor, not whether one exists.

    A missing AT-SPI EDITABLE state says this object cannot edit text directly;
    READ_ONLY is the independent prohibition that also applies to its editor.
    Ordinary containers still require an explicit editing declaration.
    """
    if role not in COMPOSITE_OWNERS or values.get('enabled') is False or values.get('readOnly') is True:
        return False
    if role in LAYOUT_ROLES:
        return values.get('editable') is True
    return platform == 'linux' or values.get('editable') is not False


def control_capabilities(platform, node, driver_features=(), *, scope_kind=None):
    """Observed semantic support; live scope/receiver checks still apply.

    Only routes backed by the connected driver's declarations are advertised.
    Composite editors require relation resolution; replacement needs its own
    native contract. Neither is guessed from the number of visible children.
    Unknown capabilities do not erase native fields or actions.
    """
    caps = {key: 'unknown' for key in ('click', 'typeText', 'replaceText', 'focus')}
    values = control_values(platform, node)
    if values['enabled'] is False:
        return {key: 'unavailable' for key in caps}
    raw_role = field_value(platform, node, 'role')
    role = UNKNOWN if raw_role is UNKNOWN else role_name(platform, raw_role)
    if (values['readOnly'] is True
            or values['editable'] is False and not composite_editor_owner(role, values, platform=platform)):
        caps['typeText'] = 'unavailable'
    if platform == 'linux':
        # AT-SPI explicitly denies direct text editing on this non-composite
        # object. The native replacement unit requires EDITABLE too; it is not
        # an independent writable-value route as it can be on other platforms.
        if values['editable'] is False and not composite_editor_owner(role, values, platform=platform):
            caps['replaceText'] = 'unavailable'
        actions = node['actions']
        if ('nativeActionIntents' in driver_features
                and action_for_intent(platform, raw_role, [a['name'] for a in actions['items']], actions['state']
                                      == 'complete', 'click') is not None):
            caps['click'] = 'available'
        interfaces = values['interfaces']
        if editing_evidence(role, values, platform=platform) is True and raw_role != 'password text':
            object_insert = {'Text', 'EditableText'} <= set(interfaces)
            keyboard_insert = ('nativeKeyboardIntents' in driver_features and 'Component' in interfaces
                               and values['focusWritable'] is True)
            if 'nativeTextInput' in driver_features and (object_insert or keyboard_insert):
                caps['typeText'] = 'available'
            if 'nativeTextReplacement' in driver_features and 'EditableText' in interfaces:
                caps['replaceText'] = 'available'
        if values['readOnly'] is True:
            caps['typeText'] = caps['replaceText'] = 'unavailable'
        can_focus_component = ('nativeKeyboardIntents' in driver_features and isinstance(interfaces, tuple)
                               and 'Component' in interfaces and values['focusWritable'] is True)
        if can_focus_component:
            caps['focus'] = 'available'
        return caps
    if platform == 'windows':
        actions = node['actions']
        if ('nativeActionIntents' in driver_features
                and action_for_intent(platform, raw_role, [a['name'] for a in actions['items']], actions['state']
                                      == 'complete', 'click') is not None):
            caps['click'] = 'available'
        patterns = values['interfaces']
        if 'nativeKeyboardIntents' in driver_features and values['focusWritable'] is True:
            caps['focus'] = 'available'
        if ('nativeTextInput' in driver_features and values['focusWritable'] is True
                and editing_evidence(role, values, platform=platform) is True):
            caps['typeText'] = 'available'
            if (isinstance(patterns, tuple) and 'TextPatternIdentifiers.Pattern' in patterns
                    and field_value(platform, node, 'textSelection') in ('Single', 'Multiple')):
                caps['replaceText'] = 'available'
        if values.get('textReadOnly') is True:
            caps['typeText'] = 'unavailable'
        if values['readOnly'] is True:
            caps['typeText'] = caps['replaceText'] = 'unavailable'
        else:
            can_replace_value = ('nativeTextReplacement' in driver_features and values['readOnly'] is False
                                 and isinstance(patterns, tuple) and 'ValuePatternIdentifiers.Pattern' in patterns)
            if can_replace_value:
                # SetValue replaces a whole string, including on custom controls.
                # It neither inserts at a caret nor proves a keyboard/focus route.
                caps['replaceText'] = 'available'
        return caps
    if platform != 'macos':
        return caps
    actions = node['actions']
    has_primary_action = ('nativeActionIntents' in driver_features
                          and (raw_role not in ('AXMenuBar', 'AXMenuBarItem', 'AXMenuItem', 'AXMenu')
                               or scope_kind == 'applicationMenu' and 'nativeMenuActions' in driver_features)
                          and action_for_intent(platform, raw_role, [a['name'] for a in actions['items']],
                                                actions['state'] == 'complete', 'click') is not None)
    if has_primary_action:
        caps['click'] = 'available'
    if 'nativeKeyboardIntents' in driver_features and values['focusWritable'] is True:
        caps['focus'] = 'available'
    if ('nativeTextInput' in driver_features and 'nativeActuators' in driver_features
            and direct_editor(role, values, platform=platform)):
        caps['typeText'] = 'available'
    return caps
