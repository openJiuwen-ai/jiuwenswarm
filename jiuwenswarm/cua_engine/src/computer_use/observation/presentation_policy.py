"""Evidence used only to organize a fact-backed display, never to route input.

Supporting actions and provider-wide metadata are still emitted by native fact
accounting. They need not turn every layout wrapper into a separate UI control.
Unknown fields, incomplete observations and independent actions stay boundaries.
"""

SUPPORTING_ACTIONS = frozenset(('AXShowMenu', 'AXScrollToVisible'))
_BOOLEAN_METADATA = frozenset(('focusWritable', 'valueWritable', 'textWritable', 'editable', 'readOnly', 'textReadOnly',
                               'sensitive', 'protected', 'multiLine'))


def known_metadata(field, value):
    """Recognized metadata may share an annotation without forcing a wrapper.

    Keep this typed and narrow: being present in the field-name dictionary is
    not proof that an unfamiliar value is safe to move away from its node.
    """
    if field == 'editable' and value is True:
        # An explicit editor declaration is meaningful even when this driver
        # lacks a currently available input route.
        return False
    if field in _BOOLEAN_METADATA:
        return isinstance(value, bool)
    if field == 'orientation':
        return value in ('AXUnknownOrientation', 'AXHorizontalOrientation', 'AXVerticalOrientation')
    return False


def independent_actions(node):
    return any(action['name'] not in SUPPORTING_ACTIONS for action in node.get('actions', ()))


def editing(node):
    return (node['role'] in ('textField', 'textArea', 'comboBox', 'document')
            or any(node.get('capabilities', {}).get(key) == 'available' for key in ('typeText', 'replaceText')))


def display_copy(node):
    """Move incidental defaults to exact, grouped native facts, not oblivion."""
    result = {**node, '_factBacked': True}
    irrelevant_empty_selection = (not editing(node) and not node.get('focused') and not node.get('selectedText')
                                  and node.get('selectionRange') == dict(location=0, length=0))
    if irrelevant_empty_selection:
        result.pop('selectionRange')
    default_non_outline_depth = (node.get('disclosureLevel') == 0
                                 and node['role'] not in ('outline', 'row', 'disclosureTriangle')
                                 and not node.get('disclosedBy') and not node.get('expanded')
                                 and not node.get('disclosing'))
    if default_non_outline_depth:
        result.pop('disclosureLevel')
    return result
