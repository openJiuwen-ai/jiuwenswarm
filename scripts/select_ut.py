"""Select PR unit tests."""

import json
from pathlib import Path
import subprocess
import sys

from ut_imports import (
    SOURCE_ROOT, UNIT_ROOT, import_test_hits, include_worker_peers, is_unit_test,
    mirrored_tests, normalize_ut_paths, worker_groups,
)


RULES = Path(__file__).with_name('ut_selection.json')
DOC_PREFIXES = ('docs/', '.agents/agent-docs/')
TEMPLATE_PREFIXES = ('.gitcode/ISSUE_TEMPLATE.zh/', '.gitcode/ISSUE_TEMPLATE.en/')


def git(*args: str) -> str:
    result = subprocess.run(['git', *args], check=True, capture_output=True)
    return result.stdout.decode()


def fallback(reason: str) -> list[str]:
    print(f'UT full suite: {reason}', file=sys.stderr)
    return [UNIT_ROOT]


def matches(path: str, rules: list[str]) -> bool:
    return any(path == rule or (rule.endswith('/') and path.startswith(rule)) for rule in rules)


def is_metadata(path: str) -> bool:
    if path.endswith('.md'):
        return path.startswith(DOC_PREFIXES) or path in {'README.md', 'README_CN.md'}
    return path.startswith(TEMPLATE_PREFIXES) and path.endswith(('.yml', '.yaml'))


def select(base_ref: str) -> list[str]:
    if not base_ref:
        return fallback('UT_BASE_SHA is missing')
    base = git('rev-parse', '--verify', '--end-of-options', f'{base_ref}^{{commit}}').strip()
    head = git('rev-parse', 'HEAD').strip()
    git('merge-base', '--is-ancestor', base, head)
    changed = git('diff', '--name-only', '-z', '--no-renames', base, head, '--').split(chr(0))[:-1]
    if not changed:
        return fallback('no committed diff against the supplied base')

    rules = json.loads(RULES.read_text(encoding='utf-8'))
    baseline = set(rules['baseline'])
    if not baseline or not all(is_unit_test(path) for path in baseline):
        return fallback('baseline test list is empty or invalid')

    features = rules['features']
    direct = set()
    affected = set()
    groups = set()
    needs_import_evidence = set()
    for path in changed:
        candidate = Path(path)
        if matches(path, rules['full_run']) or candidate.name == 'conftest.py':
            return fallback(f'shared file changed: {path!r}')
        if is_metadata(path):
            continue

        if path.startswith('tests/'):
            if not is_unit_test(path):
                return fallback(f'test infrastructure, non-UT, or removed test changed: {path!r}')
            direct.add(path)
            for name, feature in features.items():
                if matches(path, feature['tests']):
                    groups.add(name)
            continue

        hit_groups = [
            name for name, feature in features.items()
            if matches(path, feature['sources'])
        ]
        mirrors = mirrored_tests(path)
        if not hit_groups:
            if not path.startswith(SOURCE_ROOT) or candidate.suffix != '.py' or not candidate.is_file():
                return fallback(f'no safe mapping for {path!r}')
            if candidate.name == '__init__.py':
                return fallback(f'unmapped package initializer: {path!r}')
            if not mirrors:
                needs_import_evidence.add(path)
        groups.update(hit_groups)
        direct.update(mirrors)

    for name in groups:
        if not features[name]['tests']:
            return fallback(f'feature {name!r} has no regression tests')
        affected.update(path.rstrip('/') for path in features[name]['tests'])

    if any(path.startswith(SOURCE_ROOT) and path.endswith('.py') for path in changed):
        tracked = git('ls-files', '-z', '--', 'jiuwenswarm', 'tests').split(chr(0))
        hits = import_test_hits(changed, tracked)
        for source in sorted(needs_import_evidence):
            if not any(hits[source]):
                return fallback(f'no related UT found for {source!r}')
        import_direct = {test for first, _ in hits.values() for test in first}
        import_chained = {test for _, second in hits.values() for test in second} - import_direct
        print(f'UT import hits: direct={len(import_direct)}, chained={len(import_chained)}', file=sys.stderr)
        direct.update(import_direct | import_chained)
    selected = baseline | direct | affected
    include_worker_peers(selected, worker_groups(rules))
    paths = normalize_ut_paths(selected)
    print(f'UT {base[:12]}..{head[:12]}: {len(changed)} files changed, {len(paths)} test paths', file=sys.stderr)
    print(f'UT feature groups: {", ".join(sorted(groups)) or "none"}', file=sys.stderr)
    return paths


if __name__ == '__main__':
    try:
        paths = select(sys.argv[1] if len(sys.argv) > 1 else '')
    except SyntaxError as exc:
        paths = fallback(f'Python parse failed: {exc.filename!r}')
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        paths = fallback(f'{type(exc).__name__}: {exc}')
    for path in paths:
        print(path)
