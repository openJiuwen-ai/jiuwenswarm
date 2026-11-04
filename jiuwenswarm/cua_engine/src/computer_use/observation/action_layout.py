"""Share identical secondary-action lists without sharing execution targets."""
import json

from .fact_groups import reference_set
from .projection_rules.policy import secondary_actions


def action_fields(owner, parts, render_actions):
    if not parts:
        actions = render_actions(owner)
        return ['Secondary Actions: ' + '; '.join(actions)] if actions else []
    groups = {}
    for source in (owner, *parts):
        actions = secondary_actions(source)
        if not actions or not render_actions(source):
            continue
        # Names as well as descriptions must agree. Equal display labels alone
        # do not mean that performAction accepts the same native action name.
        key = json.dumps([actions, render_actions(source)], ensure_ascii=True, sort_keys=True, separators=(',', ':'))
        groups.setdefault(key, []).append(source)
    fields, ordinary = [], []
    for sources in groups.values():
        if len(sources) > 1:
            fields.append('Secondary Actions [elements ' + reference_set(s['ref'] for s in sources) + ']: ' +
                          '; '.join(render_actions(sources[0])))
        else:
            source = sources[0]
            suffix = '' if source is owner else ' [element ' + source['ref'] + ']'
            ordinary.extend(action + suffix for action in render_actions(source))
    if ordinary:
        fields.insert(0, 'Secondary Actions: ' + '; '.join(ordinary))
    return fields
