"""Bounded local metadata reads, without executing any Skill code."""
import hashlib
import json
from pathlib import Path
import re
import stat
from .execution import checkpoint
from .freshness import source_stamp


def read_sources(directory, raw=None, *, observed_files=None):
    checkpoint()
    root = Path(directory).resolve()
    path = root / 'SKILL.md'
    if raw is None:
        if not path.is_file() or not path.resolve().is_relative_to(root) or path.stat().st_size > 1024 * 1024:
            return ()
        raw = path.read_text(encoding='utf-8-sig')
    # Hash the whole bounded root document, including text beyond the excerpt.
    # A later change must invalidate the metadata presented to the model.
    sources = [raw[:65536] + '\n[Source fingerprint: ' + hashlib.sha256(raw.encode()).hexdigest() + ']']
    # Only local Markdown references actually named by the root document.
    references = dict.fromkeys(re.findall(r'(?:\]\(|`)([^`\s()]+\.md)(?:\)|`)', raw, re.I))
    for reference in list(references)[:8]:
        checkpoint()
        try:
            reference_path = root / reference
            stamp = source_stamp(reference_path)
            target = Path(stamp.target)
            if observed_files is not None:
                # Missing references are tracked too: creating one changes the catalog.
                # Keep the referenced path so retargeting an in-root link is
                # detected as well as changing the original target's content.
                observed_files[reference_path] = stamp
            if not target.is_relative_to(root) or target == path or target.suffix.lower() != '.md':
                continue
            # The recorded target metadata already includes size; avoid opening
            # each path twice more for is_file() and stat().
            if stamp.file is not None and stat.S_ISREG(stamp.file[0]) and stamp.file[1] <= 65536:
                sources.append(target.read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError):
            continue
    return tuple(sources)


def source_fingerprint(description, sources):
    return hashlib.sha256(json.dumps([description, *sources], ensure_ascii=False).encode()).hexdigest()
