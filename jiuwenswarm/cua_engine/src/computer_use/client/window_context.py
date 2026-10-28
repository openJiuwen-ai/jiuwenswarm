"""Reconcile App keyboard context after an observed modal view disappears.

An App keeps its working root. Element and image targets are exact observations
and never use this reconciliation. No tree, capture, activation or input occurs.
"""
from dataclasses import replace

from ..errors import ComputerUseError
from ..observation.app import window_binding
from ..observation.directory import select_surface


def refuse(code, detail):
    raise ComputerUseError(
        dict(code=code, effect='none', target='App keyboard context', detail=detail,
             next='read this App again before continuing; no input was sent'))


async def keyboard_context(reader, selection, observed, target, remaining):
    if target == selection.root:
        return target
    _, current = await reader.discover(selection, remaining)
    active = window_binding(select_surface(current, selection.root.root_window))
    if active == target:
        return target
    if current['stopReasons']:
        refuse('observation_incomplete', 'The window directory is partial; the previous modal view may still exist.')
    if any(item['parentState'] == 'unknown' for item in current['surfaces']):
        refuse('observation_incomplete', 'Window ownership is unknown; the keyboard context cannot be reconciled.')

    previous = {item['surface']: item for item in observed.source_directory['surfaces']}
    windows = {item['window']: item for item in previous.values()}
    present = {item['surface'] for item in current['surfaces']}
    surface, seen = target.surface, set()
    while surface != active.surface:
        if surface in seen or surface not in previous or surface in present:
            refuse('stale_target', 'The observed view changed without a confirmed return to its previous parent.')
        seen.add(surface)
        item = previous[surface]
        if item['modal'] is not True or item['parentState'] != 'known' or item['parent'] not in windows:
            refuse('observation_incomplete', 'The previous view has no confirmed modal-parent chain.')
        parent = windows[item['parent']]
        surface = parent['surface']
    if active != replace(target, surface=surface):
        refuse('stale_target', 'The previous keyboard context no longer belongs to the same working window.')
    return active
