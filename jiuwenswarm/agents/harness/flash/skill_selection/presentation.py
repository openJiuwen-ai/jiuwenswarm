"""Small model-facing candidates; ranking and load tickets retain full metadata."""
import re

_CLAUSES = re.compile(r'(?<=[。！？;；\n])|(?<=\.)\s+(?=[A-Z])')
_RESTRICTION = re.compile(r'不支持|不能|无法|仅支持|仅限|只支持|只能|限制|'
                          r'\b(?:cannot|unsupported|only|requires?|must not)\b|does not|not support', re.I)


def compact_record(record):
    result = {}
    for key in ('supported', 'denied'):
        groups = {}
        for operation, format_name in record.get(key, ()):
            groups.setdefault(operation, []).append(format_name)
        if groups:
            result[key] = {operation: sorted(set(formats)) for operation, formats in sorted(groups.items())}
    for key in ('outputs', 'closed_outputs'):
        if record.get(key):
            result[key] = record[key]
    return result


def candidate_view(document, *, max_chars=640):
    description = ' '.join(document.description.split())
    candidate = {'name': document.name, 'description': description[:max_chars]}
    # Preserve restrictions anywhere in the bounded catalog description, not
    # only in its prefix. Do not silently turn an incomplete excerpt into proof
    # that a skill supports something; the model may always choose fallback.
    if len(description) > max_chars:
        candidate['description_truncated'] = True
        restrictions = [clause.strip() for clause in _CLAUSES.split(document.description)
                        if _RESTRICTION.search(clause) and clause.strip() not in candidate['description']]
        if restrictions:
            candidate['restrictions'] = list(dict.fromkeys(restrictions))
    record = compact_record(document.capability_record or {})
    if record:
        candidate['capabilities'] = record
    return candidate
